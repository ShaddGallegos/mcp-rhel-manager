#!/bin/bash
# Install HAL Auto-Update Timer and Service
# Usage: sudo ./install-auto-update.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="hal-auto-update"

echo "=== HAL Auto-Update Installation ==="
echo "This will install daily auto-update tasks at 7 AM"
echo "Note: Python venv dependencies are isolated. aider-chat is optional — if installed"
echo "      system-wide, restore filelock with: pip3 install --upgrade 'filelock>=3.24.2'"
echo ""

if [ "$EUID" -ne 0 ]; then 
    echo "ERROR: This script must be run with sudo"
    exit 1
fi

# Check if systemd is available
if ! command -v systemctl &> /dev/null; then
    echo "ERROR: systemd is required but not found"
    exit 1
fi

# Copy systemd files
echo "Installing systemd service and timer..."
if [ ! -f "$REPO_ROOT/hal-auto-update.service" ]; then
    echo "ERROR: hal-auto-update.service not found in $REPO_ROOT"
    exit 1
fi
if [ ! -f "$REPO_ROOT/hal-auto-update.timer" ]; then
    echo "ERROR: hal-auto-update.timer not found in $REPO_ROOT"
    exit 1
fi

cp "$REPO_ROOT/hal-auto-update.service" /etc/systemd/system/
cp "$REPO_ROOT/hal-auto-update.timer" /etc/systemd/system/

chmod 644 /etc/systemd/system/hal-auto-update.service
chmod 644 /etc/systemd/system/hal-auto-update.timer

echo "  ✓ Copied service files to /etc/systemd/system/"

# Copy update script
echo "Installing auto-update script..."
if [ ! -f "$SCRIPT_DIR/hal-auto-update.sh" ]; then
    echo "ERROR: hal-auto-update.sh not found in $SCRIPT_DIR"
    exit 1
fi

cp "$SCRIPT_DIR/hal-auto-update.sh" /usr/local/bin/
chmod 755 /usr/local/bin/hal-auto-update.sh

echo "  ✓ Copied update script to /usr/local/bin/"

# Reload systemd
echo "Reloading systemd configuration..."
systemctl daemon-reload
echo "  ✓ Systemd reloaded"

# Enable timer
echo "Enabling hal-auto-update timer..."
systemctl enable $SERVICE_NAME.timer

# Start timer
echo "Starting hal-auto-update timer..."
systemctl start $SERVICE_NAME.timer

# Show status
echo ""
echo "=== Installation Complete ==="
echo ""
systemctl status $SERVICE_NAME.timer --no-pager || true

echo ""
echo "Updates will run daily at 7:00 AM (randomized +0-5 minutes)"
echo ""
echo "Useful commands:"
echo "  View timer status:        systemctl status hal-auto-update.timer"
echo "  View service status:      systemctl status hal-auto-update.service"
echo "  View update logs:         journalctl -u hal-auto-update.service -f"
echo "  View yesterday's updates: cat /var/log/hal-updates/hal-auto-update-*.log"
echo "  Manually run update:      sudo /usr/local/bin/hal-auto-update.sh"
echo "  Disable timer:            sudo systemctl disable hal-auto-update.timer"
echo ""
echo "✓ HAL auto-update service installed and enabled!"
