# mailkanban — your Gmail as a kanban board

A Textual TUI that renders your Gmail labels as kanban columns. Move a
card, and your mail moves: messages get label changes applied over IMAP
with real Gmail "move" semantics (add target label, remove source label).

## Keys

| key | action |
|---|---|
| `h` / `l` | previous / next column |
| `j` / `k` | down / up through messages |
| `m` | move the focused message to another column (label) |
| `f` | view the full mail (modal, j/k scroll) |
| `a` | archive |
| `r` | refresh |
| `q` | quit |

## Run

    uv sync
    cp config.example.toml ~/.config/mailkanban/config.toml  # then fill it in
    uv run mailkanban

Gmail users: enable an app password (or your account's IMAP password)
and prefer leaving `password` empty in the config, setting
`MAILKANBAN_PASSWORD` instead. The config file is gitignored for a
reason — never commit credentials.

## Config

Columns are configured Gmail label names, rendered left to right:

```toml
[board]
columns = ["Inbox", "@todo", "@doing", "@done"]

[behavior]
move_removes_source = true   # Gmail "move" semantics
per_column_limit = 50
```

## Layout

- `src/mailkanban/imap.py` — Gmail-over-IMAP client (labels, moves,
  fetch, archive, reconnects)
- `src/mailkanban/app.py` — the Textual UI
- `src/mailkanban/config.py` — TOML config loading

Also live at: https://razodin137.github.io/2026-09-20-emailtui-kanban/