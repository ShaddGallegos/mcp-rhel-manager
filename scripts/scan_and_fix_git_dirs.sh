#!/usr/bin/env bash
set -euo pipefail

# scan_and_fix_git_dirs.sh
# Find directories named 'git' (case-insensitive) under a set of base paths
# and run fix_code.sh against them. Designed to be invoked by systemd timer
# or manually. Usage: scan_and_fix_git_dirs.sh [--dry-run] [base...]

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift || true
fi

# Default bases: current dir and /home (covers typical developer repos).
# Override with WATCH_BASES env or pass explicit base dirs as args.
if [[ -n "${WATCH_BASES:-}" ]]; then
  # split WATCH_BASES on whitespace
  read -r -a BASES <<< "$WATCH_BASES"
elif [[ $# -gt 0 ]]; then
  BASES=("$@")
else
  BASES=("$PWD" "/home")
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIX_SCRIPT="$SCRIPT_DIR/fix_code.sh"
if [[ ! -x "$FIX_SCRIPT" ]]; then
  # allow installed path
  if [[ -x "/usr/local/bin/fix_code.sh" ]]; then
    FIX_SCRIPT="/usr/local/bin/fix_code.sh"
  fi
fi

echo "scan_and_fix starting (dry-run=$DRY_RUN)" >&2
for base in "${BASES[@]}"; do
  [[ -d "$base" ]] || continue
  echo "scanning $base" >&2
  while IFS= read -r -d $'\0' dir; do
    echo "found git dir: $dir" >&2
    # Run fixes in the git directory itself
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "DRY-RUN: would run $FIX_SCRIPT on $dir" >&2
    else
      echo "running fixes on $dir" >&2
      "$FIX_SCRIPT" "$dir" || echo "fix script failed on $dir" >&2
    fi
  done < <(find "$base" -type d -iname 'git' -print0 2>/dev/null || true)
done

echo "scan_and_fix completed" >&2

exit 0
