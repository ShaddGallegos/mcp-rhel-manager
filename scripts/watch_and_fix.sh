#!/usr/bin/env bash
set -euo pipefail

# watch_and_fix.sh
# Watch directories named 'git' (case-insensitive) under WATCH_BASES and
# run fix_code.sh when relevant files change. Provides a debounce per-repo.

WATCH_BASES=${WATCH_BASES:-"$PWD /home"}
DEBOUNCE=${DEBOUNCE:-3}
FIX_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fix_code.sh"

if ! command -v inotifywait >/dev/null 2>&1; then
  echo "inotifywait not found. Install inotify-tools." >&2
  exit 1
fi

declare -A last_run

rescan() {
  # return list of directories named 'git'
  local base
  for base in $WATCH_BASES; do
    find "$base" -type d -iname 'git' -print0 2>/dev/null || true
  done
}

echo "Starting watch_and_fix (WATCH_BASES='$WATCH_BASES')"

while true; do
  # collect watch dirs
  mapfile -t watch_dirs < <(rescan | xargs -0 -n1 echo 2>/dev/null || true)
  if [[ ${#watch_dirs[@]} -eq 0 ]]; then
    sleep 10
    continue
  fi

  # start inotifywait on all found dirs
  echo "Watching: ${watch_dirs[*]}"
  inotifywait -m -r -e modify,create,delete,move --format '%w%f' "${watch_dirs[@]}" | while read -r path; do
    # only react to relevant file extensions
    case "${path,,}" in
      *.py|*.sh|*.bash|*.yml|*.yaml)
        # find nearest ancestor directory named 'git' (case-insensitive)
        dir="$path"
        while [[ "$dir" != "/" && -n "$dir" ]]; do
          b=$(basename "$dir")
          if [[ "${b,,}" == "git" ]]; then
            repo_dir="$dir"
            break
          fi
          dir=$(dirname "$dir")
        done
        if [[ -z "${repo_dir:-}" ]]; then
          continue
        fi
        now=$(date +%s)
        last=${last_run["$repo_dir"]:-0}
        if (( now - last < DEBOUNCE )); then
          continue
        fi
        last_run["$repo_dir"]=$now
        echo "Change detected in $repo_dir -> running fixes"
        ("$FIX_SCRIPT" "$repo_dir") &
        ;;
      *)
        # ignore
        ;;
    esac
  done
  # if inotifywait exits, loop and rescan
  echo "inotifywait exited; rescanning"
  sleep 2
done
