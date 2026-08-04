#!/usr/bin/env bash
# Helper: detect common tracked virtualenvs and suggest git commands to untrack them.
# Usage: scripts/prepare_repo_for_push.sh [--check|--apply]

set -euo pipefail
MODE=check
if [ "${1-}" = "--apply" ]; then
  MODE=apply
fi

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "$(pwd)")
cd "$ROOT"

echo "Scanning tracked files for virtualenvs and large artifacts..."
TRACKED=$(git ls-files)

default_patterns=("venv" "venv-bridge" ".venv" "venv/" "venv-bridge/" ".venv/")
found=()
for p in "${default_patterns[@]}"; do
  if echo "$TRACKED" | grep -E "^$p(/|$)" >/dev/null 2>&1; then
    found+=("$p")
  fi
done

# Also detect tracked files under those dirs
tracked_under=()
for d in venv venv-bridge .venv; do
  matches=$(echo "$TRACKED" | grep -E "^$d/" || true)
  if [ -n "$matches" ]; then
    tracked_under+=("$d")
  fi
done

if [ ${#found[@]} -eq 0 ] && [ ${#tracked_under[@]} -eq 0 ]; then
  echo "No tracked virtualenv directories detected."
  exit 0
fi

echo "Detected tracked virtualenv directories: ${tracked_under[*]:-none}"

echo
if [ "$MODE" = "check" ]; then
  echo "Recommended steps to untrack these directories (run from repo root):"
  echo
  for d in "${tracked_under[@]}"; do
    echo "  git rm -r --cached $d || true"
  done
  echo "  # Add to .gitignore if not present:"
  echo "  echo -e '\n# Virtualenvs\n.venv/\nvenv/\nvenv-bridge/' >> .gitignore"
  echo "  git add .gitignore && git commit -m \"chore: ignore virtualenv directories\""
  echo
  echo "If you want to run the commands now, re-run with --apply"
  exit 0
fi

# apply mode
for d in "${tracked_under[@]}"; do
  echo "Running: git rm -r --cached $d"
  git rm -r --cached "$d" || true
done

# ensure .gitignore has entries
grep -q "^\.venv/\|^venv/\|^venv-bridge/" .gitignore 2>/dev/null || \
  (echo -e "\n# Virtualenvs\n.venv/\nvenv/\nvenv-bridge/" >> .gitignore && echo "Updated .gitignore")

git add .gitignore
git commit -m "chore: ignore virtualenv directories" || true

echo "Done. Please verify changes and push."
