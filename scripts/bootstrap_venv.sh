#!/usr/bin/env bash
set -euo pipefail

PROG=$(basename "$0")
usage() {
  echo "Usage: $PROG [--python PYTHON] [--venv VENV_DIR] [--no-install]"
  exit 1
}

PYTHON=python3
VENV_DIR=${VENV_DIR:-.venv}
NO_INSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --python)
      PYTHON="$2"; shift 2;;
    --venv)
      VENV_DIR="$2"; shift 2;;
    --no-install)
      NO_INSTALL=1; shift ;;
    -h|--help)
      usage ;;
    *)
      echo "Unknown arg: $1"; usage ;;
  esac
done

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtualenv in $VENV_DIR with $PYTHON"
  $PYTHON -m venv "$VENV_DIR"
else
  echo "Virtualenv already exists at $VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel

if [ "$NO_INSTALL" -ne 1 ]; then
  if [ -f requirements.txt ]; then
    echo "Installing requirements.txt..."
    "$VENV_DIR/bin/pip" install -r requirements.txt
  fi
  if [ -f dev-requirements.txt ]; then
    echo "Installing dev-requirements.txt..."
    "$VENV_DIR/bin/pip" install -r dev-requirements.txt
  fi
else
  echo "Skipping pip installs (--no-install)"
fi

echo "Bootstrap complete. Activate with: source $VENV_DIR/bin/activate"
