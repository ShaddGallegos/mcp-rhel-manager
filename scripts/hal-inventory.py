#!/usr/bin/env python3
"""
HAL System Inventory & First-Run Setup
Detects installed applications and creates interactive configuration menu
"""

import os
import sys
import json
import subprocess
import shutil
from pathlib import Path

HOME = os.path.expanduser('~')
CONFIG_DIR = os.path.join(HOME, '.mcp-ai')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'hal-setup.json')
INVENTORY_FILE = os.path.join(CONFIG_DIR, 'hal-inventory.json')

# Default configuration
DEFAULT_CONFIG = {
    'version': '1.0',
    'first_run': True,
    'setup_date': None,
    'metrics_enabled': True,
    'metrics_retention_days': 90,
    'predictions_enabled': True,
    'notifications': {},
    'integrations': {},
}

# Applications to detect
APPS_TO_DETECT = {
    'docker': {
        'name': 'Docker',
        'command': 'docker --version',
        'feature': 'Container monitoring and management',
    },
    'kubernetes': {
        'name': 'Kubernetes (kubectl)',
        'command': 'kubectl version --client',
        'feature': 'Kubernetes cluster management',
    },
    'podman': {
        'name': 'Podman',
        'command': 'podman --version',
        'feature': 'Container management (Podman)',
    },
    'systemd': {
        'name': 'Systemd',
        'command': 'systemctl --version',
        'feature': 'Service management and monitoring',
    },
    'git': {
        'name': 'Git',
        'command': 'git --version',
        'feature': 'Version control and repository scanning',
    },
    'ansible': {
        'name': 'Ansible',
        'command': 'ansible --version',
        'feature': 'Configuration management and automation',
    },
    'prometheus': {
        'name': 'Prometheus',
        'command': 'prometheus --version',
        'feature': 'Metrics collection and storage',
    },
    'grafana': {
        'name': 'Grafana',
        'command': 'grafana-server --version',
        'feature': 'Metrics visualization and dashboards',
    },
    'mysql': {
        'name': 'MySQL/MariaDB',
        'command': 'mysql --version',
        'feature': 'Database monitoring',
    },
    'postgresql': {
        'name': 'PostgreSQL',
        'command': 'psql --version',
        'feature': 'Database monitoring',
    },
    'redis': {
        'name': 'Redis',
        'command': 'redis-cli --version',
        'feature': 'Cache/data store monitoring',
    },
    'nginx': {
        'name': 'Nginx',
        'command': 'nginx -v',
        'feature': 'Web server monitoring',
    },
    'apache': {
        'name': 'Apache',
        'command': 'apache2 -v',
        'feature': 'Web server monitoring',
    },
}

# Notification integrations to detect
INTEGRATIONS_TO_DETECT = {
    'slack': {
        'name': 'Slack',
        'config_path': os.path.join(HOME, '.slack'),
        'description': 'Receive alerts and notifications in Slack',
    },
    'pagerduty': {
        'name': 'PagerDuty',
        'config_path': os.path.join(HOME, '.pagerduty'),
        'description': 'Critical incident alerting via PagerDuty',
    },
    'grafana': {
        'name': 'Grafana',
        'config_path': os.path.join(HOME, '.grafana'),
        'description': 'Send metrics to Grafana for visualization',
    },
    'prometheus': {
        'name': 'Prometheus',
        'config_path': '/etc/prometheus',
        'description': 'Push metrics to Prometheus',
    },
}


def detect_app(app_info: dict) -> bool:
    """Check if an application is installed."""
    try:
        result = subprocess.run(
            app_info['command'],
            shell=True,
            capture_output=True,
            timeout=2
        )
        return result.returncode == 0
    except Exception:
        return False


def detect_integration(integration_info: dict) -> bool:
    """Check if an integration configuration exists."""
    return os.path.exists(integration_info['config_path'])


