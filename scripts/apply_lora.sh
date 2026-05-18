#!/usr/bin/env bash
set -euo pipefail

# Install a LoRA adapter archive into a model's adapters/ directory.
# Usage: scripts/apply_lora.sh <adapter-archive> <model-name> [dest-dir]

ADAPTER=${1:-}
MODEL=${2:-}
DEST=${3:-/var/lib/mcp-llms}

if [ -z "$ADAPTER" ] || [ -z "$MODEL" ]; then
  echo "Usage: $0 <adapter-archive> <model-name> [dest-dir]" >&2
  exit 2
fi

MODEL_DIR="$DEST/$MODEL"
if [ ! -d "$MODEL_DIR" ]; then
  echo "Model directory does not exist: $MODEL_DIR" >&2
  exit 3
fi

ADAPTER_NAME=$(basename "$ADAPTER")
BASE_NAME=${ADAPTER_NAME%.*}
DEST_DIR="$MODEL_DIR/adapters/$BASE_NAME"

mkdir -p "$(dirname "$DEST_DIR")" 2>/dev/null || sudo mkdir -p "$(dirname "$DEST_DIR")"
if tar -tf "$ADAPTER" >/dev/null 2>&1; then
  mkdir -p "$DEST_DIR"
  tar -C "$DEST_DIR" -xvf "$ADAPTER"
elif unzip -t "$ADAPTER" >/dev/null 2>&1; then
  mkdir -p "$DEST_DIR"
  unzip -d "$DEST_DIR" "$ADAPTER"
else
  echo "Adapter archive must be tar or zip" >&2
  exit 4
fi

echo "Adapter installed at: $DEST_DIR"
