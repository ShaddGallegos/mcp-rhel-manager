#!/usr/bin/env bash
set -euo pipefail

PROG=${0##*/}
DRY_RUN=0

usage(){
  cat <<EOF
Usage: $PROG [--dry-run]

Prune podman system caches/images. Use with caution.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 2;;
  esac
done

if ! command -v podman >/dev/null 2>&1; then
  echo "podman not found; nothing to do" >&2
  exit 0
fi

echo "Podman system df:"
podman system df || true

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "DRY RUN: would run 'podman system prune -a -f'"
  exit 0
fi

echo "Pruning podman system caches and unused images..."
podman system prune -a -f || true
echo "Prune complete. Current disk usage:"
podman system df || true

exit 0
