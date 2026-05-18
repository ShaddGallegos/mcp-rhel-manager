# HAL Auto-Update & Daily Maintenance Setup

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

This guide sets up automated daily system maintenance tasks to run at 7 AM via systemd timers.

## What Gets Automated

### Daily Tasks (7:00 AM)

- **DNF Upgrade**: `dnf upgrade -y` - Updates all system packages
- **Flatpak Updates**: `flatpak update -y` - Updates flatpak applications
- **SELinux Repairs**: Runs `restorecon` on home directories, checks for denials
- **Firewall Updates**: Reloads firewall configuration, validates zones
- **No Reboot Required**: All updates installed without requiring system restart

### Auto-Healing (Also runs on-demand)

HAL now includes 9 auto-healing domains in its comprehensive health check (mode 3):

1. SSH key permissions (600/700)
2. .mcp-ai permissions & cache cleanup
3. Venv executable permissions
4. Critical config file validation
5. Broken symlink detection
6. Locale/UTF-8 configuration
7. **SELinux error fixes** (NEW)
8. **Firewall configuration** (NEW)
9. **Auto-update timer verification** (NEW)

## Installation

### Prerequisites

- RHEL 10 system with sudo access
- systemd (usually pre-installed)
- DNF (package manager)

### Step 1: Install the Auto-Update Service

```bash
cd <REPO_ROOT>
sudo ./scripts/install-auto-update.sh
```

This script will:

- Copy the service/timer files to `/etc/systemd/system/`
- Copy the update script to `/usr/local/bin/hal-auto-update.sh`
- Reload systemd configuration
- Enable the timer
- Start the timer

### Step 2: Verify Installation

```bash
# Check timer status
systemctl status hal-auto-update.timer

# View next scheduled run
systemctl list-timers hal-auto-update.timer

# View service status
systemctl status hal-auto-update.service
```

## Usage

### View Update Logs

```bash
# Follow real-time logs
journalctl -u hal-auto-update.service -f

# View today's updates
cat /var/log/hal-updates/hal-auto-update-$(date +%Y%m%d).log

# View all update logs
ls -lh /var/log/hal-updates/
```

### Manually Run Updates

```bash
# Run updates immediately (requires sudo)
sudo /usr/local/bin/hal-auto-update.sh
```

### Test Auto-Healing in HAL

```bash
# Trigger health check with full auto-remediation
cd <REPO_ROOT>
python3 scripts/hal.py 'system health'
# When prompted: select "y" for health check, then "3" for full auto-remediation
```

## Files Created

| File                      | Location                               | Purpose                         |
| ------------------------- | -------------------------------------- | ------------------------------- |
| `hal-auto-update.sh`      | `scripts/`                             | Main update script (runs daily) |
| `hal-auto-update.service` | `/etc/systemd/system/` (after install) | systemd service unit            |
| `hal-auto-update.timer`   | `/etc/systemd/system/` (after install) | systemd timer (7 AM daily)      |
| `install-auto-update.sh`  | `scripts/`                             | Installation script             |

## Advanced Configuration

### Change Update Time

Edit `/etc/systemd/system/hal-auto-update.timer` and modify the `OnCalendar` line:

```ini
# Default: 7:00 AM daily
OnCalendar=*-*-* 07:00:00

# Examples:
# 9:00 AM: OnCalendar=*-*-* 09:00:00
# Midnight: OnCalendar=*-*-* 00:00:00
# Twice daily (7 AM and 7 PM): 
# OnCalendar=*-*-* 07:00:00
# OnCalendar=*-*-* 19:00:00
```

Then reload systemd:

```bash
sudo systemctl daemon-reload
sudo systemctl restart hal-auto-update.timer
```

### Disable Updates

```bash
# Disable the timer (won't run at 7 AM)
sudo systemctl disable hal-auto-update.timer

# Stop the timer immediately
sudo systemctl stop hal-auto-update.timer
```

### Re-enable Updates

```bash
sudo systemctl enable hal-auto-update.timer
sudo systemctl start hal-auto-update.timer
```

## What Each Update Task Does

### DNF Upgrade

- Updates all installed packages to latest versions
- Handles kernel updates (no reboot required at install time)
- Logs all package changes to syslog and local log file

### Flatpak Update

- Updates all installed flatpak applications
- Only runs if flatpak is installed
- Skipped silently if flatpak is not present

### SELinux Repairs

- Runs `restorecon` on `/home`, `/opt`, `/srv` directories
- Checks for SELinux denials using `ausearch`
- Reports denials in logs for manual review with `audit2allow`
- Only runs if SELinux tools are available

### Firewall Updates

- Verifies firewalld service is running
- Reloads firewall configuration (applies pending rules)
- Validates all zones are healthy
- Only runs if firewalld is installed

### Error Handling

- All tasks have built-in error handling
- Failures in one task don't prevent others from running
- All errors are logged with timestamps
- Log retention: One file per day in `/var/log/hal-updates/`

## Log File Format

Each day's log includes:

```text
[2026-04-29 07:00:15] === HAL Auto-Update Started ===
[2026-04-29 07:00:15] System: kaso
[2026-04-29 07:00:16] Starting DNF system upgrade...
[2026-04-29 07:05:42] ✓ DNF upgrade completed successfully
...
[2026-04-29 07:06:15] === Auto-Update Summary ===
[2026-04-29 07:06:15] DNF Upgrade:        OK
[2026-04-29 07:06:15] Flatpak Update:     OK
[2026-04-29 07:06:15] SELinux Fixes:      OK
[2026-04-29 07:06:15] Firewall Updates:   OK
```

## Troubleshooting

### Timer Not Running

```bash
# Check if timer is enabled
systemctl is-enabled hal-auto-update.timer

# Check next run time
systemctl list-timers hal-auto-update.timer

# Check for errors
journalctl -u hal-auto-update.timer -n 20
```

### Updates Not Completing

```bash
# Check service logs
journalctl -u hal-auto-update.service -n 50

# Check if lock file is stuck
ls -la /run/hal-auto-update.lock

# Manually run to debug
sudo /usr/local/bin/hal-auto-update.sh
```

### Permission Denied Errors

- Ensure the install script was run with `sudo`
- Check that `/usr/local/bin/hal-auto-update.sh` is executable: `ls -l /usr/local/bin/hal-auto-update.sh`
- Systemd services run as root, so they should have full permissions

## Integration with HAL Health Checks

When you run HAL with a system health check (mode 3 - full auto-remediation), it now performs:

- All 9 auto-healing checks
- Reports all fixes applied
- Provides aggregated summary
- Validates with post-remediation checks

The auto-update timer status is part of these checks, so HAL can alert if it detects the timer is not enabled.

## System Impact

- **Disk Usage**: Minimal (~50MB for logs per month)
- **Network**: During update window only (randomized 0-5 min after 7 AM)
- **CPU**: High during update, normal afterwards
- **Downtime**: Zero (no reboot required)

## Next Steps

1. Install the service: `sudo ./scripts/install-auto-update.sh`
2. Verify it's running: `systemctl status hal-auto-update.timer`
3. Check logs the next morning: `cat /var/log/hal-updates/hal-auto-update-*.log`
4. (Optional) Customize the time by editing `/etc/systemd/system/hal-auto-update.timer`
