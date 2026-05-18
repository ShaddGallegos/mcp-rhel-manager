#!/usr/bin/env bash
# Load variables from ~/.ansble/conf/env.yml into current shell.
# Usage: eval "$(scripts/load_ansble_env.sh)"

PY=$(command -v python3 || command -v python)
if [[ -z "$PY" ]]; then
  echo "# python not found; cannot load env.yml" >&2
  exit 0
fi

"$PY" "$(dirname "${BASH_SOURCE[0]}")/load_ansble_env.py"
