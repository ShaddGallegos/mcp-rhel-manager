#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--src DIR] [--dest DIR] [--repo-root PATH] [--token TOKEN] [--install] [--enable]

  --src DIR        Source directory containing unit templates (default: packaging/systemd)
  --dest DIR       Destination directory to place rendered units (default: packaging/systemd-rendered)
  --repo-root PATH Absolute path to repo root to replace (default: auto-detected via git)
  --token TOKEN    Placeholder token to substitute for repo root (default: @REPO_ROOT@)
  --install        Copy rendered units to /etc/systemd/system (requires sudo)
  --enable         If --install provided, also enable and start units (optional)
  --dry-run        Show files that would be rendered, do not write files or install
  -h|--help        Show this help

Example:
  $(basename "$0") --dry-run
  $(basename "$0") --dest packaging/systemd-rendered
  sudo $(basename "$0") --install --enable
EOF
  exit 1
}

SRC_DIR="packaging/systemd"
DEST_DIR="packaging/systemd-rendered"
TOKEN='@REPO_ROOT@'
INSTALL=0
ENABLE=0
DRY_RUN=0
APPLY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --src) SRC_DIR="$2"; shift 2 ;;
    --dest) DEST_DIR="$2"; shift 2 ;;
    --repo-root) REPO_ROOT="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --install) INSTALL=1; shift ;;
    --enable) ENABLE=1; shift ;;
    --apply) APPLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

# Auto-detect repo root
: "${REPO_ROOT:=}" 2>/dev/null || true
if [ -z "${REPO_ROOT:-}" ]; then
  if git rev-parse --show-toplevel >/dev/null 2>&1; then
    REPO_ROOT=$(git rev-parse --show-toplevel)
  else
    REPO_ROOT="$(pwd)"
  fi
fi

echo "Using repo root: $REPO_ROOT"
echo "Token: $TOKEN"
echo "Source: $SRC_DIR"
echo "Destination: $DEST_DIR"

if [ ! -d "$SRC_DIR" ]; then
  echo "Source directory '$SRC_DIR' does not exist. Nothing to render." >&2
  exit 0
fi

mkdir -p "$DEST_DIR"

# Escape repo root and token for use in sed
esc_repo_root=$(printf '%s' "$REPO_ROOT" | sed -e 's/[\/&|]/\\&/g')
esc_token=$(printf '%s' "$TOKEN" | sed -e 's/[\/&|]/\\&/g')

render_file() {
  local srcfile="$1"
  local relpath
  relpath=$(realpath --relative-to="$SRC_DIR" "$srcfile")
  local dstfile="$DEST_DIR/$relpath"
  mkdir -p "$(dirname "$dstfile")"

  echo "Rendering $relpath -> ${dstfile}"
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$APPLY" -eq 1 ]; then
      sed "s|$esc_token|$esc_repo_root|g" "$srcfile" | sed -n "1,30p"
    else
      sed "s|$esc_repo_root|$esc_token|g" "$srcfile" | sed -n "1,30p"
    fi
    return 0
  fi

  if [ "$APPLY" -eq 1 ]; then
    sed "s|$esc_token|$esc_repo_root|g" "$srcfile" > "$dstfile"
  else
    sed "s|$esc_repo_root|$esc_token|g" "$srcfile" > "$dstfile"
  fi
  chmod --reference="$srcfile" "$dstfile" || true
}

find "$SRC_DIR" -type f \( -name '*.service' -o -name '*.socket' -o -name '*.target' -o -name '*.timer' -o -name '*.path' \) -print0 |
  while IFS= read -r -d '' file; do
    render_file "$file"
  done

if [ "$INSTALL" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run: would copy rendered units to /etc/systemd/system"
    exit 0
  fi
  if [ "$APPLY" -eq 0 ]; then
    echo "Refusing to install tokenized files. Re-run with --apply to substitute tokens before installing." >&2
    exit 2
  fi

  if [ "$EUID" -ne 0 ]; then
    echo "--install requires root; re-run with sudo" >&2
    exit 2
  fi

  echo "Installing rendered units to /etc/systemd/system"
  find "$DEST_DIR" -type f -print0 | while IFS= read -r -d '' f; do
    echo "  Installing: $f -> /etc/systemd/system/$(basename "$f")"
    cp -a "$f" "/etc/systemd/system/$(basename "$f")"
  done
  systemctl daemon-reload || true

  if [ "$ENABLE" -eq 1 ]; then
    echo "Enabling and starting units found in $DEST_DIR"
    find "$DEST_DIR" -type f -print0 | while IFS= read -r -d '' f; do
      name=$(basename "$f")
      systemctl enable --now "$name" || true
    done
  fi
fi

echo "Rendered units written to: $DEST_DIR"
