#!/usr/bin/env bash
set -euo pipefail
# install_watch_service.sh
# Installs a conservative systemd unit (`hal-watch.service`) that runs the
# repository's `watch_system_and_fix.py` daemon at boot and enables it now.

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_PATH="/etc/systemd/system/hal-watch.service"

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root (use sudo)." >&2
  exit 1
fi

cat >"${UNIT_PATH}.tmp" <<UNIT
[Unit]
Description=HAL System Watcher (mcp-rhel-manager)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $BASE_DIR/scripts/watch_system_and_fix.py --daemon --check-smart --no-reboot
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

mv "${UNIT_PATH}.tmp" "$UNIT_PATH"
chmod 644 "$UNIT_PATH"
echo "Wrote $UNIT_PATH"

systemctl daemon-reload
systemctl enable --now hal-watch.service
echo "Enabled and started hal-watch.service. Status:" 
systemctl --no-pager status hal-watch.service || true

echo "Logs: journalctl -u hal-watch.service -n 200 --no-pager"

exit 0