def scan_system() -> dict:
    """Scan system for installed applications and integrations."""
    inventory = {
        'scanned_at': subprocess.run(['date', '-u', '+%Y-%m-%dT%H:%M:%SZ'], capture_output=True, text=True).stdout.strip(),
        'applications': {},
        'integrations': {},
        'hostname': subprocess.run(['hostname'], capture_output=True, text=True).stdout.strip(),
        'os_info': subprocess.run(['uname', '-s'], capture_output=True, text=True).stdout.strip(),
    }

    # Detect applications
    print("Scanning for installed applications...\n")
    for app_key, app_info in APPS_TO_DETECT.items():
        installed = detect_app(app_info)
        inventory['applications'][app_key] = {
            'installed': installed,
            'name': app_info['name'],
            'feature': app_info['feature'],
        }
        status = "[FOUND]" if installed else "[not found]"
        print(f"  {status} {app_info['name']}")

    # Detect integrations
    print("\nScanning for notification integrations...\n")
    for int_key, int_info in INTEGRATIONS_TO_DETECT.items():
        available = detect_integration(int_info)
        inventory['integrations'][int_key] = {
            'available': available,
            'name': int_info['name'],
            'description': int_info['description'],
        }
        status = "[AVAILABLE]" if available else "[not found]"
        print(f"  {status} {int_info['name']}")

    return inventory


def interactive_setup(inventory: dict) -> dict:
    """Interactive configuration menu based on detected applications."""
    config = DEFAULT_CONFIG.copy()
    config['first_run'] = False
    config['setup_date'] = subprocess.run(['date', '-u', '+%Y-%m-%dT%H:%M:%SZ'], capture_output=True, text=True).stdout.strip()

    print("\n" + "=" * 70)
    print("HAL FIRST-RUN CONFIGURATION")
    print("=" * 70)

    # Metrics configuration
    print("\n[METRICS & TRENDING]")
    print("HAL can collect system metrics daily for trend analysis and predictions.")
    response = input("Enable metrics collection? [Y/n]: ").strip().lower()
    config['metrics_enabled'] = response != 'n'

    if config['metrics_enabled']:
        response = input("Retention period in days [90]: ").strip()
        config['metrics_retention_days'] = int(response) if response.isdigit() else 90

    # Predictions
    if config['metrics_enabled']:
        print("\n[PREDICTIVE ALERTS]")
        print("HAL can predict when problems will occur (disk full, memory exhaustion, etc).")
        response = input("Enable predictive alerts? [Y/n]: ").strip().lower()
        config['predictions_enabled'] = response != 'n'

    # Notifications
    print("\n[NOTIFICATIONS]")
    print("HAL can send alerts to various services:\n")

    available_integrations = [
        key for key, info in inventory['integrations'].items()
        if info['available']
    ]

    if available_integrations:
        print("Available integrations detected:")
        for int_key in available_integrations:
            int_info = INTEGRATIONS_TO_DETECT[int_key]
            print(f"  - {int_info['name']}: {int_info['description']}")
        print()

        for int_key in available_integrations:
            int_name = INTEGRATIONS_TO_DETECT[int_key]['name']
            response = input(f"Configure {int_name} integration? [y/N]: ").strip().lower()
            if response == 'y':
                config['integrations'][int_key] = _setup_integration(int_key)

    # Manual integrations
    print("\nManual integration setup (without detected config):")
    response = input("Setup Slack webhook manually? [y/N]: ").strip().lower()
    if response == 'y':
        config['integrations']['slack'] = _setup_slack_manual()

    # Feature summary
    print("\n" + "=" * 70)
    print("CONFIGURATION SUMMARY")
    print("=" * 70)
    print(f"Metrics collection: {'ENABLED' if config['metrics_enabled'] else 'DISABLED'}")
    if config['metrics_enabled']:
        print(f"  Retention: {config['metrics_retention_days']} days")
    print(f"Predictive alerts: {'ENABLED' if config['predictions_enabled'] else 'DISABLED'}")
    print(f"Integrations configured: {len(config['integrations'])}")
    for int_name, int_config in config['integrations'].items():
        print(f"  - {int_name}: configured")

    response = input("\nSave configuration? [Y/n]: ").strip().lower()
    return config if response != 'n' else DEFAULT_CONFIG.copy()


