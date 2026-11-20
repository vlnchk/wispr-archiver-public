# Operator runbook

Choose one host to own the local archive, SQLite state and rotating Wispr token. Use the same OS account for setup and operation. The default display timezone is UTC. When upgrading or restoring an existing archive, explicitly set `timezone` to the zone originally used to render its Markdown. Keep the archive outside the source checkout. Drive contains a replica of this archive.

## macOS

Use a terminal in a permanent checkout. Install uv, then prepare the worker and private configuration. For the scheduled worker or a Drive replica, also install rclone.

```sh
uv sync --locked --no-dev
umask 077
mkdir -p "$HOME/.config/wispr-archiver" "$HOME/Library/Application Support/wispr-archiver"
cp -n deploy/mac.example.toml "$HOME/.config/wispr-archiver/mac.toml"
```

Edit `~/.config/wispr-archiver/mac.toml`. For Drive replication, set `rclone_binary` to the absolute path from `command -v rclone`. Choose the archive's display timezone (an IANA name such as `Europe/London` or `UTC`). Keep this timezone available for verifying rendered Markdown. The example puts the archive and database under your user Library. For an existing installation, retain the original archive, state, timezone and Drive folder.

Define this shell function in the checkout for the manual commands below. The schedule reads the same configuration.

```sh
wa() { .venv/bin/wispr-archiver "$@" --config "$HOME/.config/wispr-archiver/mac.toml"; }
wa login
```

