#!/usr/bin/env bash
# Install a reviewed checkout on Ubuntu. Run as root; activate timers after commissioning.
set -euo pipefail
umask 022
cd -- "$(dirname -- "$0")/.."
if (( EUID != 0 )); then echo 'Run install.sh as root' >&2; exit 1; fi
for tool in uv rclone systemctl systemd-analyze flock runuser; do
    command -v "$tool" >/dev/null || { echo "Missing executable: $tool" >&2; exit 1; }
done

svc=wispr-archiver
app=/opt/$svc
data=/var/lib/$svc
conf=/etc/$svc
units=/etc/systemd/system
install -d -m 0755 "$app" "$app/releases"
exec 9>"$app/install.lock"
flock 9

getent group "$svc" >/dev/null || groupadd --system "$svc"
if ! id "$svc" >/dev/null 2>&1; then
    useradd --system --gid "$svc" --home-dir "$data" --no-create-home --shell /usr/sbin/nologin "$svc"
fi
if [[ $(id -gn "$svc") != "$svc" || $(getent passwd "$svc" | cut -d: -f6,7) != "$data:/usr/sbin/nologin" ]]; then
    echo "Existing account $svc has unexpected group, home or shell" >&2
    exit 1
fi
install -d -o "$svc" -g "$svc" -m 0750 "$data"
runuser -u "$svc" -- install -d -m 0750 "$data/archive"
runuser -u "$svc" -- install -d -m 0700 "$data/.config" "$data/.config/$svc" "$data/.config/rclone"
install -d -m 0750 -o root -g "$svc" "$conf"
if [[ -L "$conf/env" || ( -e "$conf/env" && ! -f "$conf/env" ) ]]; then
    echo "Expected a regular file at $conf/env" >&2
    exit 1
fi
if [[ ! -e "$conf/env" ]]; then
    install -m 0600 -o root -g root deploy/env.example "$conf/env"
fi
chown root:root "$conf/env"
chmod 0600 "$conf/env"

# Build in the final location: venv scripts contain absolute interpreter paths.
digest=$(
    { printf '%s\0' pyproject.toml README.md LICENSE uv.lock .python-version; find src -type f -name '*.py' -print0; } |
        sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
)
release="$app/releases/$digest"
if [[ ! -f "$release/.ready" ]]; then
    export UV_PYTHON_INSTALL_DIR="$app/python"
    uv python install --no-bin 3.12
    UV_PROJECT_ENVIRONMENT="$release" uv sync --locked --no-dev --no-editable --managed-python --python 3.12 --no-cache
    runuser -u "$svc" -- "$release/bin/wispr-archiver" --version
    touch "$release/.ready"
fi

install -m 0644 deploy/systemd/*.service deploy/systemd/*.timer "$units/"
install -Dm 0644 deploy/systemd/wispr-archiver@sync.service.d/publish.conf \
    "$units/wispr-archiver@sync.service.d/publish.conf"
systemd-analyze verify wispr-archiver-{sync,reconcile,deadman}.timer \
    wispr-archiver@{sync,reconcile,publish,deadman-check}.service wispr-archiver-notify@sync.service
ln -sfnT "$release" "$app/.next"
mv -Tf "$app/.next" "$app/current"
systemctl daemon-reload
echo 'Installation complete. Follow docs/RUNBOOK.md for credentials and timer activation.'
