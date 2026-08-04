#!/usr/bin/env bash
set -euo pipefail

VENV_DIR=${VENV_DIR:-.venv}
INSTALL_IF_MISSING=0

if [ "${1:-}" = "--install" ]; then
  INSTALL_IF_MISSING=1
fi

if [ ! -x "$VENV_DIR/bin/pre-commit" ]; then
  if [ "$INSTALL_IF_MISSING" -eq 1 ]; then
    echo "pre-commit not found; installing dev requirements..."
    "$VENV_DIR/bin/pip" install -r dev-requirements.txt
  else
    echo "pre-commit not found in $VENV_DIR. Run scripts/bootstrap_venv.sh or pass --install." >&2
    exit 2
  fi
fi

echo "Installing git pre-commit hook (if not already installed)"
"$VENV_DIR/bin/pre-commit" install || true

echo "Running pre-commit against all files"
"$VENV_DIR/bin/pre-commit" run --all-files
