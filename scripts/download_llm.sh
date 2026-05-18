#!/usr/bin/env bash
set -euo pipefail

# Download an LLM archive and extract it into the host model store.
# Usage: scripts/download_llm.sh <url> <model-name> [dest-dir]

URL=${1:-}
MODEL=${2:-}
DEST=${3:-/var/lib/mcp-llms}

if [ -z "$URL" ] || [ -z "$MODEL" ]; then
  echo "Usage: $0 <url> <model-name> [dest-dir]" >&2
  exit 2
fi

mkdir -p "$DEST" 2>/dev/null || sudo mkdir -p "$DEST"

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

FNAME="$TMPDIR/$(basename "$URL")"
if command -v curl >/dev/null 2>&1; then
  curl -L -o "$FNAME" "$URL"
else
  wget -O "$FNAME" "$URL"
fi

mkdir -p "$DEST/$MODEL"
if tar -tf "$FNAME" >/dev/null 2>&1; then
  tar -C "$DEST/$MODEL" -xvf "$FNAME"
elif unzip -t "$FNAME" >/dev/null 2>&1; then
  unzip -d "$DEST/$MODEL" "$FNAME"
else
  # Not an archive, move the file
  mv "$FNAME" "$DEST/$MODEL/"
fi

echo "Model installed at: $DEST/$MODEL"
