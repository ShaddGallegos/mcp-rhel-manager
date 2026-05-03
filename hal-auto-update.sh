#!/bin/bash
# HAL Auto-Update Script: Runs daily at 7 AM
# Performs: DNF upgrade, flatpak updates, SELinux fixes, firewall updates
# No reboot required; logs all actions

set -o pipefail

LOG_DIR="/var/log/hal-updates"
LOG_FILE="${LOG_DIR}/hal-auto-update-$(date +%Y%m%d).log"
LOCK_FILE="/run/hal-auto-update.lock"

# Ensure log directory exists
mkdir -p "$LOG_DIR"
chmod 755 "$LOG_DIR"

# Function to log with timestamp
log_msg() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Function to log errors
log_err() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" | tee -a "$LOG_FILE"
}

# Prevent concurrent runs
if [ -f "$LOCK_FILE" ]; then
    log_msg "Update already in progress (lock file exists). Skipping."
    exit 0
fi

trap "rm -f '$LOCK_FILE'" EXIT
touch "$LOCK_FILE"

log_msg "=== HAL Auto-Update Started ==="
log_msg "System: $(hostname)"
log_msg "Note: Python venv deps are isolated. If aider-chat was installed system-wide,"
log_msg "      restore filelock with: pip3 install --upgrade 'filelock>=3.24.2'"

# ========== DNF Upgrade ==========
log_msg "Starting DNF system upgrade..."
if dnf upgrade -y &>> "$LOG_FILE"; then
    log_msg "✓ DNF upgrade completed successfully"
    dnf_status="OK"
else
    log_err "DNF upgrade encountered errors (see log for details)"
    dnf_status="FAILED"
fi

# ========== Flatpak Update ==========
log_msg "Starting flatpak updates..."
if command -v flatpak &> /dev/null; then
    if flatpak update -y &>> "$LOG_FILE"; then
        log_msg "✓ Flatpak updates completed successfully"
        flatpak_status="OK"
    else
        log_err "Flatpak updates encountered errors (see log for details)"
        flatpak_status="FAILED"
    fi
else
    log_msg "ℹ Flatpak not installed, skipping"
    flatpak_status="SKIPPED"
fi

# ========== SELinux Error Fixes ==========
log_msg "Checking and fixing SELinux errors..."
if command -v semanage &> /dev/null && command -v restorecon &> /dev/null; then
    # Run restorecon on common directories
    for dir in /home /opt /srv /var/log; do
        if [ -d "$dir" ]; then
            log_msg "  Running restorecon on $dir..."
            if restorecon -RF "$dir" &>> "$LOG_FILE" 2>&1; then
                :  # Silent success
            else
                log_msg "  ⓘ restorecon on $dir had issues (may be normal)"
            fi
        fi
    done
    
    # Check for SELinux denials and try to fix them
    if command -v audit2why &> /dev/null; then
        denial_count=$(ausearch -m avc -ts recent 2>/dev/null | grep -c "avc:" || echo "0")
        if [ "$denial_count" -gt 0 ]; then
            log_msg "  Found $denial_count recent SELinux denials, analyzing..."
            # Note: audit2allow requires manual review; we log but don't auto-apply
            log_msg "  ℹ SELinux denials detected. Review with: ausearch -m avc -ts recent"
        else
            log_msg "  ✓ No recent SELinux denials found"
        fi
    fi
    selinux_status="OK"
else
    log_msg "ℹ SELinux tools not available, skipping SELinux fixes"
    selinux_status="SKIPPED"
fi

# ========== Firewall Updates ==========
log_msg "Checking firewall configuration..."
if command -v firewall-cmd &> /dev/null; then
    # Verify firewall is running
    if systemctl is-active --quiet firewalld; then
        log_msg "✓ Firewall service is active"
        
        # Reload firewall configuration (applies any pending rules)
        if firewall-cmd --reload &>> "$LOG_FILE" 2>&1; then
            log_msg "✓ Firewall configuration reloaded"
        else
            log_msg "ⓘ Firewall reload had issues"
        fi
        
        # Check for failed zones or rules
        failed_zones=$(firewall-cmd --get-zones 2>/dev/null | tr ' ' '\n' | while read zone; do
            if ! firewall-cmd --zone="$zone" --list-all &> /dev/null 2>&1; then
                echo "$zone"
            fi
        done | wc -l)
        
        if [ "$failed_zones" -eq 0 ]; then
            log_msg "✓ All firewall zones healthy"
        else
            log_msg "⚠ $failed_zones firewall zones have issues"
        fi
        
        firewall_status="OK"
    else
        log_msg "⚠ Firewall service is not running"
        firewall_status="WARNING"
    fi
else
    log_msg "ℹ firewalld not installed, skipping firewall updates"
    firewall_status="SKIPPED"
fi

# ========== Summary ==========
log_msg ""
log_msg "=== Auto-Update Summary ==="
log_msg "DNF Upgrade:        $dnf_status"
log_msg "Flatpak Update:     $flatpak_status"
log_msg "SELinux Fixes:      $selinux_status"
log_msg "Firewall Updates:   $firewall_status"
log_msg "Log saved to:       $LOG_FILE"
log_msg "=== Auto-Update Completed ==="
