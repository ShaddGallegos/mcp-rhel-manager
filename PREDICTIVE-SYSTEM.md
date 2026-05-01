# HAL Predictive System - Quick Start

## New Features

HAL now includes a complete predictive system with trending metrics, health scoring, and intelligent alerts.

## Getting Started

### 1. Initial Setup (First Run)

```bash
cd /home/sgallego/GIT/mcp-rhel-manager
python3 hal.py --inventory
```

This will:

- Detect installed applications (Docker, Ansible, Git, etc.)
- Check for notification integrations (Slack, PagerDuty, Grafana)
- Create interactive setup menu
- Save configuration to ~/.mcp-ai/hal-setup.json

### 2. View System Trends

```bash
# Display 7-day metrics dashboard
python3 hal.py --metrics

# View 30-day trends
python3 hal.py --metrics --metric-days 30

# Display specific metric only
python3 hal-dashboard.py --metric cpu
```

### 3. Check Predictions

```bash
# Show predicted threshold breaches for next 7 days
python3 hal.py --predict
```

## What's New

### CLI Commands Added

#### `--inventory`

Runs first-time system detection and setup:

- Scans for 13+ applications (Docker, Kubernetes, Ansible, etc.)
- Detects notification integrations (Slack, PagerDuty, Grafana)
- Interactive menu for enabling metrics & notifications
- Saves configuration for future use

#### `--metrics` / `--metric-days N`

Beautiful terminal dashboard showing:

- System health score (A-F grade)
- Per-metric trends with Unicode sparklines
- Min/max/average statistics
- Trend direction (increasing/decreasing) with percentage change
- Sample count and data availability

Example output:

```text
SYSTEM HEALTH SCORE
  Overall:   [CRITICAL] [====      ]  40.8/100 [F]

CPU Usage
  Current:   [OK      ] [====      ]   46.6%
  Trend:     / Increasing (+18.5% over 7 days)
  Average:     41.1%  (Min:   32.5%  Max:   49.8%)
  History:   ▃▂▃▄▄▃▄▂▃▅▃▃▅▄▃▅▅▄▅▅▆▆▅▆█▆▇▆▆▆
  Samples:   42 data point(s)
```text

#### `--predict`

Shows predictive alerts:

- Identifies metrics trending toward thresholds
- Projects days until threshold breach
- Calculates rate of change
- Shows predicted breach date
- Severity levels (critical/warning/info)

### New Modules

#### hal-notify.py

Multi-channel notification system supporting:

- **Slack**: Rich attachments with metric details and action buttons
- **PagerDuty**: V2 API integration for incident routing
- **Grafana**: Annotations for metric events
- **Email**: Mail command wrapper
- **systemd**: Notifications to logged-in users

Configuration auto-detected from ~/.mcp-ai/hal-setup.json

#### hal-dashboard.py

Standalone trending dashboard:

- Same visualization as `--metrics` command
- Supports `--days N` for custom timeframe
- Can filter specific metrics with `--metric`

#### hal-metrics.py

Metrics collection & prediction engine:

- Collects hourly metric samples
- Calculates 7/30-day trends
- Linear regression forecasting
- Threshold breach prediction
- Data stored in ~/.mcp-ai/metrics/ (daily JSON files)

## Data Storage

### Configuration

`~/.mcp-ai/hal-setup.json` - HAL setup configuration (600 perms)

### Metrics

`~/.mcp-ai/metrics/` - Daily metric data files

- Format: `metric_type_YYYY-MM-DD.json`
- Each file contains hourly samples: `{type, date, samples: [{timestamp, value, tags}]}`
- Retention: 90 days (configurable)

### Inventory

`~/.mcp-ai/hal-inventory.json` - Detected system inventory

## Tracked Metrics

- **CPU Usage** (%): System-wide CPU utilization
- **Memory Usage** (%): RAM usage percentage  
- **Disk Usage** (%): Primary partition usage
- **Network In** (Mbps): Incoming bandwidth
- **Network Out** (Mbps): Outgoing bandwidth

## Thresholds

- **CPU**: Warning at 70%, Critical at 90%
- **Memory**: Warning at 70%, Critical at 90%
- **Disk**: Warning at 70%, Critical at 90%

## Next Steps

1. Enable metrics collection in first-run setup
2. Configure notifications (Slack/PagerDuty/Grafana optional)
3. Wait for metrics to accumulate (24-48 hours for meaningful trends)
4. Monitor dashboard with `python3 hal.py --metrics`
5. Check predictions with `python3 hal.py --predict`

## Integration Roadmap

Currently in development:

- Systemd timer for automatic daily metrics collection
- Automated prediction alerting
- Anomaly detection
- Machine learning recommendations
- Custom threshold configuration per user

## Troubleshooting

### No metrics showing

- Check: `ls ~/.mcp-ai/metrics/`
- Ensure: Files have proper date format (`YYYY-MM-DD`)
- Check: At least 2 samples exist
- Run: `python3 hal.py --inventory` to create initial setup

### Import errors

- Ensure: Running from `/home/sgallego/GIT/mcp-rhel-manager/` directory
- Check: All `.py` files are executable (`chmod +x hal*.py`)
- Try: `python3 -m py_compile hal-metrics.py`

### Predictions always empty

- This is normal if system is stable
- Metrics need 7+ days of trending history
- Predictions only trigger when approaching configured thresholds

## Command Reference

```bash
# System setup
python3 hal.py --inventory

# View trends
python3 hal.py --metrics
python3 hal.py --metrics --metric-days 14
python3 hal-dashboard.py --days 30
python3 hal-dashboard.py --metric memory

# Check predictions
python3 hal.py --predict

# Help
python3 hal.py --help
```
