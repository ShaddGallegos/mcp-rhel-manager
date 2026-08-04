#!/usr/bin/env bash
set -euo pipefail
# run_health_and_fix_once.sh
# Run a one-shot pass of the watcher (attempt safe fixes) and save a HAL doctor report.

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON=${PYTHON:-/usr/bin/python3}

echo "Running one-shot system watcher (safe fixes, SMART checks)..."
if [[ $EUID -ne 0 ]]; then
  echo "Note: running without root; some remediation (service restarts, smartctl) may require sudo/root."
  sudo "$PYTHON" "$BASE_DIR/scripts/watch_system_and_fix.py" --once --check-smart || true
else
  "$PYTHON" "$BASE_DIR/scripts/watch_system_and_fix.py" --once --check-smart || true
fi

echo "Generating HAL doctor report (human-readable)..."
"$PYTHON" "$BASE_DIR/scripts/hal.py" --doctor || true

echo "Done. Look for reports in ~/Documents/reports/ or ~/.mcp-ai/reports/ and logs in $HOME/.mcp-ai/reports/"

exit 0
