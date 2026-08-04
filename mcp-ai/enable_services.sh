#!/usr/bin/env bash
# Enable and start MCP services. Run as root (or with sudo).
# Usage: enable_services.sh [--dry-run]

DRY=0
if [ "$1" == "--dry-run" ]; then DRY=1; fi
SERVICES=(mcp-bridge.service mcp-sentinel.service mcp-ai-collector.service)
for s in "${SERVICES[@]}"; do
  echo "Will enable & start: $s"
  if [ $DRY -eq 0 ]; then
    systemctl daemon-reload
    systemctl enable --now "$s" || echo "Failed to enable $s"
    systemctl status --no-pager "$s" || true
  fi
done
