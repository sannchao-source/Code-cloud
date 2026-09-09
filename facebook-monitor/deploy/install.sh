#!/usr/bin/env bash
# Install fbmonitor on an always-on Linux box (the NUC).
#
# Creates a virtualenv, installs dependencies, and sets up a systemd timer
# that runs the monitor every 15 minutes. Safe to re-run: it updates an
# existing install rather than duplicating it.
#
#   sudo ./deploy/install.sh
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$USER}"
VENV="$APP_DIR/.venv"

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo -- systemd units are installed system-wide" >&2
    exit 1
fi

echo "==> installing into $APP_DIR for user $SERVICE_USER"

if ! command -v python3 >/dev/null; then
    echo "python3 not found; install it first" >&2
    exit 1
fi

# Debian/Ubuntu split venv out of the base python package.
if ! python3 -c "import venv" 2>/dev/null; then
    echo "python3-venv missing; run: apt install python3-venv" >&2
    exit 1
fi

sudo -u "$SERVICE_USER" python3 -m venv "$VENV"
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "==> dependencies installed"

for required in accounts.yaml .env; do
    if [[ ! -f "$APP_DIR/$required" ]]; then
        echo "!! $required is missing -- copy $required.example and fill it in" >&2
    fi
done

# .env holds non-expiring Page tokens; nobody but the service user needs it.
if [[ -f "$APP_DIR/.env" ]]; then
    chown "$SERVICE_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "==> locked down .env (0600, owned by $SERVICE_USER)"
fi

sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@USER@|$SERVICE_USER|g" \
    "$APP_DIR/deploy/fbmonitor.service" > /etc/systemd/system/fbmonitor.service
cp "$APP_DIR/deploy/fbmonitor.timer" /etc/systemd/system/fbmonitor.timer

systemctl daemon-reload
systemctl enable --now fbmonitor.timer
echo "==> timer enabled; first run within 15 minutes"
echo
echo "Check it:      systemctl list-timers fbmonitor.timer"
echo "Run it now:    sudo systemctl start fbmonitor.service"
echo "Read the log:  journalctl -u fbmonitor.service -n 50 --no-pager"
echo "Latest digest: cat $APP_DIR/digest.txt"
