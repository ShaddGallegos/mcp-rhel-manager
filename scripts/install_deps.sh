#!/usr/bin/env bash
set -euo pipefail

# install_deps.sh - helper to install Python and OS-level deps for development
# Usage: ./scripts/install_deps.sh [--venv PATH] [--sys]

VENV_PATH="./.venv_dev"
INSTALL_SYS=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --venv)
      VENV_PATH="$2"; shift 2;;
    --sys)
      INSTALL_SYS=true; shift;;
    -h|--help)
      echo "Usage: $0 [--venv PATH] [--sys]"; exit 0;;
    *)
      echo "Unknown arg: $1"; exit 2;;
  esac
done

echo "Using venv: $VENV_PATH"
python3 -m venv "$VENV_PATH"
"$VENV_PATH/bin/python" -m pip install --upgrade pip setuptools wheel

if [ -f requirements.txt ]; then
  echo "Installing pip requirements..."
  "$VENV_PATH/bin/python" -m pip install --upgrade -r requirements.txt || true
fi
if [ -f dev-requirements.txt ]; then
  echo "Installing dev requirements..."
  "$VENV_PATH/bin/python" -m pip install --upgrade -r dev-requirements.txt || true
fi

echo "Reconciling HAL HTTP client stack..."
"$VENV_PATH/bin/python" -m pip install --upgrade --force-reinstall \
  "requests>=2.32.0,<3.0.0" \
  "urllib3>=2.0.0,<3.0.0" \
  "chardet>=5.0.0,<6.0.0" \
  "charset-normalizer>=3.3.0,<4.0.0" || true

if [ "$INSTALL_SYS" = true ]; then
  echo "Attempting to install OS packages from bindep.txt"
  PKGS=()
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(echo "$line" | xargs)"
    [ -z "$line" ] && continue
    # take first token before any alternate pipe
    token="${line%%|*}"
    token="$(echo "$token" | xargs)"
    PKGS+=("$token")
  done < bindep.txt

  if command -v dnf >/dev/null 2>&1; then
    echo "Using dnf to install: ${PKGS[*]}"
    sudo dnf install -y "${PKGS[@]}" || echo "dnf failed; please install packages manually"
  elif command -v apt-get >/dev/null 2>&1; then
    echo "Using apt-get to install: ${PKGS[*]}"
    sudo apt-get update && sudo apt-get install -y "${PKGS[@]}" || echo "apt-get failed; please install packages manually"
  else
    echo "No supported package manager found (dnf/apt-get). Please install: ${PKGS[*]}"
  fi
fi

echo "Done. Activate with: source $VENV_PATH/bin/activate"
