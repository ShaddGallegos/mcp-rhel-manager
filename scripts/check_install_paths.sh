#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="${1:-/opt/mcp-rhel-manager}"
VENV_DIR="${2:-$BASE_DIR/venv}"
MCP_USER="${3:-mcp}"

printf "Checking install paths and environment\n"
printf "BASE_DIR=%s\nVENV_DIR=%s\nMCP_USER=%s\n\n" "$BASE_DIR" "$VENV_DIR" "$MCP_USER"

if [[ ! -d "$BASE_DIR" ]]; then
  echo "MISSING: base dir $BASE_DIR"
else
  ls -ld "$BASE_DIR"
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "MISSING: venv python at $VENV_DIR/bin/python"
else
  "$VENV_DIR/bin/python" -V
fi

if ! id "$MCP_USER" >/dev/null 2>&1; then
  echo "MISSING: user $MCP_USER does not exist"
else
  id "$MCP_USER"
fi

exit 0
