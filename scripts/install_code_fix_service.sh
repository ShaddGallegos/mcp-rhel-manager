#!/usr/bin/env bash
set -euo pipefail

# install_code_fix_service.sh
# Copies the scan/fix scripts to /usr/local/bin and installs the systemd unit and timer.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "repo: $REPO_ROOT"

if [[ $EUID -ne 0 ]]; then
  echo "This installer requires root. Re-run with sudo." >&2
  exit 1
fi

cp -v "$REPO_ROOT/scripts/fix_code.sh" /usr/local/bin/fix_code.sh
cp -v "$REPO_ROOT/scripts/scan_and_fix_git_dirs.sh" /usr/local/bin/scan_and_fix_git_dirs.sh
chmod +x /usr/local/bin/fix_code.sh /usr/local/bin/scan_and_fix_git_dirs.sh

cp -v "$REPO_ROOT/packaging/systemd/mcp-code-fix.service" /etc/systemd/system/
cp -v "$REPO_ROOT/packaging/systemd/mcp-code-fix.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now mcp-code-fix.timer
echo "Installed and enabled mcp-code-fix.timer"
echo "Use: systemctl status mcp-code-fix.timer && journalctl -u mcp-code-fix.service -n 200"
