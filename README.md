# Wispr Archiver

Keep a local copy of your Wispr Flow Notetaker meetings on macOS or Ubuntu. The worker reads finalized meetings through Wispr's official Remote MCP server and saves each one as Markdown and JSON. You can read a meeting offline, retain earlier versions, and copy the archive to Google Drive.

Each record includes the transcript, summary, personal notes, action items, available participants and meeting metadata. Meeting text stays in its original language. The archive contains the text Wispr makes available at the time of collection. Audio and per-speaker timestamps are outside the MCP export.

## Get started

You need a Wispr account with Cloud Sync enabled and access to Remote MCP. Choose the host that will hold your archive and sign in to Wispr there. The refresh token rotates, so run one scheduled worker per archive.

- **macOS** — Start with a local export. To run every half hour after login, configure Google Drive and enable the LaunchAgent. [Set up a Mac](docs/RUNBOOK.md#macos)
- **Ubuntu 24.04 LTS** — Install under a service account, sign in through an SSH tunnel, then enable the systemd timers. [Set up an Ubuntu server](docs/RUNBOOK.md#ubuntu)
- **Moving an existing archive** — Restore its original files from Drive before starting the new worker. [Restore from Drive](docs/RUNBOOK.md#restore-from-drive)

Use a clone or an extracted source release. The worker uses Python 3.12, which uv can install. Install rclone for Drive replication and for the Ubuntu installer. [Install uv](https://docs.astral.sh/uv/getting-started/installation/) · [Install rclone](https://rclone.org/install/)

## What stays in the archive

| Location | Contents |
| --- | --- |
| `meetings/YYYY/MM/` | A JSON record and readable Markdown file for each meeting. The path stays fixed after the first save. |
| `_snapshots/<meeting-id>/` | An immutable JSON snapshot for each distinct version, including versions with a shortened transcript. |
| SQLite state (outside the archive) | Sync progress, errors, and run history. The meeting index can be rebuilt from the JSON records. |

The worker polls every 30 minutes on a running, configured host. If a transcript disappears or shrinks by more than 10%, the fuller version stays in the main files. Weekly reconciliation marks meetings that disappeared upstream. Drive publication uses checksum-based copies and preserves older snapshots. Start the first export while the source meetings are still available.

## Development

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Tests use synthetic meetings and a fake Wispr server. With rclone installed, the suite also copies files between local directories. When changing sync or storage, check that a second sync of identical inputs writes zero archive bytes and that a shorter transcript preserves the fuller main copy.

Use read-only tools on the official Remote MCP endpoint. Preserve upstream text verbatim. For parser changes, check the observed response envelopes, chunk markers and unresolved cases in the [MCP contract](docs/wispr-mcp-contract.md).

When reporting an issue, include the command, exit code and error kind. Keep meeting contents, titles, OAuth URLs, tokens and rclone configuration private. Run live checks only with your own account.

## License

Wispr Archiver is an independent project distributed under the [MIT License](LICENSE).
