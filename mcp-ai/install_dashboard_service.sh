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

echo "Installing dashboard Python dependencies into /opt/mcp-rhel-manager/venv"
if [ -f /opt/mcp-rhel-manager/requirements.txt ]; then
  echo "Installing from requirements.txt as user mcp-ai"
  if sudo -u mcp-ai /opt/mcp-rhel-manager/venv/bin/python -m pip install -r /opt/mcp-rhel-manager/requirements.txt; then
    echo "Python dependencies installed successfully."
  else
    echo "Warning: pip install from requirements.txt failed." >&2
  fi
else
  echo "requirements.txt not found; installing Flask only as a minimal dependency"
  if sudo -u mcp-ai /opt/mcp-rhel-manager/venv/bin/python -m pip install flask; then
    echo "Flask installed successfully."
  else
    echo "Warning: pip install flask failed." >&2
  fi
fi
sudo systemctl enable --now mcp-ai-dashboard.service
sudo systemctl status --no-pager mcp-ai-dashboard.service || true

echo "Restarting mcp-ai-dashboard.service to pick up installed packages (if any)"
sudo systemctl restart mcp-ai-dashboard.service || true

echo "Recent mcp-ai-dashboard.service logs:"
sudo journalctl -u mcp-ai-dashboard.service -n 200 -l || true

echo "Dashboard service installed, dependencies attempted, and logs shown."
