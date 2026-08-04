#!/usr/bin/env python3
"""
HAL Notification System
Sends alerts and notifications to various channels
"""

import os
import json
import subprocess
from typing import Dict, List, Optional
from datetime import datetime

HOME = os.path.expanduser('~')
CONFIG_DIR = os.path.join(HOME, '.mcp-ai')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'hal-setup.json')


def get_config() -> dict:
    """Load HAL configuration."""
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def send_slack_notification(message: str, title: str = "HAL Alert", severity: str = "info", prediction: dict = None) -> bool:
    """Send notification to Slack."""
    config = get_config()
    slack_config = config.get('integrations', {}).get('slack', {})

    if not slack_config.get('enabled') or not slack_config.get('webhook_url'):
        return False

    try:
        webhook_url = slack_config['webhook_url']
        channel = slack_config.get('channel', '#alerts')

        # Color based on severity
        color_map = {
            'critical': '#FF0000',  # Red
            'warning': '#FFA500',   # Orange
            'info': '#0099FF',      # Blue
        }
        color = color_map.get(severity, '#0099FF')

        # Build Slack payload
        payload = {
            'channel': channel,
            'attachments': [
                {
                    'title': title,
                    'text': message,
                    'color': color,
                    'footer': 'HAL9000',
                    'ts': int(datetime.utcnow().timestamp()),
                }
            ]
        }

        # Add prediction details if provided
        if prediction:
            payload['attachments'][0]['fields'] = [
                {'title': 'Metric', 'value': prediction.get('metric_type', 'N/A'), 'short': True},
                {'title': 'Days to Breach', 'value': str(prediction.get('days_to_breach', 'N/A')), 'short': True},
                {'title': 'Current Value', 'value': f"{prediction.get('current_value', 0):.1f}%", 'short': True},
                {'title': 'Threshold', 'value': f"{prediction.get('threshold', 0):.1f}%", 'short': True},
                {'title': 'Projected Date', 'value': prediction.get('projected_date', 'N/A'), 'short': True},
                {'title': 'Rate of Change', 'value': f"{prediction.get('rate_of_change', 0):.2f}%/day", 'short': True},
            ]

            # Add action button
            payload['attachments'][0]['actions'] = [
                {
                    'type': 'button',
                    'text': 'View Trends',
                    'value': 'view_trends',
                    'url': 'http://localhost:8000',
                }
            ]

        # Send to Slack
        import requests
        response = requests.post(webhook_url, json=payload, timeout=10)
        return response.status_code == 200

    except Exception as e:
        print(f"Failed to send Slack notification: {e}")
        return False


def send_email_notification(recipient: str, subject: str, message: str) -> bool:
    """Send notification via email."""
    try:
        # Try using mail command
        cmd = f"echo '{message}' | mail -s '{subject}' {recipient}"
        result = subprocess.run(cmd, shell=True, capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def send_systemd_notification(message: str, severity: str = "info") -> bool:
    """Send systemd notification (visible to logged-in users)."""
    try:
        priority_map = {
            'critical': '2',
            'warning': '1',
            'info': '5',
        }
        priority = priority_map.get(severity, '5')

        cmd = f"systemd-notify --uid=$(id -u) 'STATUS={message}' --priority={priority}"
        result = subprocess.run(cmd, shell=True, capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def send_pagerduty_alert(alert_data: dict) -> bool:
    """Send alert to PagerDuty."""
    config = get_config()
    pd_config = config.get('integrations', {}).get('pagerduty', {})

    if not pd_config.get('enabled') or not pd_config.get('integration_key'):
        return False

    try:
        import requests

        payload = {
            'routing_key': pd_config['integration_key'],
            'event_action': 'trigger',
            'dedup_key': alert_data.get('dedup_key', f"hal-{int(datetime.utcnow().timestamp())}"),
            'payload': {
                'summary': alert_data.get('title', 'HAL Alert'),
                'severity': alert_data.get('severity', 'warning'),
                'source': 'HAL9000',
                'custom_details': alert_data.get('details', {}),
            }
        }

        response = requests.post(
            'https://events.pagerduty.com/v2/enqueue',
            json=payload,
            timeout=10
        )
        return response.status_code == 202

    except Exception as e:
        print(f"Failed to send PagerDuty alert: {e}")
        return False


def send_grafana_annotation(title: str, description: str, tags: List[str] = None) -> bool:
    """Send annotation to Grafana."""
    config = get_config()
    grafana_config = config.get('integrations', {}).get('grafana', {})

    if not grafana_config.get('enabled'):
        return False

    try:
        import requests

        url = f"{grafana_config.get('url', 'http://localhost:3000')}/api/annotations"
        headers = {
            'Authorization': f"Bearer {grafana_config.get('api_key', '')}",
            'Content-Type': 'application/json',
        }

        payload = {
            'text': description,
            'tags': tags or ['hal-alert'],
        }

        response = requests.post(url, json=payload, headers=headers, timeout=10)
        return response.status_code in [200, 201]

    except Exception as e:
        print(f"Failed to send Grafana annotation: {e}")
        return False


def send_notification(message: str, title: str = "HAL Alert", severity: str = "info",
                      prediction: dict = None, notify_type: str = 'all') -> int:
    """Send notification through all configured channels."""
    channels_sent = 0

    config = get_config()
    integrations = config.get('integrations', {})

    # Send to Slack
    if notify_type in ['all', 'slack'] and integrations.get('slack', {}).get('enabled'):
        if send_slack_notification(message, title, severity, prediction):
            channels_sent += 1

    # Send to PagerDuty (only for critical)
    if severity == 'critical' and integrations.get('pagerduty', {}).get('enabled'):
        alert_data = {
            'title': title,
            'severity': severity,
            'details': prediction or {},
        }
        if send_pagerduty_alert(alert_data):
            channels_sent += 1

    # Send Grafana annotation
    if integrations.get('grafana', {}).get('enabled'):
        if send_grafana_annotation(title, message, tags=['hal-alert', severity]):
            channels_sent += 1

    # Send systemd notification
    if send_systemd_notification(message, severity):
        channels_sent += 1

    return channels_sent


def broadcast_prediction_alert(prediction: dict) -> int:
    """Send alert for a prediction breach."""
    metric_type = prediction.get('metric_type', 'unknown').upper()
    days = prediction.get('days_to_breach', 0)
    severity = prediction.get('severity', 'info')

    title = f"System Alert: {metric_type} Threshold Breach Predicted"
    message = (
        f"{metric_type} is trending toward {prediction.get('threshold', 100):.1f}% threshold.\n"
        f"Predicted breach in {days} day(s) ({prediction.get('projected_date', 'unknown')}).\n"
        f"Current value: {prediction.get('current_value', 0):.1f}%\n"
        f"Rate of change: {prediction.get('rate_of_change', 0):.2f}%/day"
    )

    return send_notification(message, title, severity, prediction)
