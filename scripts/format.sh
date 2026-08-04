#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_BIN="./.precommit-venv/bin"
EXCLUDE='(^|/)(\.venv|\.precommit-venv|\.git|\.cache|external|node_modules)(/|$$)'

echo "Running formatter: shfmt (shell), black (python), ruff (python), isort"

# Shell formatting (only top-level scripts)
if command -v shfmt >/dev/null 2>&1; then
  find "$SCRIPT_DIR" -maxdepth 1 -type f -name '*.sh' -print0 | xargs -0 shfmt -w -i 2 -s || true
else
  echo "shfmt not available; skipping shell formatting"
fi

# Python formatting using tools from the local pre-commit venv when available
if [ -x "$VENV_BIN/black" ]; then
  "$VENV_BIN/black" . --exclude "$EXCLUDE" || true
else
  if command -v black >/dev/null 2>&1; then
    black . --exclude "$EXCLUDE" || true
  else
    echo "black not available; skipping Python formatting"
  fi
fi

if [ -x "$VENV_BIN/ruff" ]; then
  "$VENV_BIN/ruff" check --fix . || true
else
  if command -v ruff >/dev/null 2>&1; then
    ruff check --fix . || true
  else
    echo "ruff not available; skipping ruff fixes"
  fi
fi

if [ -x "$VENV_BIN/isort" ]; then
  "$VENV_BIN/isort" . --skip "$EXCLUDE" || true
else
  if command -v isort >/dev/null 2>&1; then
    isort . --skip "$EXCLUDE" || true
  else
    echo "isort not available; skipping isort"
  fi
fi

echo "Formatting complete"
