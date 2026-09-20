"""Minimal IMAP access for Gmail labels, using only the standard library.

Gmail exposes labels through the IMAP extensions X-GM-LABELS and
X-GM-RAW. `imaplib` does not wrap these, but allows sending raw
commands, which is what we do here.
"""

from __future__ import annotations

import imaplib
import re
import ssl
import threading
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime

from .config import Account


@dataclass
class EmailHeader:
    uid: str
    subject: str
    sender: str
    date: str
    labels: list[str]


@dataclass
class FullEmail:
    uid: str
    subject: str
    sender: str
    to: str
    date: str
    body: str


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError):
        return value


def _quote(label: str) -> str:
    """Quote an IMAP mailbox/label name for use in a raw command."""
    escaped = label.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _parse_list_response(raw: bytes) -> list[str]:
    """Turn a raw X-GM-LABELS value like (\\Inbox foo bar) into names."""
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    # Labels may be quoted; split respecting quotes.
    labels: list[str] = []
    buf = ""
    in_quotes = False
    escape = False
    for ch in text:
        if escape:
            buf += ch
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == '"':
            in_quotes = not in_quotes
        elif ch == " " and not in_quotes:
            if buf:
                labels.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        labels.append(buf)
    return [lbl for lbl in labels if lbl and not lbl.startswith("\\")]


