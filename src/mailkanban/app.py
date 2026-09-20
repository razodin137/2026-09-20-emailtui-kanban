"""Textual UI: labels as kanban columns, with move and refresh commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
)

from . import __version__
from .config import Config, ConfigError, load_config
from .imap import EmailHeader, FullEmail, GmailIMAP


def _merge_labels(config_columns: list[str], all_labels: list[str]) -> list[str]:
    """Combine configured columns with live labels, case-insensitively.

    Gmail reports the inbox as 'INBOX' while config may say 'Inbox'; keep
    the configured spelling and drop the duplicate.
    """
    merged: dict[str, str] = {}
    for name in [*config_columns, *all_labels]:
        merged.setdefault(name.lower(), name)
    return sorted(merged.values())


class EmailCard(ListItem):
    """One message row, focused or selectable within a column."""

    def __init__(self, header: EmailHeader) -> None:
        super().__init__()
        self.header = header

    def compose(self) -> ComposeResult:
        sender = self.header.sender.split("<")[0].strip() or self.header.sender
        yield Label(f"[b]{sender[:36]}[/b]")
        yield Label(self.header.subject[:38])
        yield Label(f"[dim]{self.header.date}[/dim]")


class Column(Vertical):
    """A single kanban column backed by a Gmail label.

    Widget ids must be letters/numbers/underscores/hyphens, so labels
    (which may contain '@' or spaces) are keyed by their board index.
    """

    def __init__(self, index: int, label: str) -> None:
        super().__init__(id=f"col-{index}")
        self.label = label
        self.list_view = ListView(id=f"list-{index}")

    def compose(self) -> ComposeResult:
        yield Label(f"[b]{self.label}[/b]", classes="column-title")
        yield self.list_view


class MoveScreen(ModalScreen[str | None]):
    """Modal label picker shown when the user presses M."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
    ]

    def __init__(self, labels: list[str], current: list[str]) -> None:
        super().__init__()
        self._labels = labels
        self._current = current
        self._by_item: dict[ListItem, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="move-dialog"):
            yield Label("Move to which label?  (j/k to move, enter to pick)")
            items: list[ListItem] = []
            for lbl in self._labels:
                text = f"{lbl} {'(current)' if lbl in self._current else ''}".strip()
                item = ListItem(Label(text))
                self._by_item[item] = lbl
                items.append(item)
            yield ListView(*items, id="move-list")

    def on_mount(self) -> None:
        if self._labels:
            self.query_one("#move-list", ListView).index = 0

    def _list(self) -> ListView:
        return self.query_one("#move-list", ListView)

    def action_cursor_down(self) -> None:
        self._list().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._list().action_cursor_up()

    @on(ListView.Selected, "#move-list")
    def _selected(self, event: ListView.Selected) -> None:
        self.dismiss(self._by_item.get(event.item))

    def action_cancel(self) -> None:
        self.dismiss(None)


