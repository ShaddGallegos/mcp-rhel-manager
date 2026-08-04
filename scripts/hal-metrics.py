#!/usr/bin/env python3
"""
HAL Metrics, Trending & Prediction Engine
Collects, analyzes, and predicts system behavior
"""

import os
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import timezone
import statistics

HOME = os.path.expanduser('~')
METRICS_DIR = os.path.join(HOME, '.mcp-ai', 'metrics')
CONFIG_DIR = os.path.join(HOME, '.mcp-ai')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'hal-setup.json')


def ensure_metrics_dir():
    """Ensure metrics directory exists."""
    os.makedirs(METRICS_DIR, mode=0o700, exist_ok=True)


def get_config() -> dict:
    """Load HAL configuration."""
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def collect_metric(metric_type: str, value: float, tags: dict = None) -> bool:
    """Collect a single metric data point."""
    ensure_metrics_dir()

    try:
        today = datetime.utcnow().strftime('%Y-%m-%d')
        metric_file = os.path.join(METRICS_DIR, f'{metric_type}_{today}.json')

        # Load existing metrics for today
        if os.path.isfile(metric_file):
            with open(metric_file, 'r') as f:
                metrics = json.load(f)
        else:
            metrics = {'type': metric_type, 'date': today, 'samples': []}

        # Add new sample
        sample = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'value': value,
            'tags': tags or {},
        }
        metrics['samples'].append(sample)

        # Save back
        with open(metric_file, 'w') as f:
            json.dump(metrics, f)

        return True
    except Exception:
        return False


def get_historical_metrics(metric_type: str, days: int = 7) -> List[Tuple[str, float]]:
    """Get historical metrics for analysis."""
    ensure_metrics_dir()
    data_points = []

    try:
        cutoff_date = (datetime.utcnow() - timedelta(days=days)).date()

        for file_path in sorted(Path(METRICS_DIR).glob(f'{metric_type}_*.json')):
            try:
                # Extract date from filename (format: metric_type_YYYY-MM-DD.json)
                # Get the part after the last underscore and before .json
                filename_parts = file_path.stem.rsplit('_', 1)
                if len(filename_parts) != 2:
                    continue
                date_str = filename_parts[1]
                file_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                if file_date < cutoff_date:
                    continue

                with open(file_path, 'r') as f:
                    metrics = json.load(f)

                for sample in metrics.get('samples', []):
                    timestamp = sample['timestamp']
                    value = sample['value']
                    data_points.append((timestamp, value))
            except Exception:
                pass

        return sorted(data_points)
    except Exception:
        return []


def calculate_trend(metric_type: str, days: int = 7) -> Optional[dict]:
    """Calculate trend for a metric."""
    data = get_historical_metrics(metric_type, days)
    if not data or len(data) < 2:
        return None

    values = [v for _, v in data]
    timestamps = [t for t, _ in data]

    try:
        avg = statistics.mean(values)
        min_val = min(values)
        max_val = max(values)
        current = values[-1] if values else 0

        # Calculate trend direction (increase/decrease)
        first_half = statistics.mean(values[:len(values)//2]) if len(values) > 1 else 0
        second_half = statistics.mean(values[len(values)//2:]) if len(values) > 1 else 0
        trend_direction = 'increasing' if second_half > first_half else 'decreasing'
        trend_percent = ((second_half - first_half) / first_half * 100) if first_half > 0 else 0

        return {
            'metric_type': metric_type,
            'days': days,
            'current': current,
            'average': avg,
            'min': min_val,
            'max': max_val,
            'trend': trend_direction,
            'trend_percent': round(trend_percent, 1),
            'data_points': len(values),
            'timestamps': timestamps,
            'values': values,
        }
    except Exception:
        return None


def predict_threshold_breach(metric_type: str, threshold: float, days_ahead: int = 7) -> Optional[dict]:
    """Predict when a metric will breach a threshold."""
    trend = calculate_trend(metric_type, days=30)
    if not trend or trend['trend'] == 'decreasing':
        return None

    try:
        values = trend['values']
        if len(values) < 2:
            return None

        # Simple linear regression
        x = list(range(len(values)))
        y = values
        n = len(values)

        x_avg = sum(x) / n
        y_avg = sum(y) / n

        numerator = sum((x[i] - x_avg) * (y[i] - y_avg) for i in range(n))
        denominator = sum((x[i] - x_avg) ** 2 for i in range(n))

        if denominator == 0:
            return None

        slope = numerator / denominator
        intercept = y_avg - slope * x_avg

        # Project forward
        if slope <= 0:
            return None

        # Find x where y = threshold
        days_to_threshold = (threshold - intercept) / slope if slope > 0 else None

        if days_to_threshold and 0 < days_to_threshold <= days_ahead:
            return {
                'metric_type': metric_type,
                'current_value': values[-1],
                'threshold': threshold,
                'days_to_breach': round(days_to_threshold, 1),
                'projected_date': (datetime.now(timezone.utc) + timedelta(days=days_to_threshold)).strftime('%Y-%m-%d'),
                'severity': 'critical' if days_to_threshold < 2 else 'warning' if days_to_threshold < 5 else 'info',
                'rate_of_change': round(slope, 2),
            }
    except Exception:
        pass

    return None


def get_all_predictions(days_ahead: int = 7) -> List[dict]:
    """Get all active predictions."""
    predictions = []

    # Disk usage prediction
    disk_pred = predict_threshold_breach('disk_usage', threshold=90, days_ahead=days_ahead)
    if disk_pred:
        predictions.append(disk_pred)

    # Memory usage prediction
    mem_pred = predict_threshold_breach('memory_usage', threshold=90, days_ahead=days_ahead)
    if mem_pred:
        predictions.append(mem_pred)

    # CPU temperature prediction (if available)
    temp_pred = predict_threshold_breach('cpu_temp', threshold=85, days_ahead=days_ahead)
    if temp_pred:
        predictions.append(temp_pred)

    return sorted(predictions, key=lambda x: x['days_to_breach'])


def cleanup_old_metrics(retention_days: int = 90):
    """Clean up metrics older than retention period."""
    ensure_metrics_dir()
    cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
    cleaned = 0

    try:
        for file_path in Path(METRICS_DIR).glob('*.json'):
            try:
                file_date_str = file_path.stem.split('_', 1)[1]
                file_date = datetime.strptime(file_date_str, '%Y-%m-%d')
                if file_date < cutoff_date:
                    os.remove(file_path)
                    cleaned += 1
            except Exception:
                pass

        return cleaned
    except Exception:
        return 0


def generate_metrics_report(days: int = 7) -> dict:
    """Generate comprehensive metrics report."""
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'period_days': days,
        'trends': [],
        'predictions': [],
    }

    # Collect trends
    for metric_type in ['cpu_usage', 'memory_usage', 'disk_usage', 'network_in', 'network_out']:
        trend = calculate_trend(metric_type, days)
        if trend:
            report['trends'].append(trend)

    # Get predictions
    report['predictions'] = get_all_predictions(days_ahead=7)

    return report
