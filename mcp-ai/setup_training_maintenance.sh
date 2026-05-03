#!/bin/bash
# Setup script to add scheduled HAL training maintenance jobs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$SCRIPT_DIR"

DAILY_SCHEDULE="30 1 * * *"
WEEKLY_SCHEDULE="45 2 * * 0"
DAILY_CMD="cd $REPO_ROOT && source .venv/bin/activate && python3 hal.py --training-maintenance >> ~/.mcp-ai/training_maintenance.log 2>&1"
WEEKLY_CMD="cd $REPO_ROOT && source .venv/bin/activate && python3 hal.py --training-maintenance-apply >> ~/.mcp-ai/training_maintenance.log 2>&1"

mkdir -p ~/.mcp-ai

echo "==========================================================================="
echo "HAL Training Maintenance Scheduler Setup"
echo "==========================================================================="
echo ""

if crontab -l 2>/dev/null | grep -q "training-maintenance"; then
    echo "Existing training maintenance cron entries found."
    echo ""
    crontab -l | grep "training-maintenance" || true
    echo ""
    read -p "Replace existing entries? (y/n) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "No changes made."
        exit 0
    fi
fi

TMP_CRON="$(mktemp)"
(crontab -l 2>/dev/null || echo "") | grep -v "training-maintenance" > "$TMP_CRON" || true

echo "$DAILY_SCHEDULE $DAILY_CMD" >> "$TMP_CRON"
echo "$WEEKLY_SCHEDULE $WEEKLY_CMD" >> "$TMP_CRON"

crontab "$TMP_CRON"
rm -f "$TMP_CRON"

echo "Installed cron entries:"
crontab -l | grep "training-maintenance" || true

echo ""
echo "Daily dry-run  : $DAILY_SCHEDULE"
echo "Weekly cleanup : $WEEKLY_SCHEDULE"
echo "Log file       : ~/.mcp-ai/training_maintenance.log"
echo ""
echo "Note: cron jobs activate the project venv (.venv) which is isolated from system pip."
echo "aider-chat is optional — install separately:"
echo "  pip3 install --upgrade aider-chat && pip3 install --upgrade 'filelock>=3.24.2'"
echo "  (aider-chat pins filelock==3.20.3 which conflicts with virtualenv>=3.24.2 and tox)"
echo ""
echo "Manual checks:"
echo "  HAL --training-maintenance"
echo "  HAL --training-maintenance-apply"
echo "═══════════════════════════════════════════════════════════════════════════"