class GmailIMAP:
    def __init__(self, account: Account) -> None:
        self._account = account
        self._conn: imaplib.IMAP4_SSL | None = None
        self._label_index: dict[str, str] = {}
        # imaplib is not thread-safe: a single connection must not be used
        # from more than one thread at a time or TLS framing gets corrupted
        # (manifesting as ssl.SSLEOFError). Serialize every socket call.
        self._lock = threading.RLock()

    def connect(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            self._connect_locked()

    def _connect_locked(self) -> None:
        conn = imaplib.IMAP4_SSL(self._account.host, self._account.port)
        conn.login(self._account.address, self._account.password)
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            finally:
                self._conn = None

    def _require(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            raise RuntimeError("not connected; call connect() first")
        return self._conn

    def _reconnect(self) -> None:
        """Drop a dead connection and log in again (caller holds the lock)."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        self._connect_locked()

    def _call(self, method: str, *args, **kwargs):
        """Run an imaplib method under the lock, reconnecting once on drop."""
        with self._lock:
            try:
                return getattr(self._require(), method)(*args, **kwargs)
            except (ssl.SSLError, imaplib.IMAP4.abort, OSError):
                self._reconnect()
                return getattr(self._require(), method)(*args, **kwargs)

    def list_labels(self) -> list[str]:
        # Gmail returns labels as mailboxes; X-GM-LABELS covers the tags too.
        typ, data = self._call("list")
        if typ != "OK":
            return []
        names: list[str] = []
        for item in data:
            if not item:
                continue
            line = item.decode("utf-8", errors="replace")
            if '"' in line:
                names.append(line.rsplit('"', 2)[1])
        # Drop system folders ([Gmail]/..., [Superhuman]/...) and collapse
        # case-duplicates such as INBOX vs Inbox. Keep first occurrence.
        keep: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name.startswith("["):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            keep.append(name)
        self._label_index = {n.lower(): n for n in keep}
        return sorted(keep)

    def _resolve_mailbox(self, label: str) -> str:
        """Map a config label to the canonical IMAP mailbox name.

        Gmail's system inbox is reported as 'INBOX' regardless of case,
        so a case-insensitive lookup keeps config files friendly.
        """
        if label.lower() == "inbox":
            return "INBOX"
        return self._label_index.get(label.lower(), label)

    def _resolve_gm_label(self, label: str) -> str:
        """Map a config label to its X-GM-LABELS form.

        The inbox is the special system label '\\Inbox'.
        """
        if label.lower() == "inbox":
            return "\\Inbox"
        return self._label_index.get(label.lower(), label)

    def _select_writable(self, mailbox: str) -> None:
        """SELECT a mailbox read-write, falling back to INBOX.

        STORE is only legal in the SELECTED state, and any failed SELECT
        (a label that does not exist) or reconnect drops imaplib back to
        AUTH. Callers must hold the lock.
        """
        typ, _ = self._call(
            "select", _quote(self._resolve_mailbox(mailbox)), readonly=False,
        )
        if typ == "OK":
            return
        typ, _ = self._call("select", "INBOX", readonly=False)
        if typ != "OK":
            raise imaplib.IMAP4.error(
                f"cannot select a writable mailbox {mailbox!r}",
            )

    def archive(self, uid: str, mailbox: str) -> None:
        """Archive a message, matching Gmail's swipe-to-archive.

        Gmail silently ignores '-X-GM-LABELS (\\Inbox)', so the inbox label
        cannot be removed that way. Selecting the mailbox and flagging the
        message \\Deleted then UID EXPUNGE removes it from that mailbox only:
        for INBOX that is "archive" (the message stays in All Mail), and for
        any other label it removes the label.
        """
        with self._lock:
            self._select_writable(mailbox)
            self._call("uid", "STORE", uid, "+FLAGS", "(\\Deleted)")
            self._call("uid", "EXPUNGE", uid)

    def set_labels(
        self,
        uid: str,
        mailbox: str,
        *,
        add: str | None = None,
        remove: str | None = None,
    ) -> None:
        """Add and/or remove a Gmail label on a message.

        The message UID is stable across Gmail mailboxes, so selecting the
        mailbox it was listed under gives a valid SELECTED state for STORE.
        """
        with self._lock:
            self._select_writable(mailbox)
            if add:
                self._call(
                    "uid", "STORE", uid, "+X-GM-LABELS",
                    f"({_quote(self._resolve_gm_label(add))})",
                )
            if remove:
                self._call(
                    "uid", "STORE", uid, "-X-GM-LABELS",
                    f"({_quote(self._resolve_gm_label(remove))})",
                )

    def fetch_full(self, uid: str, mailbox: str) -> FullEmail:
        """Fetch the complete message body and headers for a UID."""
        with self._lock:
            self._select_writable(mailbox)
            typ, data = self._call("uid", "FETCH", uid, "(RFC822)")
        raw = b""
        if typ == "OK":
            for item in data:
                if isinstance(item, tuple):
                    raw = item[1]
                    break
        if not raw:
            raise imaplib.IMAP4.error(f"no body returned for uid {uid}")

        msg: Message = BytesParser(policy=default_policy).parsebytes(raw)
        return FullEmail(
            uid=uid,
            subject=_decode(msg.get("Subject")) or "(no subject)",
            sender=_decode(msg.get("From")) or "(unknown sender)",
            to=_decode(msg.get("To")),
            date=_format_date(msg.get("Date") or ""),
            body=_extract_body(msg),
        )

    def fetch_column(self, label: str, limit: int) -> list[EmailHeader]:
        """Return header rows for the newest `limit` messages in a label."""
        typ, _ = self._call(
            "select", _quote(self._resolve_mailbox(label)), readonly=True,
        )
        if typ != "OK":
            return []

        typ, data = self._call("uid", "SEARCH", "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-limit:]

        headers: list[EmailHeader] = []
        for uid in reversed(uids):
            headers.append(self._fetch_one(uid))
        return headers

    def _fetch_one(self, uid: bytes) -> EmailHeader:
        typ, data = self._call(
            "uid",
            "FETCH",
            uid.decode(),
            "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)] X-GM-LABELS)",
        )
        subject = sender = date = ""
        labels: list[str] = []
        if typ == "OK":
            for item in data:
                if isinstance(item, tuple):
                    block = item[1].decode("utf-8", errors="replace")
                    for line in block.splitlines():
                        lower = line.lower()
                        if lower.startswith("subject:"):
                            subject = _decode(line.split(":", 1)[1].strip())
                        elif lower.startswith("from:"):
                            sender = _decode(line.split(":", 1)[1].strip())
                        elif lower.startswith("date:"):
                            date = _format_date(line.split(":", 1)[1].strip())
                    labels = _extract_gm_labels(item[0])
                else:
                    labels.extend(_extract_gm_labels(item))
        return EmailHeader(
            uid=uid.decode(),
            subject=subject or "(no subject)",
            sender=sender or "(unknown sender)",
            date=date,
            labels=labels,
        )

    def add_label(self, uid: str, label: str) -> None:
        self._call(
            "uid", "STORE", uid, "+X-GM-LABELS",
            f"({_quote(self._resolve_gm_label(label))})",
        )

    def remove_label(self, uid: str, label: str) -> None:
        self._call(
            "uid", "STORE", uid, "-X-GM-LABELS",
            f"({_quote(self._resolve_gm_label(label))})",
        )


def _extract_body(msg: Message) -> str:
    """Return a readable text body, preferring text/plain."""
    if msg.is_multipart():
        plain = html = None
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() == "text/plain" and plain is None:
                plain = part
            elif part.get_content_type() == "text/html" and html is None:
                html = part
        chosen = plain or html
        if chosen is None:
            return "(no readable text body)"
        return _part_text(chosen, strip_html=chosen is html)
    return _part_text(msg, strip_html=msg.get_content_type() == "text/html")


def _part_text(part: Message, *, strip_html: bool) -> str:
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode(part.get_content_charset() or "utf-8", "replace")
    if not isinstance(content, str):
        content = str(content)
    if strip_html:
        content = _strip_html(content)
    return content.strip() or "(empty message)"


def _strip_html(html: str) -> str:
    """Very small HTML-to-text conversion; no third-party parser."""
    import html as html_mod

    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_gm_labels(fetch_line: bytes) -> list[str]:
    """Pull the X-GM-LABELS value out of a FETCH response line."""
    marker = b"X-GM-LABELS"
    idx = fetch_line.upper().find(marker)
    if idx == -1:
        return []
    rest = fetch_line[idx + len(marker):].strip()
    if rest.startswith(b"("):
        rest = rest[1:]
    # The label list ends at the closing paren of the FETCH item.
    end = rest.find(b")")
    if end != -1:
        rest = rest[:end]
    return _parse_list_response(rest)


def _format_date(value: str) -> str:
    try:
        return parsedate_to_datetime(value).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value
