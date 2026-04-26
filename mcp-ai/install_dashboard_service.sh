#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_SRC="$SRC_DIR/mcp-ai-dashboard.service"
UNIT_DEST="/etc/systemd/system/mcp-ai-dashboard.service"

if [ ! -f "$UNIT_SRC" ]; then
  echo "Unit file not found: $UNIT_SRC" >&2
  exit 2
fi

echo "Installing dashboard systemd unit to $UNIT_DEST"
sudo cp "$UNIT_SRC" "$UNIT_DEST"
sudo chmod 644 "$UNIT_DEST"
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-ai-dashboard.service
sudo systemctl status --no-pager mcp-ai-dashboard.service || true

echo "Dashboard service installed and started (if systemctl allowed)."
