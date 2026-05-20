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
# Prefer systemd user if available; otherwise fall back to launchd (macOS)
if command -v systemctl >/dev/null 2>&1 && systemctl --user >/dev/null 2>&1; then
  echo "Reloading systemd user daemon..."
  systemctl --user daemon-reload

  echo "Enabling and starting mcp-ai git_watcher service..."
  systemctl --user enable --now mcp-ai-git-watcher.service

  echo "Service status:"
  systemctl --user status --no-pager mcp-ai-git-watcher.service || true

  echo "To uninstall: systemctl --user disable --now mcp-ai-git-watcher.service && rm -f $UNIT_FILE && systemctl --user daemon-reload"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  PLIST_DIR="$HOME/Library/LaunchAgents"
  mkdir -p "$PLIST_DIR"
  PLIST_FILE="$PLIST_DIR/com.mcp-ai.git-watcher.plist"
  cat > "$PLIST_FILE" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mcp-ai.git-watcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_PY</string>
    <string>$REPO_ROOT/mcp-ai/cli.py</string>
    <string>git</string>
    <string>watch</string>
    <string>--roots</string>
    <string>$HOME/GIT</string>
    <string>--interval</string>
    <string>30</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>$REPO_ROOT</string>
  <key>StandardOutPath</key>
  <string>$HOME/.mcp-ai/git_watcher.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/.mcp-ai/git_watcher.err</string>
</dict>
</plist>
PLIST
  echo "Installed launchd plist to $PLIST_FILE"
  launchctl unload "$PLIST_FILE" 2>/dev/null || true
  launchctl load "$PLIST_FILE"
  echo "Loaded launchd agent. To check: launchctl list | grep com.mcp-ai.git-watcher"
  echo "To uninstall: launchctl unload $PLIST_FILE && rm -f $PLIST_FILE"
else
  echo "No supported service manager found (systemd user or launchd). Please manage $UNIT_FILE manually." >&2
fi
