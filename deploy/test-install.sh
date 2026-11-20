#!/usr/bin/env bash
# CI only: run as root on a fresh disposable Ubuntu runner with systemd.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-false} == true && $EUID -eq 0 ]] || exit 1
cd -- "$(dirname -- "$0")/.."
svc=wispr-archiver
base=/var/lib/$svc
app=/opt/$svc/current/bin/wispr-archiver
config=/etc/$svc/env
[[ ! -e $config && ! -e $base ]] || exit 1

bash deploy/install.sh
runuser -u "$svc" -- "$app" --version
echo '# CI marker' >> "$config"
# An existing env prepared by an operator may have the wrong owner/mode.
chown "$svc:$svc" "$config"
chmod 0640 "$config"
runuser -u "$svc" -- touch "$base/archive/ci-marker"
release=$(readlink -f /opt/$svc/current)
bash deploy/install.sh
[[ $(readlink -f /opt/$svc/current) == "$release" ]]
[[ $(stat -c '%a:%U:%G' "$config") == 600:root:root ]]
runuser -u "$svc" -- test -x /etc/wispr-archiver
if runuser -u "$svc" -- test -r "$config"; then exit 1; fi
[[ $(stat -c '%a:%U' "$base") == "750:$svc" ]]
[[ $(stat -c '%a:%U' "$base/archive") == "750:$svc" ]]
grep -Fxq '# CI marker' "$config"
[[ -e $base/archive/ci-marker ]]
systemd-analyze verify wispr-archiver-{sync,reconcile,deadman}.timer \
    wispr-archiver@{sync,reconcile,publish,deadman-check}.service wispr-archiver-notify@sync.service
for timer in sync reconcile deadman; do
    if systemctl is-enabled --quiet "wispr-archiver-$timer.timer"; then exit 1; fi
done
systemctl show -p OnSuccess --value wispr-archiver@sync.service | grep -Fwq wispr-archiver@publish.service
for unit in wispr-archiver-notify@sync.service wispr-archiver@publish.service; do
    systemctl show -p OnFailure --value wispr-archiver@sync.service | grep -Fwq "$unit"
done
# Exercise the installed unit with a synthetic, completed archive run. No external calls.
runuser -u "$svc" -- /opt/$svc/current/bin/python - <<'PY'
from datetime import UTC, datetime
from pathlib import Path
from wispr_archiver.state import State

state = State.open(Path("/var/lib/wispr-archiver/state.db"))
state.set_meta("last_completed_at", datetime.now(UTC).isoformat())
state.close()
PY
if ! systemctl start wispr-archiver@deadman-check.service; then
    journalctl -b -u wispr-archiver@deadman-check.service --no-pager -n 35
    exit 1
fi

# A failed sync with a delivered domain alert must still trigger a local-only publication.
# The synthetic failing sync writes the same state marker the real engine sets.
drop=/run/systemd/system/wispr-archiver@sync.service.d
install -d "$drop"
cat > "$drop/ci.conf" <<'UNIT'
[Service]
PrivateNetwork=yes
ExecStart=
ExecStart=/opt/wispr-archiver/current/bin/python /var/lib/wispr-archiver/ci-failure.py
UNIT
cat > "$base/ci-failure.py" <<'PYTHON'
import os
from datetime import UTC, datetime
from pathlib import Path
from wispr_archiver.state import State

state = State.open(Path("/var/lib/wispr-archiver/state.db"))
invocation = os.environ["INVOCATION_ID"]
state.start_run(invocation, "sync", datetime.now(UTC).isoformat())
state.mark_run_alerted(invocation)
state.close()
raise SystemExit(1)
PYTHON
runuser -u "$svc" -- mkdir -p "$base/archive/meetings"
runuser -u "$svc" -- touch "$base/archive/meetings/synthetic.json"
echo 'WISPR_ARCHIVER_RCLONE_REMOTE=:local:/var/lib/wispr-archiver/ci-replica' >> "$config"
systemctl daemon-reload
if systemctl start wispr-archiver@sync.service; then exit 1; fi
for _ in {1..30}; do
    if [[ -e "$base/ci-replica/meetings/synthetic.json" ]] &&
        journalctl -b -u wispr-archiver@publish.service --no-pager -o cat | grep -F '"run_finished"' >/dev/null &&
        journalctl -b -u wispr-archiver-notify@sync.service --no-pager -o cat | grep -F '"suppressed": true' >/dev/null; then
        # Check the real publish executable got the same invocation ID as journald.
        journalctl -b -u wispr-archiver@publish.service -o json | python3 -c '
import json, sys
for line in sys.stdin:
    record = json.loads(line)
    try:
        event = json.loads(record["MESSAGE"])
    except (ValueError, KeyError):
        continue
    if event.get("event") == "run_finished":
        assert event["run_id"] == record["_SYSTEMD_INVOCATION_ID"] and event["exit_code"] == 0
        break
else:
    sys.exit(1)
'
        exit 0
    fi
    sleep 1
done
journalctl -b -u wispr-archiver-notify@sync.service -u wispr-archiver@publish.service --no-pager -n 30
exit 1
