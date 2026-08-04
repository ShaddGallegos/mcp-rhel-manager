#!/usr/bin/env python3
"""
HAL Trending Dashboard
Displays system trends and predictions in terminal
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

# Import from hal-metrics dynamically
from importlib.util import spec_from_file_location, module_from_spec

metrics_path = os.path.join(os.path.dirname(__file__), 'hal-metrics.py')
spec = spec_from_file_location("hal_metrics", metrics_path)
hal_metrics = module_from_spec(spec)
spec.loader.exec_module(hal_metrics)

calculate_trend = hal_metrics.calculate_trend
get_all_predictions = hal_metrics.get_all_predictions


def format_sparkline(values, width=20):
    """Create ASCII sparkline from values."""
    if not values or len(values) == 0:
        return '-' * width

    min_val = min(values)
    max_val = max(values)
    range_val = max_val - min_val if max_val > min_val else 1

    # Normalize values to 0-8 (number of blocks)
    bars = [int(((v - min_val) / range_val * 8)) if range_val > 0 else 4 for v in values]

    # Unicode block characters (from empty to full)
    blocks = [' ', '▁', '▂', '▃', '▄', '▅', '▆', '▇', '█']
    return ''.join(blocks[b] for b in bars[-width:])


def format_trend_bar(value, max_val=100, width=10):
    """Create ASCII progress bar."""
    if value < 0:
        return '[' + '-' * width + ']'

    filled = int((value / max_val) * width)
    filled = min(filled, width)

    bar = '[' + '=' * filled + ' ' * (width - filled) + ']'
    return bar


def get_status_indicator(value, warning=70, critical=90):
    """Get status indicator based on thresholds."""
    if value >= critical:
        return '[CRITICAL]'
    elif value >= warning:
        return '[WARNING ]'
    else:
        return '[OK      ]'


def display_metric_trend(metric_type, metric_name, unit='%', days=7, max_val=100):
    """Display a single metric trend."""
    trend = calculate_trend(metric_type, days)

    if not trend or trend['data_points'] == 0:
        print(f"\n{metric_name}: No data available")
        return

    current = trend['current']
    avg = trend['average']
    min_val = trend['min']
    max_val_actual = trend['max']
    trend_dir = trend['trend']
    trend_pct = trend['trend_percent']

    # Build display
    print(f"\n{metric_name}")
    print("-" * 70)

    # Current value with status
    status = get_status_indicator(current, warning=70, critical=90)
    bar = format_trend_bar(current, max_val)
    print(f"  Current:   {status} {bar} {current:6.1f}{unit}")

    # Trend info
    trend_arrow = '/' if trend_dir == 'increasing' else '\\'
    print(f"  Trend:     {trend_arrow} {trend_dir.capitalize()} ({trend_pct:+.1f}% over {days} days)")

    # Stats
    print(f"  Average:   {avg:6.1f}{unit}  (Min: {min_val:6.1f}{unit}  Max: {max_val_actual:6.1f}{unit})")

    # Sparkline
    sparkline = format_sparkline(trend['values'], width=30)
    print(f"  History:   {sparkline}")

    # Data points
    print(f"  Samples:   {trend['data_points']} data point(s)")


def display_predictions():
    """Display all active predictions."""
    predictions = get_all_predictions(days_ahead=7)

    if not predictions:
        print("\n\nNo active predictions. System is stable.")
        return

    print("\n\nPREDICTED ISSUES (Next 7 Days)")
    print("=" * 70)

    for i, pred in enumerate(predictions, 1):
        metric = pred['metric_type'].upper()
        days = pred['days_to_breach']
        current = pred['current_value']
        threshold = pred['threshold']
        severity = pred['severity'].upper()
        date = pred['projected_date']
        rate = pred['rate_of_change']

        print(f"\n{i}. {metric} Threshold Breach")
        print(f"   Severity:  [{severity:8s}]")
        print(f"   Current:   {current:6.1f}% of {threshold:6.1f}% threshold")
        print(f"   Timeframe: {days:5.1f} days until {date}")
        print(f"   Rate:      {rate:6.2f}%/day")

        bar = format_trend_bar(current, threshold)
        print(f"   Progress:  {bar}")


def display_health_score():
    """Calculate and display overall system health score."""
    # Health score based on available metrics
    scores = []

    for metric_type, max_val in [('cpu_usage', 100), ('memory_usage', 100), ('disk_usage', 100)]:
        trend = calculate_trend(metric_type, days=7)
        if trend:
            current = trend['current']
            # Score: 100 if under 50%, decreases as it approaches max
            score = max(0, 100 - (current / max_val * 100))
            scores.append(score)

    if scores:
        avg_score = sum(scores) / len(scores)

        # Grade letter
        if avg_score >= 90:
            grade = 'A'
        elif avg_score >= 80:
            grade = 'B'
        elif avg_score >= 70:
            grade = 'C'
        elif avg_score >= 60:
            grade = 'D'
        else:
            grade = 'F'

        status = get_status_indicator(100 - avg_score, warning=30, critical=50)
        bar = format_trend_bar(avg_score, max_val=100)

        print(f"\nSYSTEM HEALTH SCORE")
        print("=" * 70)
        print(f"  Overall:   {status} {bar} {avg_score:5.1f}/100 [{grade}]")


def display_summary():
    """Display system summary and top issues."""
    config_file = os.path.join(os.path.expanduser('~'), '.mcp-ai', 'hal-setup.json')

    print("\n" + "=" * 70)
    print("HAL TRENDING DASHBOARD")
    print("=" * 70)
    print(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    if os.path.isfile(config_file):
        print(f"Configuration: Loaded")
    else:
        print(f"Configuration: Not configured (run hal-inventory.py first)")

    print("=" * 70)


def main():
    import argparse

    parser = argparse.ArgumentParser(description='HAL Trending Dashboard')
    parser.add_argument('--days', type=int, default=7, help='Number of days to analyze')
    parser.add_argument('--metric', help='Show specific metric only')

    args = parser.parse_args()

    # Display header
    display_summary()

    # Display health score
    display_health_score()

    # Display metric trends
    if args.metric:
        metric_map = {
            'cpu': ('cpu_usage', 'CPU Usage'),
            'memory': ('memory_usage', 'Memory Usage'),
            'disk': ('disk_usage', 'Disk Usage'),
            'net_in': ('network_in', 'Network In'),
            'net_out': ('network_out', 'Network Out'),
        }
        if args.metric in metric_map:
            metric_type, name = metric_map[args.metric]
            display_metric_trend(metric_type, name, days=args.days)
    else:
        # Show all metrics
        display_metric_trend('cpu_usage', 'CPU Usage', days=args.days)
        display_metric_trend('memory_usage', 'Memory Usage', days=args.days)
        display_metric_trend('disk_usage', 'Disk Usage', days=args.days)

    # Display predictions
    display_predictions()

    print("\n" + "=" * 70 + "\n")

    return 0


if __name__ == '__main__':
    sys.exit(main())
