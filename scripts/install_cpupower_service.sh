#!/usr/bin/env bash
# Install cpupower systemd unit (optional)
set -euo pipefail

UNIT_SRC="$(dirname "$0")/../packaging/systemd/cpupower.service"
UNIT_DST="/etc/systemd/system/cpupower.service"

if [ ! -f "$UNIT_SRC" ]; then
  echo "Unit template not found: $UNIT_SRC" >&2
  exit 1
fi

echo "Installing cpupower unit to $UNIT_DST"
sudo install -m 644 "$UNIT_SRC" "$UNIT_DST"
sudo systemctl daemon-reload
echo "Enabling and starting cpupower service (may fail if cpupower binary missing)"
sudo systemctl enable --now cpupower.service || true
echo "Done. Check 'systemctl status cpupower.service' for details."
