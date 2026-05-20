#!/usr/bin/env bash
set -euo pipefail

# Healthcheck script for the MCP AI dashboard.
# Usage: DASH_URL=... DASH_SERVICE=... ./scripts/dashboard_healthcheck.sh [--dry-run]

HOST=${MCP_DASH_HOST:-127.0.0.1}
PORT=${MCP_DASH_PORT:-8080}
URL=${DASH_URL:-http://${HOST}:${PORT}/health}
SERVICE=${DASH_SERVICE:-mcp-ai-dashboard}

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

LOG=/var/log/mcp-ai-dashboard-health.log
touch "$LOG" 2>/dev/null || LOG=/tmp/mcp-ai-dashboard-health.log

timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "$(timestamp) healthcheck: checking $URL" >> "$LOG"

if curl --fail -sS --max-time 5 "$URL" >/dev/null 2>&1; then
  echo "$(timestamp) healthcheck: OK" >> "$LOG"
  exit 0
fi

echo "$(timestamp) healthcheck: FAILED to reach $URL" >> "$LOG"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "$(timestamp) healthcheck: dry-run, not restarting $SERVICE" >> "$LOG"
  exit 2
fi

echo "$(timestamp) healthcheck: attempting restart of $SERVICE" >> "$LOG"
if systemctl restart "$SERVICE"; then
  echo "$(timestamp) healthcheck: restart issued for $SERVICE" >> "$LOG"
else
  echo "$(timestamp) healthcheck: systemctl restart failed" >> "$LOG"
fi

sleep 5
if curl --fail -sS --max-time 5 "$URL" >/dev/null 2>&1; then
  echo "$(timestamp) healthcheck: recovered after restart" >> "$LOG"
  exit 0
else
  echo "$(timestamp) healthcheck: still failing after restart" >> "$LOG"
  journalctl -u "$SERVICE" -n 200 --no-pager >> "$LOG" 2>&1 || true
  exit 1
fi
