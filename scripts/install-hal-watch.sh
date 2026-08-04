#!/usr/bin/env bash
set -euo pipefail

# Installer for hal-watch.service. Copies service unit to /etc/systemd/system
# and enables+starts it. Requires sudo.

# Default install location is /opt/mcp-rhel-manager when present
if [[ -n "${1:-}" && "$1" != "" ]]; then
  REPO_ROOT="$1"
else
  if [[ -d "/opt/mcp-rhel-manager" ]]; then
    REPO_ROOT="/opt/mcp-rhel-manager"
  else
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  fi
fi

UNIT_SRC="$REPO_ROOT/systemd/hal-watch.service"

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "Unit source not found: $UNIT_SRC" >&2
  exit 1
fi

TMP=$(mktemp)
sed "s|<REPO_ROOT>|$REPO_ROOT|g" "$UNIT_SRC" > "$TMP"

echo "Installing hal-watch.service to /etc/systemd/system/ (requires sudo)"
sudo cp "$TMP" /etc/systemd/system/hal-watch.service
rm -f "$TMP"

sudo systemctl daemon-reload
sudo systemctl enable --now hal-watch.service

echo "hal-watch.service installed and started"
