"""Configuration loading for mailkanban.

Credentials are never stored in the repository. Precedence for the app
password is: environment variable > config file. The config file is
expected to live at ~/.config/mailkanban/config.toml with mode 0600.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "mailkanban" / "config.toml"
PASSWORD_ENV = "MAILKANBAN_PASSWORD"


class ConfigError(Exception):
    """Raised when configuration is missing or unsafe."""


@dataclass(frozen=True)
class Account:
    address: str
    password: str
    host: str = "imap.gmail.com"
    port: int = 993


@dataclass(frozen=True)
class Behavior:
    move_removes_source: bool = True
    per_column_limit: int = 50


@dataclass(frozen=True)
class Config:
    account: Account
    columns: list[str] = field(default_factory=list)
    behavior: Behavior = field(default_factory=Behavior)


def _warn_if_permissive(path: Path) -> None:
    """Warn (do not fail) if the config file is group/world readable."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        # Emitted to stderr so it shows up before the TUI takes over.
        print(
            f"warning: {path} is group/world readable; "
            f"run `chmod 600 {path}`",
        )


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f"config not found at {path}. "
            f"Copy config.example.toml there and fill it in.",
        )

    _warn_if_permissive(path)

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    account_raw = raw.get("account", {})
    address = account_raw.get("address", "").strip()
    if not address:
        raise ConfigError("account.address is required")

    password = os.environ.get(PASSWORD_ENV, "") or account_raw.get("password", "")
    if not password:
        raise ConfigError(
            "no app password found; set "
            f"{PASSWORD_ENV} or account.password in {path}",
        )

    account = Account(
        address=address,
        password=password,
        host=account_raw.get("host", "imap.gmail.com"),
        port=int(account_raw.get("port", 993)),
    )

    columns = [c for c in raw.get("board", {}).get("columns", []) if c]
    if not columns:
        raise ConfigError("board.columns must list at least one label")

    behavior_raw = raw.get("behavior", {})
    behavior = Behavior(
        move_removes_source=bool(
            behavior_raw.get("move_removes_source", True),
        ),
        per_column_limit=int(behavior_raw.get("per_column_limit", 50)),
    )

    return Config(account=account, columns=columns, behavior=behavior)
