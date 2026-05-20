#!/usr/bin/env bash
set -euo pipefail

usage(){
  cat <<EOF
Usage: $0 --dashboard PATH_TO_JSON --url GRAFANA_URL --api-key API_KEY

Imports a Grafana dashboard JSON into a Grafana instance using the HTTP API.
Requires an API key with permission to create dashboards.
EOF
}

DASH=""
URL="http://localhost:3000"
API_KEY="${GRAFANA_API_KEY:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dashboard) DASH="$2"; shift 2;;
    --url) URL="$2"; shift 2;;
    --api-key) API_KEY="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 2;;
  esac
done

if [[ -z "$DASH" || ! -f "$DASH" ]]; then
  echo "Dashboard JSON file required" >&2; usage; exit 2
fi
if [[ -z "$API_KEY" ]]; then
  echo "GRAFANA API key required (set env GRAFANA_API_KEY or use --api-key)" >&2; exit 2
fi

json=$(jq -c . "$DASH")
payload=$(jq -n --argjson dash "$json" '{dashboard: $dash, overwrite: true}')

resp=$(curl -sS -X POST "$URL/api/dashboards/db" -H "Content-Type: application/json" -H "Authorization: Bearer $API_KEY" -d "$payload")
echo "$resp"
