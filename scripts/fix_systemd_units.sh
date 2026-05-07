#!/usr/bin/env bash
set -euo pipefail
# Conservative helper to repair systemd unit files that reference a missing
# developer path (/home/sgallego/mcp-rhel-manager) by either creating a
# symlink to the production path (/opt/mcp-rhel-manager) or patching unit
# files to point to the production path. Intended to be run manually with
# sudo after review.

DEFAULT_HOME_PATH="/home/sgallego/mcp-rhel-manager"
TARGET_PATH="/opt/mcp-rhel-manager"

DRY_RUN=0
DO_SYMLINK=0
DO_PATCH=0
DO_RESTART=0
LIST_ONLY=0

usage(){
  cat <<EOF
Usage: $(basename "$0") [options]
Options:
  --list           Show systemd unit files referencing the dev path
  --symlink        Create a symlink from the dev path -> production path
  --patch-units    Patch unit files replacing dev path with production path
  --restart        After patching, run systemctl daemon-reload and restart units
  --dry-run        Show actions without making changes
  -h, --help       Show this help

This script is conservative: it backups unit files before modifying them.
Run with sudo when applying changes.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list) LIST_ONLY=1; shift ;;
    --symlink) DO_SYMLINK=1; shift ;;
    --patch-units) DO_PATCH=1; shift ;;
    --restart) DO_RESTART=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown arg: $1"; usage ;;
  esac
done

search_dirs=("/etc/systemd/system" "/lib/systemd/system" "/run/systemd/system")
matches=()
for d in "${search_dirs[@]}"; do
  if [[ -d "$d" ]]; then
    while IFS= read -r -d $'\0' f; do
      matches+=("$f")
    done < <(grep -RI --binary-files=without-match -l -- "${DEFAULT_HOME_PATH}" "$d" 2>/dev/null || true)
  fi
done

if [[ ${#matches[@]} -eq 0 ]]; then
  echo "No systemd unit files found referencing ${DEFAULT_HOME_PATH}."
else
  echo "Found ${#matches[@]} unit file(s) referencing ${DEFAULT_HOME_PATH}:"
  for u in "${matches[@]}"; do
    echo "  - $u"
  done
fi

if [[ $LIST_ONLY -eq 1 ]]; then
  exit 0
fi

if [[ $DO_SYMLINK -eq 1 ]]; then
  if [[ ! -d "$TARGET_PATH" ]]; then
    echo "Target path $TARGET_PATH does not exist — aborting symlink creation." >&2
    exit 2
  fi
  if [[ -e "$DEFAULT_HOME_PATH" ]]; then
    echo "$DEFAULT_HOME_PATH already exists — not creating symlink." >&2
  else
    echo "Will create symlink: $DEFAULT_HOME_PATH -> $TARGET_PATH"
    if [[ $DRY_RUN -eq 0 ]]; then
      mkdir -p "$(dirname "$DEFAULT_HOME_PATH")"
      ln -s "$TARGET_PATH" "$DEFAULT_HOME_PATH"
      echo "Symlink created."
    else
      echo "Dry-run: symlink not created.";
    fi
  fi
fi

if [[ $DO_PATCH -eq 1 ]]; then
  if [[ ${#matches[@]} -eq 0 ]]; then
    echo "No matching unit files to patch.";
  else
    for u in "${matches[@]}"; do
      echo "Processing $u"
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "  Dry-run: would backup and replace ${DEFAULT_HOME_PATH} -> ${TARGET_PATH} in $u"
        continue
      fi
      bak="${u}.bak-$(date +%Y%m%d%H%M%S)"
      if [[ ! -f "$bak" ]]; then
        cp -p "$u" "$bak"
        echo "  Backed up to $bak"
      fi
      tmpfile="${u}.tmp"
      sed "s|${DEFAULT_HOME_PATH}|${TARGET_PATH}|g" "$u" > "$tmpfile"
      mv "$tmpfile" "$u"
      echo "  Patched $u"
    done

    if [[ $DO_RESTART -eq 1 ]]; then
      echo "Reloading systemd daemon and restarting affected units..."
      if [[ $DRY_RUN -eq 0 ]]; then
        systemctl daemon-reload
        for u in "${matches[@]}"; do
          name=$(basename "$u")
          echo "  Restarting $name"
          systemctl restart "$name" || echo "    Failed to restart $name"
        done
      else
        echo "Dry-run: would have reloaded daemon and restarted units."
      fi
    fi
  fi
fi

echo "Done. Review changes and logs."
