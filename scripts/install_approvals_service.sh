#!/usr/bin/env bash
set -euo pipefail

# Install mcp-ai approvals systemd unit (local service)
UNIT_SRC="$(dirname "$0")/../packaging/systemd/mcp-ai-approvals.service"
UNIT_DST="/etc/systemd/system/mcp-ai-approvals.service"

if [ ! -f "$UNIT_SRC" ]; then
  echo "Unit template not found: $UNIT_SRC" >&2
  exit 1
fi

echo "Installing approvals unit to $UNIT_DST"
sudo install -m 644 "$UNIT_SRC" "$UNIT_DST"
sudo systemctl daemon-reload
echo "Enabling and starting approvals service"
sudo systemctl enable --now mcp-ai-approvals.service || true
echo "Done. Check 'systemctl status mcp-ai-approvals.service' for details."
