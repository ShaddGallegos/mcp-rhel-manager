#!/usr/bin/env python3
"""Weekly chkrootkit maintenance and scan report.

This script reuses the user's existing email/report configuration. It does not
prompt for a new email address. It can:
- run the provided chkrootkit installer script to refresh/update installation
- run chkrootkit scan output parsing
- write structured results for weekly reporting
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

try:
    import mcp_config as cfg
except Exception:
    cfg = None

HOME = Path.home()
DEFAULT_ENV_PATH = Path(os.path.expanduser(os.getenv('ANSIBLE_ENV_PATH', str(HOME / '.ansible' / 'conf' / 'env.yml'))))
DEFAULT_REPORT_DIR = Path(getattr(cfg, 'REPORTS_DIR', str(HOME / '.mcp-ai' / 'reports')))
LATEST_REPORT = DEFAULT_REPORT_DIR / 'chkrootkit-latest.json'
HISTORY_REPORT = DEFAULT_REPORT_DIR / 'chkrootkit-history.jsonl'
DEFAULT_INSTALLER = Path(os.path.expanduser(os.getenv('HAL_CHKROOTKIT_INSTALLER', str(HOME / 'Downloads' / 'chkrootkit' / 'install.sh'))))


def _progress_enabled() -> bool:
    return os.environ.get('HAL_NO_PROGRESS', '').lower() not in ('1', 'true', 'yes') and os.isatty(2)


def _run_with_spinner(cmd: list[str], label: str, timeout: int) -> tuple[int, str, str]:
    if not _progress_enabled():
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or '', proc.stderr or ''

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    spin = ['|', '/', '-', '\\']
    idx = 0
    start = time.time()
    while True:
        rc = p.poll()
        if rc is not None:
            break
        elapsed = int(time.time() - start)
        print(f"\r{label} {spin[idx % len(spin)]} {elapsed}s", end='', file=sys.stderr, flush=True)
        idx += 1
        if elapsed >= timeout:
            p.kill()
            out, err = p.communicate()
            print(file=sys.stderr, flush=True)
            return 124, out or '', err or f'{label} timed out'
        time.sleep(0.2)

    out, err = p.communicate()
    print(f"\r{label} done", file=sys.stderr, flush=True)
    return p.returncode, out or '', err or ''


def _flatten_env_mapping(data: dict[str, Any]) -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if nested_value is not None:
                    flat[str(nested_key)] = str(nested_value)
        elif value is not None:
            flat[str(key)] = str(value)
    return flat


def load_settings() -> dict[str, str]:
    settings: dict[str, str] = {}
    if yaml is not None and DEFAULT_ENV_PATH.exists():
        try:
            parsed = yaml.safe_load(DEFAULT_ENV_PATH.read_text(encoding='utf-8')) or {}
            if isinstance(parsed, dict):
                settings.update(_flatten_env_mapping(parsed))
        except Exception:
            pass
    for key in (
        'HAL_ENABLE_WEEKLY_CHKROOTKIT',
        'HAL_CHKROOTKIT_INSTALLER',
        'HAL_WEEKLY_REPORT_EMAIL',
    ):
        value = os.getenv(key)
        if value:
            settings[key] = value
    settings.setdefault('HAL_ENABLE_WEEKLY_CHKROOTKIT', '1')
    return settings


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def run_command(cmd: list[str], timeout: int = 3600, label: str | None = None) -> tuple[int, str, str]:
    try:
        if label:
            return _run_with_spinner(cmd, label, timeout)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or '', proc.stderr or ''
    except subprocess.TimeoutExpired as exc:
        return 124, '', str(exc)
    except Exception as exc:
        return 1, '', str(exc)


def _host_default_email() -> str:
    return f'root@{platform.node() or "localhost"}'


def ensure_chkrootkit(settings: dict[str, str]) -> dict[str, Any]:
    installer = Path(os.path.expanduser(settings.get('HAL_CHKROOTKIT_INSTALLER', str(DEFAULT_INSTALLER))))
    email = (settings.get('HAL_WEEKLY_REPORT_EMAIL') or '').strip() or _host_default_email()
    result: dict[str, Any] = {
        'installer': str(installer),
        'attempted': False,
        'updated': False,
        'rc': None,
        'detail': '',
    }

    if not installer.exists():
        result['detail'] = 'installer not found'
        return result
    if not os.access(installer, os.X_OK):
        result['detail'] = 'installer not executable'
        return result

    result['attempted'] = True
    rc, out, err = run_command(['bash', str(installer), email], timeout=7200, label='Updating chkrootkit')
    result['rc'] = rc
    result['updated'] = rc == 0
    result['detail'] = (err or out).strip()[:1200]
    return result


def run_chkrootkit_scan() -> dict[str, Any]:
    chk = shutil.which('chkrootkit') or '/usr/bin/chkrootkit'
    if not Path(chk).exists() and not shutil.which('chkrootkit'):
        return {
            'available': False,
            'rc': None,
            'findings_count': 0,
            'findings': [],
            'detail': 'chkrootkit binary not found',
        }

    rc, out, err = run_command([chk], timeout=7200, label='Running chkrootkit scan')
    findings = []
    pattern = re.compile(r'(INFECTED|Vulnerable)', re.IGNORECASE)
    for line in out.splitlines():
        if pattern.search(line):
            findings.append(line.strip())

    return {
        'available': True,
        'binary': chk,
        'rc': rc,
        'findings_count': len(findings),
        'findings': findings,
        'detail': (err or '').strip()[:1200],
    }


def write_report(payload: dict[str, Any]) -> Path:
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    with open(HISTORY_REPORT, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(payload) + '\n')
    return LATEST_REPORT


def main() -> int:
    parser = argparse.ArgumentParser(description='Run weekly chkrootkit maintenance and scan')
    parser.add_argument('--stdout', action='store_true', help='Print JSON payload to stdout')
    args = parser.parse_args()

    settings = load_settings()
    enabled = str(settings.get('HAL_ENABLE_WEEKLY_CHKROOTKIT', '1')).strip().lower() not in ('0', 'false', 'no', 'disabled')
    if not enabled:
        payload = {'timestamp': utc_now(), 'enabled': False, 'detail': 'weekly chkrootkit maintenance disabled'}
        write_report(payload)
        if args.stdout:
            print(json.dumps(payload, indent=2))
        return 0

    maint = ensure_chkrootkit(settings)
    scan = run_chkrootkit_scan()
    payload = {
        'timestamp': utc_now(),
        'enabled': True,
        'maintenance': maint,
        'scan': scan,
        'serious_findings': int(scan.get('findings_count', 0) or 0) > 0,
    }
    report_path = write_report(payload)

    if args.stdout:
        print(json.dumps(payload, indent=2))
    else:
        print(f'chkrootkit report written to {report_path}')
    return 1 if payload['serious_findings'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
