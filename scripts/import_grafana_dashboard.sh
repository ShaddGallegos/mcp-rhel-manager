#!/usr/bin/env bash
set -euo pipefail

# Load centralized ansible env (if present) so secrets come from ~/.ansible/conf/env.yml
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${ANSIBLE_ENV_PATH:-$HOME/.ansible/conf/env.yml}" ]; then
  eval "$("$SCRIPT_DIR/load_ansble_env.sh")"
fi

usage(){
  cat <<EOF
Usage: $0 --dashboard PATH_TO_JSON --url GRAFANA_URL --api-key API_KEY

Imports a Grafana dashboard JSON into a Grafana instance using the HTTP API.
Requires an API key with permission to create dashboards. Prefer setting
`GRAFANA_API_KEY` in your ansible env file (`~/.ansible/conf/env.yml`) and
loading it with `eval "$(scripts/load_ansble_env.sh)"`.
EOF
}

DASH=""
URL="http://localhost:3000"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dashboard) DASH="$2"; shift 2;;
    --url) URL="$2"; shift 2;;
    --api-key) GRAFANA_API_KEY="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 2;;
  esac
done

if [[ -z "$DASH" || ! -f "$DASH" ]]; then
  echo "Dashboard JSON file required" >&2; usage; exit 2
fi
if [[ -z "${GRAFANA_API_KEY:-}" ]]; then
  echo "GRAFANA API key required (set env GRAFANA_API_KEY in ~/.ansible/conf/env.yml or use --api-key)" >&2; exit 2
fi

json=$(jq -c . "$DASH")
payload=$(jq -n --argjson dash "$json" '{dashboard: $dash, overwrite: true}')

resp=$(curl -sS -X POST "$URL/api/dashboards/db" -H "Content-Type: application/json" -H "Authorization: Bearer $GRAFANA_API_KEY" -d "$payload")
echo "$resp"
