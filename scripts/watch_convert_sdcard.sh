#!/usr/bin/env bash
set -euo pipefail

# Watch for a supplemental training dataset and attempt GGUF conversion when ready.
# Usage: set optional env vars `MAX_RETRIES` (0=infinite) and `SLEEP` (seconds).

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
OUTDIR="${HOME}/.ansible/.supplementaltraining/sdcard_docs"
DATASET="$OUTDIR/dataset.jsonl"
GGUF_OUT="$OUTDIR/dataset.gguf"
MAX_RETRIES="${MAX_RETRIES:-0}"
SLEEP="${SLEEP:-60}"

echo "$(date) watch_convert_sdcard.sh starting; OUTDIR=${OUTDIR}; DATASET=${DATASET}" >&2

# Activate venv if present
if [ -f "$VENV/bin/activate" ]; then
  # shellcheck source=/dev/null
  source "$VENV/bin/activate"
fi

count=0
while true; do
  if [ -f "$DATASET" ]; then
    echo "$(date) dataset found: $DATASET" >&2
    # Attempt conversion using the project's helper which detects external tools
    if python3 mcp-ai/gguf_converter.py "$DATASET" "$GGUF_OUT"; then
      if [ -f "$GGUF_OUT" ]; then
        echo "$(date) GGUF created: $GGUF_OUT" >&2
        exit 0
      fi
    fi
    echo "$(date) conversion not successful or no converter available; retrying in ${SLEEP}s" >&2
  else
    echo "$(date) dataset not found at $DATASET; waiting ${SLEEP}s" >&2
  fi

  if [ "$MAX_RETRIES" -gt 0 ]; then
    count=$((count+1))
    if [ "$count" -ge "$MAX_RETRIES" ]; then
      echo "$(date) reached MAX_RETRIES ($MAX_RETRIES); exiting" >&2
      exit 2
    fi
  fi

  sleep "$SLEEP"
done