def _setup_integration(integration_key: str) -> dict:
    """Setup an integration that was detected."""
    if integration_key == 'slack':
        return _setup_slack_detected()
    elif integration_key == 'grafana':
        return _setup_grafana()
    elif integration_key == 'prometheus':
        return _setup_prometheus()
    elif integration_key == 'pagerduty':
        return _setup_pagerduty()
    return {}


def _setup_slack_detected() -> dict:
    """Setup Slack using detected credentials."""
    print("\nSlack integration detected.")
    webhook = input("Enter Slack webhook URL: ").strip()
    channel = input("Default channel [#alerts]: ").strip() or "#alerts"
    return {
        'webhook_url': webhook,
        'channel': channel,
        'enabled': True,
    }


def _setup_slack_manual() -> dict:
    """Setup Slack with manual webhook entry."""
    print("\nSlack Setup Instructions:")
    print("1. Go to https://api.slack.com/apps")
    print("2. Create New App > From scratch")
    print("3. Go to Incoming Webhooks, click Add New Webhook to Workspace")
    print("4. Select channel and authorize")
    print("5. Copy webhook URL\n")

    webhook = input("Paste Slack webhook URL here: ").strip()
    channel = input("Default channel [#alerts]: ").strip() or "#alerts"

    if webhook.startswith('https://hooks.slack.com'):
        return {
            'webhook_url': webhook,
            'channel': channel,
            'enabled': True,
        }
    else:
        print("ERROR: Invalid webhook URL")
        return {'enabled': False}


def _setup_grafana() -> dict:
    """Setup Grafana integration."""
    print("\nGrafana Integration Setup:")
    url = input("Grafana URL [http://localhost:3000]: ").strip() or "http://localhost:3000"
    api_key = input("API Key: ").strip()
    return {
        'url': url,
        'api_key': api_key,
        'enabled': True,
    }


def _setup_prometheus() -> dict:
    """Setup Prometheus integration."""
    print("\nPrometheus Integration Setup:")
    url = input("Prometheus URL [http://localhost:9090]: ").strip() or "http://localhost:9090"
    return {
        'url': url,
        'enabled': True,
    }


def _setup_pagerduty() -> dict:
    """Setup PagerDuty integration."""
    print("\nPagerDuty Integration Setup:")
    integration_key = input("Integration Key (routing key): ").strip()
    return {
        'integration_key': integration_key,
        'enabled': True,
    }


def save_config(config: dict, inventory: dict) -> None:
    """Save configuration to disk."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)

    # Save config
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)
    print(f"[OK] Configuration saved to {CONFIG_FILE}")

    # Save inventory
    with open(INVENTORY_FILE, 'w') as f:
        json.dump(inventory, f, indent=2)
    os.chmod(INVENTORY_FILE, 0o600)
    print(f"[OK] Inventory saved to {INVENTORY_FILE}")


def load_config() -> dict:
    """Load existing configuration."""
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()


def load_inventory() -> dict:
    """Load existing inventory."""
    if os.path.isfile(INVENTORY_FILE):
        try:
            with open(INVENTORY_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def main():
    parser_help = "HAL System Inventory & Setup Tool"

    # Check if already configured
    existing_config = load_config()

    if existing_config['first_run'] is False:
        print("HAL is already configured.")
        print(f"Configuration: {CONFIG_FILE}")
        print(f"Inventory: {INVENTORY_FILE}")

        response = input("\nRe-run setup? [y/N]: ").strip().lower()
        if response != 'y':
            return 0

    # Scan system
    print("Starting HAL system inventory...\n")
    inventory = scan_system()

    # Interactive setup
    config = interactive_setup(inventory)

    # Save configuration
    print()
    save_config(config, inventory)

    print("\n" + "=" * 70)
    print("Setup complete! HAL is ready to use.")
    print("=" * 70)
    print("\nNext steps:")
    print("1. Run system health check: python3 hal.py 'system health'")
    print("2. View metrics: python3 hal.py --metrics")
    print("3. Check predictions: python3 hal.py --predict")

    return 0


if __name__ == '__main__':
    sys.exit(main())
