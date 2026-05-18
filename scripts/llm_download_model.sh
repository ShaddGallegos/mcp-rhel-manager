#!/usr/bin/env bash
set -euo pipefail

# llm_download_model.sh
# Simple helper to download a model file to the host model directory.
# Usage: llm_download_model.sh <model-url> [destination-dir]

MODEL_URL=${1:?model URL required}
DEST_DIR=${2:-/var/lib/mcp-llms}

mkdir -p "$DEST_DIR"
FNAME=$(basename "$MODEL_URL")
TARGET="$DEST_DIR/$FNAME"

if [ -f "$TARGET" ]; then
  echo "Model already exists at $TARGET"
  exit 0
fi

if command -v wget >/dev/null 2>&1; then
  wget -O "$TARGET" "$MODEL_URL"
elif command -v curl >/dev/null 2>&1; then
  curl -L -o "$TARGET" "$MODEL_URL"
else
  echo "Neither wget nor curl available to download model" >&2
  exit 1
fi

echo "Downloaded model to $TARGET"
