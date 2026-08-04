#!/usr/bin/env bash
set -euo pipefail
# Install and enable pre-commit hooks for this repository.
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# If there's a local venv, activate it for install convenience
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if ! command -v pre-commit >/dev/null 2>&1; then
  echo "Installing pre-commit into current Python environment..."
  python -m pip install --upgrade pre-commit
fi

echo "Installing pre-commit hooks..."
pre-commit install || true
echo "Pre-commit hooks installed. To run now: pre-commit run --all-files"
