#!/usr/bin/env bash
set -euo pipefail

# Wrapper for reindex_embeddings.py that prevents overlapping runs via flock.
# Usage: reindex_wrapper.sh --input-dir ... --out ...

LOCKFILE="/var/lock/mcp-ai-reindex.lock"
if [[ ! -d "$(dirname "$LOCKFILE")" ]]; then
  mkdir -p "$(dirname "$LOCKFILE")" 2>/dev/null || true
fi

# If flock is present, use it; otherwise emulate with mkdir locking.
if command -v flock >/dev/null 2>&1; then
  exec flock -n "$LOCKFILE" "$(command -v python3 || echo python3)" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reindex_embeddings.py" "$@"
fi

# Fallback: create lock dir
if mkdir "$LOCKFILE.lock" 2>/dev/null; then
  CLEANUP_LOCK=1
else
  echo "Another reindex is running; exiting." >&2
  exit 0
fi

# Run reindex using the same args
trap '[[ "$CLEANUP_LOCK" == 1 ]] && rmdir "$LOCKFILE.lock" || true' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || echo python)"
# Execute the real reindex script
exec "$PY" "$SCRIPT_DIR/reindex_embeddings.py" "$@"
