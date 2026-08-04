#!/usr/bin/env bash
set -euo pipefail

PROG=${0##*/}
TMPDIR=${TMPDIR:-/tmp}
DEFAULT_INDEX=/var/lib/mcp-llms/models.json

usage(){
  cat <<EOF
Usage: $PROG --url URL [--name NAME] [--dest PATH] [--checksum sha256:VALUE] [--index PATH] [--owner USER:GROUP] [--no-index]

Downloads a model file idempotently into /var/lib/mcp-llms and updates the models index.
EOF
}

if [[ ${#@} -eq 0 ]]; then usage; exit 1; fi

URL=""
NAME=""
DEST=""
CHECKSUM=""
INDEX="$DEFAULT_INDEX"
OWNER="root:root"
NO_INDEX=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) URL="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    --dest) DEST="$2"; shift 2;;
    --checksum) CHECKSUM="$2"; shift 2;;
    --index) INDEX="$2"; shift 2;;
    --owner) OWNER="$2"; shift 2;;
    --no-index) NO_INDEX=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 2;;
  esac
done

if [[ -z "$URL" ]]; then echo "--url is required" >&2; usage; exit 2; fi

if [[ -z "$DEST" ]]; then
  BASENAME=$(basename "$URL")
  DEST="/var/lib/mcp-llms/$BASENAME"
fi

mkdir -p "$(dirname "$DEST")"

TMPFILE=$(mktemp "$TMPDIR/mcp-model.XXXXXX")
trap 'rm -f "$TMPFILE"' EXIT

echo "Downloading $URL -> $TMPFILE"
if command -v curl >/dev/null 2>&1; then
  curl -fSL "$URL" -o "$TMPFILE"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$TMPFILE" "$URL"
else
  echo "No downloader (curl/wget) found" >&2
  exit 3
fi

if [[ -n "$CHECKSUM" ]]; then
  if [[ "$CHECKSUM" =~ ^sha256:([0-9a-fA-F]{64})$ ]]; then
    EXPECT=${BASH_REMATCH[1]}
    ACTUAL=$(sha256sum "$TMPFILE" | awk '{print $1}')
    if [[ "$ACTUAL" != "$EXPECT" ]]; then
      echo "Checksum mismatch: expected $EXPECT got $ACTUAL" >&2
      exit 4
    fi
  else
    echo "Unsupported checksum format. Use sha256:..." >&2
    exit 4
  fi
fi

mv -f "$TMPFILE" "$DEST"
chmod 0640 "$DEST"
chown "$OWNER" "$DEST" || true

if [[ "$NO_INDEX" -eq 0 ]]; then
  if command -v /usr/local/bin/manage_models.py >/dev/null 2>&1; then
    /usr/local/bin/manage_models.py add --path "$DEST" --name "${NAME:-$(basename "$DEST")}" --index "$INDEX" || true
  else
    # fallback: update index naively
    if command -v python3 >/dev/null 2>&1; then
      python3 - <<PY
import json,os
idx='''$INDEX'''
path='''$DEST'''
os.makedirs(os.path.dirname(idx), exist_ok=True)
data={}
if os.path.exists(idx):
    try:
        data=json.load(open(idx))
    except Exception:
        data={}
data[os.path.basename(path)]={'path':path}
json.dump(data, open(idx,'w'), indent=2)
print('Updated index:', idx)
PY
    fi
  fi
fi

echo "Installed $DEST"
exit 0
