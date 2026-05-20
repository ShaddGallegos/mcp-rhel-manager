#!/usr/bin/env bash
# Install the mcp-ai git_watcher as a systemd --user service
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
TEMPLATE="$REPO_ROOT/mcp-ai/systemd/mcp-ai-git-watcher.service.tpl"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/mcp-ai-git-watcher.service"

VENV_PY="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  VENV_PY="$(command -v python3 || command -v python)"
fi

mkdir -p "$UNIT_DIR"
sed "s|@REPO_ROOT@|$REPO_ROOT|g; s|@VENV_PY@|$VENV_PY|g" "$TEMPLATE" > "$UNIT_FILE"
echo "Installed unit to $UNIT_FILE"

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "Enabling and starting mcp-ai git_watcher service..."
systemctl --user enable --now mcp-ai-git-watcher.service

echo "Service status:"
systemctl --user status --no-pager mcp-ai-git-watcher.service || true

echo "To uninstall: systemctl --user disable --now mcp-ai-git-watcher.service && rm -f $UNIT_FILE && systemctl --user daemon-reload"