Complete the browser sign-in within five minutes, then follow [First export](#first-export). For a local-only archive, run `wa sync` manually for subsequent updates. For the schedule, first configure and verify the Drive replica, then enable the LaunchAgent:

```sh
.venv/bin/wispr-mac start
.venv/bin/wispr-mac status
```

A per-user LaunchAgent runs at login and at minutes 00 and 30. After sleep, launchd coalesces missed calendar events. After reboot, sign in to your account. The first cycle and the first cycle seven days after a successful full reconciliation run `reconcile`. Other cycles run `sync`. Each cycle then attempts `publish`. Check the UTC “Last published” timestamp and the last five runs for exit codes. An enabled schedule can show `state = not running` between cycles.

To use the short command in any terminal, add `~/.local/bin` to PATH and run the following in the checkout.

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/.venv/bin/wispr-mac" "$HOME/.local/bin/wispr-mac"
```

`wispr-mac stop` disables the schedule. `wispr-mac uninstall` also removes the matching command symlink. Both preserve the data, configuration and credentials. If a cycle is active, wait for it to finish and retry. The job uses `~/Library/LaunchAgents/local.wispr-archiver.plist` and a `mac-cycle.lock` beside the TOML file. If a job stops running, check the checkout, its virtual environment and the macOS background-item setting.

## Ubuntu

Use an Ubuntu 24.04 LTS host with Git, uv and rclone available. Connect using an SSH tunnel for the browser callback. Replace `user@your-server` with your SSH destination.

```sh
ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:33418:127.0.0.1:33418 user@your-server
```

On the server, check out the release you intend to run. From that checkout, check the prerequisites and install.

```sh
rclone version
sudo uv --version
sudo bash deploy/install.sh
```

The installer provisions a service user, installs Python 3.12 and the locked dependencies under `/opt/wispr-archiver`, checks the CLI and atomically activates the release. Reinstallation preserves the archive and the existing environment file. Data lives under `/var/lib/wispr-archiver`. A fresh installation leaves the timers disabled. [Installing uv](https://docs.astral.sh/uv/getting-started/installation/)

To receive Telegram notifications, fill in the bot token and chat ID in `sudoedit /etc/wispr-archiver/env`. The file is owned by root with mode 0600. Put nonsecret options such as `timezone` and `rclone_remote` in `/etc/wispr-archiver/config.toml` if needed. Define this helper in each SSH session. It reads the service environment and runs the worker as the service user.

```sh
wa() {
  sudo systemd-run --quiet --wait --collect --pty \
    -p User=wispr-archiver -p Group=wispr-archiver -p UMask=0027 \
    -p WorkingDirectory=/var/lib/wispr-archiver -p Environment=HOME=/var/lib/wispr-archiver \
    -p EnvironmentFile=/etc/wispr-archiver/env \
    /opt/wispr-archiver/current/bin/wispr-archiver "$@"
}
wa login --no-browser
```

Open the issued URL on the workstation while the tunnel is active. Then configure rclone as the service user.

```sh
sudo -u wispr-archiver -H sh -c 'umask 077; exec rclone config'
```

Select the settings under [Google Drive](#google-drive). For a headless server, answer `n` to browser authentication and run the exact `rclone authorize` command printed by the server on your workstation. Paste the returned block into the server terminal. Check credential permissions without displaying their contents.

```bash
sudo -u wispr-archiver -H stat -c '%a %U %n' /var/lib/wispr-archiver/.config/{wispr-archiver/token.json,rclone/rclone.conf}
```

Expect mode 600 and owner `wispr-archiver` for both files. If using Telegram on a fresh installation, start `sudo systemctl start wispr-archiver@deadman-check.service` before the first backfill and confirm delivery of the inactivity alert. Finish the first export before enabling the timers.

## First export

Use the `wa` helper defined for your platform. If you already have an archive on Drive, follow [Restore from Drive](#restore-from-drive) before starting a new local archive.

```sh
wa backfill
wa verify
```

Check the meeting count and look for an empty `problems` list. You can now open a Markdown file under `meetings/`. To preview a backfill, add `--dry-run`. A preview reads from your Wispr account.

For a Drive replica, [configure rclone](#google-drive) as the worker's user and select a new folder for this archive. Then run:

```sh
wa publish
wa verify-remote
```

Expect `ok: true`, `matched == local_files` and zero missing, differing or unreadable files. An empty account has no files to publish. To check idempotence when the source has not changed, repeat `wa sync` and `wa publish`. Expect zero archive bytes written and zero transfers. Once the schedule is enabled, use `wa status` to confirm the first automatic cycle and practice [restoring the replica](#restore-from-drive) into a fresh directory.

## Ubuntu timers

```bash
sudo systemctl enable --now wispr-archiver-{sync,reconcile,deadman}.timer
systemctl list-timers --all 'wispr-archiver-*.timer'
wa status
sudo journalctl --since '1 hour ago' -u wispr-archiver@sync.service -u wispr-archiver@publish.service --no-pager
```

Sync runs every half hour. Both a successful sync and a partial or failed sync trigger a separate publication, which can copy versions already saved. Reconcile runs on Sundays at 03:15 UTC. The inactivity monitor runs every 15 minutes and alerts when the last completed ingest is missing, malformed or at least three hours old. All three timers are persistent. Confirm fresh completion and publication timestamps after the first cycle. Per-meeting failures have their own alerts.

A specialized alert is associated with its systemd invocation. `OnFailure` sends a generic unit alert when the specialized one did not cover the failure. Use `journalctl -u UNIT` for diagnostics. For maintenance or migration, disable the schedule on the old host and let active jobs finish before starting another worker.

```bash
sudo systemctl disable --now wispr-archiver-{sync,reconcile,deadman}.timer
systemctl list-units --state=activating,active 'wispr-archiver@*.service' 'wispr-archiver-notify@*.service'
```

## Google Drive

Use the same Google account and OAuth client for all access to one archive, including recovery. The `drive.file` scope allows rclone to see files created through that client. With rclone's built-in client this can include files created by other rclone jobs using the same account. A different client may create a second folder with the same name.

| rclone prompt | Answer |
| --- | --- |
| New remote / name / type | `n`, `wispr-gdrive`, `drive` |
| Client ID and secret | Press Enter for each to use the built-in client, or supply your own client consistently |
| Scope | Select `drive.file` by its description |
| Service account file / advanced configuration | Leave the file empty and select `n` |
| Browser authentication | `y` on a desktop, `n` on a headless server |
| Shared Drive / keep remote / exit | `n`, `y`, `q` |

Use `rclone listremotes` as the worker's user to check the remote name. The default destination is `wispr-gdrive:Wispr Archive`. Override it with `rclone_remote` in TOML or the command's `--remote` option. The worker runs checksum-based copies of snapshots first (`--immutable`), then of `meetings/`. Each Drive folder belongs to one archive lineage. Snapshots include their original archive timestamp, so a second backfill into a new local archive may conflict with existing snapshots.

Keep `rclone.conf` at mode 0600. The configuration dialogue and diagnostic commands can display credentials. If exposed, revoke rclone's access in the Google account and reconnect the remote as the service user. The optional `rclone_config` setting selects a configuration file. [Headless rclone setup](https://rclone.org/remote_setup/)

| Problem code | Operator action |
| --- | --- |
| `archive_missing` | Check the mounted disk, archive path and `meetings/` directory. |
| `remote_not_configured` | Check the remote name, rclone configuration path and service user. |
| `drive_auth` | Run `rclone config reconnect wispr-gdrive:` as the rclone configuration owner. |
| `drive_quota` | Retry later. For recurring quota errors, review API quota and client configuration. |
| `snapshots:rclone_exit_6` | Compare the conflicting snapshots privately. Keep both original copies while checking archive lineage. |
| `rclone_missing` / `rclone_timeout` | Check `rclone_binary` and the default 30-minute `publish_timeout_seconds`. |
| `unexpected_deletes` | Stop the timers and inspect the Drive trash and rclone behavior before resuming. |
| Other `rclone_exit_N` | Repeat the command manually. Use a private terminal for rclone diagnostics, which may print meeting filenames. |
| `missing` / `differ` / `errors` / `unmatched` | Run `publish`, then `verify-remote`. Investigate persistent snapshot conflicts or unreadable files with a private `rclone check --one-way`. |

## Restore from Drive

Stop the schedule and wait for any active job to finish. Use the same Drive remote and OAuth client as the original publisher. Restore into a **new empty directory** owned by the archive user. This also works as a recovery drill without changing the configured archive.

On macOS, use the `wa` helper from the checkout.

```sh
restore_dir=$(mktemp -d)
rclone copy "wispr-gdrive:Wispr Archive" "$restore_dir" --checksum
wa verify --dest "$restore_dir"
wa rebuild-state --dest "$restore_dir" --state "$restore_dir/recovered.db"
```

On Ubuntu, allocate the recovery directory and copy as the service user.

```sh
restore_dir=$(sudo -u wispr-archiver -H mktemp -d /var/lib/wispr-archiver/restore.XXXXXXXX)
sudo -u wispr-archiver -H sh -c 'umask 077; exec rclone copy "wispr-gdrive:Wispr Archive" "$1" --checksum' sh "$restore_dir"
wa verify --dest "$restore_dir"
wa rebuild-state --dest "$restore_dir" --state "$restore_dir/recovered.db"
```

Check record and snapshot counts. For a drill, leave the active configuration in place. For actual recovery, move the rebuilt state outside the recovered archive and point `archive_dir` and `state_path` at the recovered files. Verify the Drive copy against this local archive before resuming the schedule. The first sync after a rebuild lists the history again. A rebuild retains an existing database as a timestamped backup. To migrate to another host, sign in separately on the new host after stopping the old schedule.

## Sign in again

Exit 2 indicates that Wispr requires a new login. Stop the schedule and wait for active jobs. Run `wa login` on macOS, or reconnect the SSH tunnel and run `wa login --no-browser` on Ubuntu. Complete the browser sign-in, check the exit code, then re-enable the schedule.

## Response format changes

Exit 3 indicates an unexpected Wispr response. The alert identifies affected meetings when the error is isolated to individual records. As the operator, run `wa probe --out PATH` with a private output path outside the checkout. Review the structural report before submitting an issue. Exit 1 covers interrupted or partial work, storage errors and lock contention. Use `wa status` and the run summary for details.