class MailScreen(ModalScreen[None]):
    """Full text of a single message."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("j", "scroll_down", "Down", show=False),
        Binding("k", "scroll_up", "Up", show=False),
        Binding("down", "scroll_down", "Down", show=False),
        Binding("up", "scroll_up", "Up", show=False),
    ]

    def __init__(self, uid: str, mailbox: str) -> None:
        super().__init__()
        self._uid = uid
        self._mailbox = mailbox

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="mail-dialog"):
            yield Label("Loading…", id="mail-body")

    def on_mount(self) -> None:
        self.load_body()

    @work(exclusive=True, thread=True)
    def load_body(self) -> None:
        try:
            email = self._imap_client().fetch_full(self._uid, self._mailbox)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(
                self.query_one("#mail-body", Label).update,
                f"Failed to load message:\n\n{exc}",
            )
            return
        self.app.call_from_thread(self._show_email, email)

    def _imap_client(self) -> GmailIMAP:
        return self.app.client  # type: ignore[attr-defined]

    def _show_email(self, email: FullEmail) -> None:
        header = (
            f"[b]{email.subject}[/b]\n"
            f"From: {email.sender}\n"
            f"To:   {email.to}\n"
            f"Date: {email.date}\n"
            + "-" * 60 + "\n"
        )
        self.query_one("#mail-body", Label).update(header + email.body)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_scroll_down(self) -> None:
        self.query_one("#mail-dialog", VerticalScroll).scroll_down()

    def action_scroll_up(self) -> None:
        self.query_one("#mail-dialog", VerticalScroll).scroll_up()


class MailKanban(App):
    CSS = """
    Screen { layout: vertical; }
    #board { height: 1fr; }
    Column {
        width: 42;
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    Column:focus-within { border: round $accent; }
    .column-title { text-style: bold; }
    EmailCard { height: auto; padding: 0 0 1 0; }
    #move-dialog {
        width: 60;
        height: 60%;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #move-list { height: 1fr; }
    #mail-dialog {
        width: 90%;
        height: 90%;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #mail-body { width: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("m", "move", "Move"),
        Binding("f", "view", "Full mail"),
        Binding("a", "archive", "Archive"),
        Binding("enter", "archive", "Archive", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("left", "prev_column", "Prev", show=False),
        Binding("right", "next_column", "Next", show=False),
        Binding("h", "prev_column", "Prev", show=False),
        Binding("l", "next_column", "Next", show=False),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.client = GmailIMAP(config.account)
        self._all_labels: list[str] = []
        self._focused_index: int = 0
        self._columns: list[Column] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="board"):
            for index, label in enumerate(self.config.columns):
                column = Column(index, label)
                self._columns.append(column)
                yield column
        yield Footer()

    @property
    def _focused_label(self) -> str:
        return self.config.columns[self._focused_index]

    def on_mount(self) -> None:
        self.sub_title = self.config.account.address
        self.load_board()

    def on_unmount(self) -> None:
        self.client.close()

    # --- background work -------------------------------------------------

    @work(exclusive=True, thread=True)
    def load_board(self) -> None:
        """Connect if needed, then fetch every column's headers.

        Runs as a single exclusive worker so a move-triggered reload cannot
        race a refresh; the IMAP client serializes calls internally anyway.
        """
        try:
            self.client.connect()
            self._all_labels = self.client.list_labels()
        except Exception as exc:  # noqa: BLE001 - surface any IMAP failure
            self.call_from_thread(self.notify, f"IMAP error: {exc}", severity="error")
            return

        for index, label in enumerate(self.config.columns):
            try:
                headers = self.client.fetch_column(
                    label, self.config.behavior.per_column_limit,
                )
            except Exception as exc:  # noqa: BLE001
                self.call_from_thread(
                    self.notify, f"{label}: {exc}", severity="error",
                )
                continue
            self.call_from_thread(self._populate, index, headers)

    def _populate(self, index: int, headers: list[EmailHeader]) -> None:
        list_view = self._columns[index].list_view
        list_view.clear()
        for header in headers:
            list_view.append(EmailCard(header))
        if headers:
            list_view.index = 0

    # --- navigation ------------------------------------------------------

    def _current_list(self) -> ListView:
        return self._columns[self._focused_index].list_view

    def action_cursor_down(self) -> None:
        self._current_list().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._current_list().action_cursor_up()

    def action_prev_column(self) -> None:
        self._focus_offset(-1)

    def action_next_column(self) -> None:
        self._focus_offset(1)

    def _focus_offset(self, delta: int) -> None:
        count = len(self.config.columns)
        self._focused_index = (self._focused_index + delta) % count
        self._current_list().focus()

    # --- commands --------------------------------------------------------

    def action_refresh(self) -> None:
        self.notify("Refreshing…")
        self.load_board()

    def action_move(self) -> None:
        highlighted = self._current_list().highlighted_child
        if not isinstance(highlighted, EmailCard):
            self.notify("No message selected", severity="warning")
            return
        header = highlighted.header
        source = self._focused_label
        labels = _merge_labels(self.config.columns, self._all_labels)
        self.push_screen(
            MoveScreen(labels, header.labels),
            lambda target: self._apply_move(header, source, target),
        )

    def action_view(self) -> None:
        highlighted = self._current_list().highlighted_child
        if not isinstance(highlighted, EmailCard):
            self.notify("No message selected", severity="warning")
            return
        self.push_screen(
            MailScreen(highlighted.header.uid, self._focused_label),
        )

    def action_archive(self) -> None:
        """Remove the current column's label, like Gmail's swipe-to-archive.

        Archiving the inbox means removing 'Inbox' (the message stays in
        All Mail); in any other column it removes that label.
        """
        highlighted = self._current_list().highlighted_child
        if not isinstance(highlighted, EmailCard):
            self.notify("No message selected", severity="warning")
            return
        header = highlighted.header
        source = self._focused_label
        self._apply_archive(header, source)

    @on(ListView.Selected)
    def _on_message_selected(self, event: ListView.Selected) -> None:
        """Enter on a message card archives it.

        ListView consumes `enter` and posts Selected, so the App-level
        binding cannot fire for it; handle the event instead. MoveScreen's
        items are plain ListItems, so its own Selected is ignored here.
        """
        if isinstance(event.item, EmailCard):
            self._apply_archive(event.item.header, self._focused_label)

    @work(exclusive=False, thread=True)
    def _apply_archive(self, header: EmailHeader, source: str) -> None:
        try:
            self.client.archive(header.uid, source)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify, f"Archive failed: {exc}", severity="error",
            )
            return
        self.call_from_thread(
            self.notify, f"Archived {header.subject[:30]}",
        )
        self.call_from_thread(self.load_board)

    @work(exclusive=False, thread=True)
    def _apply_move(
        self, header: EmailHeader, source: str, target: str | None,
    ) -> None:
        if not target or target == source:
            return
        remove = source if self.config.behavior.move_removes_source else None
        try:
            self.client.set_labels(
                header.uid, source, add=target, remove=remove,
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Move failed: {exc}", severity="error")
            return
        self.call_from_thread(
            self.notify, f"Moved {header.subject[:30]} → {target}",
        )
        self.call_from_thread(self.load_board)


def _print(msg: str) -> None:
    print(msg, file=sys.stderr)


def run_preflight(config: Config, *, verbose: bool = False) -> bool:
    """Validate config and IMAP login without starting the TUI."""
    _print(f"mailkanban {__version__} (preflight)")
    _print(f"account:  {config.account.address}")
    _print(f"host:     {config.account.host}:{config.account.port}")
    _print(f"columns:  {', '.join(config.columns)}")

    client = GmailIMAP(config.account)
    try:
        client.connect()
    except Exception as exc:  # noqa: BLE001
        _print(f"FAILED: IMAP login: {exc}")
        return False
    try:
        labels = client.list_labels()
        _print(f"login:    OK ({len(labels)} labels visible)")
        known = {lbl.lower() for lbl in labels}
        missing = [c for c in config.columns if c.lower() not in known]
        if missing:
            _print(f"warning:  columns not found as labels: {', '.join(missing)}")
        if verbose:
            for label in labels:
                _print(f"  label: {label}")
    finally:
        client.close()
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mailkanban",
        description="A Gmail kanban TUI: labels as columns.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config and IMAP login, then exit",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to config.toml (default: ~/.config/mailkanban/config.toml)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="with --check, list every visible label",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"mailkanban {__version__}",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _print(f"mailkanban: {exc}")
        raise SystemExit(2) from exc

    if args.check:
        raise SystemExit(0 if run_preflight(config, verbose=args.verbose) else 1)

    _print(f"mailkanban {__version__}: starting, connecting to "
           f"{config.account.host} as {config.account.address}…")
    MailKanban(config).run()


if __name__ == "__main__":
    main()
