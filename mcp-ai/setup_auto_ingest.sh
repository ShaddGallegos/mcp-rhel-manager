#!/bin/bash
# Setup script to add auto-ingest cron job

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$SCRIPT_DIR"

echo "═══════════════════════════════════════════════════════════════════════════"
echo "HAL Auto-Ingest Cron Setup"
echo "═══════════════════════════════════════════════════════════════════════════"
echo ""

# Check if already in crontab
if crontab -l 2>/dev/null | grep -q "auto_ingest_training.py"; then
    echo "✓ Auto-ingest cron job already configured"
    echo ""
    echo "Current crontab entry:"
    crontab -l | grep "auto_ingest_training.py"
    echo ""
    read -p "Would you like to update it? (y/n) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "No changes made."
        exit 0
    fi
fi

echo ""
echo "Scheduling options:"
echo "  1) Daily at 2 AM (recommended)"
echo "  2) Every 6 hours"
echo "  3) Every hour"
echo "  4) Custom schedule"
echo ""
read -p "Select option (1-4): " choice

case $choice in
    1)
        schedule="0 2 * * *"
        desc="Daily at 2 AM"
        ;;
    2)
        schedule="0 */6 * * *"
        desc="Every 6 hours"
        ;;
    3)
        schedule="0 * * * *"
        desc="Every hour"
        ;;
    4)
        read -p "Enter cron schedule (e.g., '0 2 * * *'): " schedule
        desc="Custom: $schedule"
        ;;
    *)
        echo "Invalid option"
        exit 1
        ;;
esac

echo ""
echo "Setting up cron job for: $desc"
echo ""

# Create the cron job entry
CRON_ENTRY="$schedule cd $REPO_ROOT && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py >> ~/.mcp-ai/auto_ingest.log 2>&1"

# Get current crontab (or empty if none)
(crontab -l 2>/dev/null || echo "") | grep -v "auto_ingest_training.py" > /tmp/crontab_new.txt || true

# Add new entry
echo "$CRON_ENTRY" >> /tmp/crontab_new.txt

# Install new crontab
crontab /tmp/crontab_new.txt
rm /tmp/crontab_new.txt

echo "✓ Cron job installed successfully"
echo ""
echo "Note: project uses .venv (isolated from system pip)."
echo "aider-chat is optional — install separately:"
echo "  pip3 install --upgrade aider-chat && pip3 install --upgrade 'filelock>=3.24.2'"
echo "  (aider-chat pins filelock==3.20.3; the 2nd command restores virtualenv/tox compatibility)"
echo ""
echo "Verify installation:"
crontab -l | grep "auto_ingest_training.py"

echo ""
echo "Log file: ~/.mcp-ai/auto_ingest.log"
echo ""
echo "To view logs: tail -f ~/.mcp-ai/auto_ingest.log"
echo "To run manually: HAL --auto-ingest"
echo "To check status: HAL --ingest-status"
echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
