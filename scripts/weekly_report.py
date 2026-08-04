#!/usr/bin/env python3
"""Generate and optionally email a weekly HAL changes/fixes report.

The report summarizes recent self-heal events, privileged actions, audit events,
and the latest health reports. It reads configuration from the user's
`~/.ansible/conf/env.yml` (or `ANSIBLE_ENV_PATH`) and can send the result via
`mailx`, `mutt`, or `mail` if available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
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
DEFAULT_SELF_HEAL_LOG = DEFAULT_REPORT_DIR / 'bridge-self-heal.log'
DEFAULT_PRIV_LOG = DEFAULT_REPORT_DIR / 'privileged_actions.log'
DEFAULT_AUDIT_LOG = DEFAULT_REPORT_DIR / 'mcp-server-audit.jsonl'
DEFAULT_MALWARE_REPORT = DEFAULT_REPORT_DIR / 'malware-scan-latest.json'
DEFAULT_VIRUS_REPORT = DEFAULT_REPORT_DIR / 'virus-scan-latest.json'
DEFAULT_CHKROOTKIT_REPORT = DEFAULT_REPORT_DIR / 'chkrootkit-latest.json'
DEFAULT_HEALTH_DIRS = [DEFAULT_REPORT_DIR, HOME / 'Documents' / 'reports']


class _Ansi:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    CYAN = '\033[36m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    RED = '\033[31m'


def _color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get('HAL_REPORT_COLOR', '1').lower() not in ('0', 'false', 'no')


def _colorize(text: str, color: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f'{color}{text}{_Ansi.RESET}'


def _human_bytes(n: int) -> str:
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    v = float(max(0, n))
    for u in units:
        if v < 1024.0 or u == units[-1]:
            return f'{v:.1f}{u}'
        v /= 1024.0
    return f'{n}B'


def _safe_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except Exception:
        return default


def _default_scan_roots() -> list[Path]:
    roots: list[Path] = []
    raw = os.getenv('HAL_WEEKLY_REPORT_SCAN_PATHS', '').strip()
    if raw:
        for item in raw.split(','):
            p = Path(os.path.expanduser(item.strip()))
            if p.exists() and p.is_dir():
                roots.append(p)
    if roots:
        return roots
    for p in (HOME / 'Documents', HOME / 'Downloads', HOME / '.mcp-ai' / 'training'):
        if p.exists() and p.is_dir():
            roots.append(p)
    return roots[:3]


def collect_mount_free_space() -> list[dict[str, Any]]:
    mounts: list[str] = []
    if Path('/proc/mounts').exists():
        try:
            for line in Path('/proc/mounts').read_text(encoding='utf-8', errors='ignore').splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt, fstype = parts[1], parts[2]
                if fstype in {'proc', 'sysfs', 'tmpfs', 'devtmpfs', 'cgroup', 'cgroup2', 'overlay', 'squashfs'}:
                    continue
                mounts.append(mnt)
        except Exception:
            mounts = []
    if '/' not in mounts:
        mounts.append('/')

    seen = set()
    rows: list[dict[str, Any]] = []
    for m in mounts:
        if m in seen:
            continue
        seen.add(m)
        try:
            st = os.statvfs(m)
            total = st.f_frsize * st.f_blocks
            free = st.f_frsize * st.f_bavail
            used = max(0, total - free)
            pct = (used / total * 100.0) if total > 0 else 0.0
            rows.append({'mount': m, 'total': total, 'used': used, 'free': free, 'used_pct': round(pct, 1)})
        except Exception:
            continue
    rows.sort(key=lambda x: x['used_pct'], reverse=True)
    return rows


def collect_large_files(roots: list[Path], max_files: int = 12) -> list[dict[str, Any]]:
    cap_files = _safe_int_env('HAL_WEEKLY_REPORT_MAX_SCAN_FILES', 8000)
    min_size = _safe_int_env('HAL_WEEKLY_REPORT_LARGE_MIN_BYTES', 100 * 1024 * 1024)
    items: list[dict[str, Any]] = []
    scanned = 0
    for root in roots:
        try:
            for p in root.rglob('*'):
                if scanned >= cap_files:
                    break
                if not p.is_file():
                    continue
                scanned += 1
                try:
                    sz = p.stat().st_size
                except Exception:
                    continue
                if sz >= min_size:
                    items.append({'path': str(p), 'size': sz})
        except Exception:
            continue
    items.sort(key=lambda x: x['size'], reverse=True)
    return items[:max_files]


def _quick_file_hash(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def collect_duplicate_files(roots: list[Path], max_groups: int = 8) -> list[dict[str, Any]]:
    cap_files = _safe_int_env('HAL_WEEKLY_REPORT_MAX_DUP_SCAN_FILES', 4000)
    max_size = _safe_int_env('HAL_WEEKLY_REPORT_MAX_DUP_FILE_BYTES', 200 * 1024 * 1024)
    by_size: dict[int, list[Path]] = defaultdict(list)
    scanned = 0
    for root in roots:
        try:
            for p in root.rglob('*'):
                if scanned >= cap_files:
                    break
                if not p.is_file():
                    continue
                scanned += 1
                try:
                    sz = p.stat().st_size
                except Exception:
                    continue
                if sz <= 0 or sz > max_size:
                    continue
                by_size[sz].append(p)
        except Exception:
            continue

    dups: list[dict[str, Any]] = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[str]] = defaultdict(list)
        for p in paths:
            digest = _quick_file_hash(p)
            if digest:
                by_hash[digest].append(str(p))
        for digest, members in by_hash.items():
            if len(members) > 1:
                dups.append({'size': size, 'hash': digest[:12], 'count': len(members), 'paths': members[:8]})
    dups.sort(key=lambda x: (x['size'] * x['count']), reverse=True)
    return dups[:max_groups]


def collect_gpu_usage() -> dict[str, Any]:
    nvidia = shutil.which('nvidia-smi')
    if nvidia:
        try:
            proc = subprocess.run(
                [nvidia, '--query-gpu=name,utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc.returncode == 0:
                rows = []
                for line in (proc.stdout or '').splitlines():
                    parts = [x.strip() for x in line.split(',')]
                    if len(parts) >= 4:
                        rows.append({'name': parts[0], 'gpu_pct': parts[1], 'mem_used_mb': parts[2], 'mem_total_mb': parts[3]})
                return {'available': True, 'vendor': 'nvidia', 'rows': rows}
        except Exception:
            pass
    return {'available': False, 'vendor': None, 'rows': []}


def collect_bleachbit_status() -> dict[str, Any]:
    exe = shutil.which('bleachbit')
    status = {'installed': bool(exe), 'path': exe or '', 'source': 'native'}
    if not exe:
        flatpak = shutil.which('flatpak')
        if flatpak:
            try:
                proc = subprocess.run([flatpak, 'list', '--app', '--columns=application'], capture_output=True, text=True, timeout=8)
                apps = set((proc.stdout or '').splitlines())
                for app_id in ('org.bleachbit.BleachBit', 'com.bleachbit.BleachBit'):
                    if app_id in apps:
                        status['installed'] = True
                        status['path'] = f'flatpak run {app_id}'
                        status['source'] = 'flatpak'
                        status['app_id'] = app_id
                        return status
            except Exception:
                pass
        return status
    try:
        proc = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=5)
        text = (proc.stdout or proc.stderr or '').strip().splitlines()
        status['version'] = text[0] if text else 'unknown'
    except Exception:
        status['version'] = 'unknown'
    return status


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
            with open(DEFAULT_ENV_PATH, 'r', encoding='utf-8') as fh:
                parsed = yaml.safe_load(fh) or {}
            if isinstance(parsed, dict):
                settings.update(_flatten_env_mapping(parsed))
        except Exception:
            pass
    for key in (
        'HAL_SELF_HEAL_MODE',
        'HAL_WEEKLY_REPORT_EMAIL',
        'HAL_EMAIL_BACKEND',
        'HAL_WEEKLY_REPORT_DAYS',
        'ANSIBLE_ENV_PATH',
        'ANSIBLE_VAULT_PASSWORD_FILE',
    ):
        value = os.getenv(key)
        if value:
            settings[key] = value
    settings.setdefault('HAL_SELF_HEAL_MODE', 'approval')
    settings.setdefault('HAL_EMAIL_BACKEND', 'auto')
    settings.setdefault('HAL_WEEKLY_REPORT_DAYS', '7')
    return settings



def _parse_utc_ts(value: str) -> datetime | None:
    try:
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None



def _read_lines(path: Path) -> list[str]:
    try:
        if path.exists():
            return path.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception:
        pass
    return []



def collect_self_heal_events(path: Path, since: datetime) -> dict[str, Any]:
    events: list[tuple[datetime, str, str]] = []
    for line in _read_lines(path):
        if ' [' not in line:
            continue
        ts_text, rest = line.split(' [', 1)
        ts = _parse_utc_ts(ts_text.strip())
        if not ts or ts < since:
            continue
        event = rest.split(']', 1)[0].strip()
        detail = rest.split(']', 1)[1].strip() if ']' in rest else ''
        events.append((ts, event, detail))
    counts = Counter(event for _, event, _ in events)
    return {
        'count': len(events),
        'counts': counts,
        'recent': events[-12:],
    }



def collect_jsonl_events(path: Path, since: datetime, timestamp_keys: tuple[str, ...]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for line in _read_lines(path):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        ts: datetime | None = None
        for key in timestamp_keys:
            raw = obj.get(key)
            if isinstance(raw, (int, float)):
                ts = datetime.fromtimestamp(float(raw), tz=timezone.utc)
                break
            if isinstance(raw, str):
                ts = _parse_utc_ts(raw)
                if ts:
                    break
        if ts and ts < since:
            continue
        events.append(obj)
    return {
        'count': len(events),
        'events': events[-12:],
        'by_type': Counter(str(e.get('event') or e.get('action') or e.get('status') or 'unknown') for e in events),
    }



def collect_recent_reports(dirs: list[Path], since: datetime) -> list[Path]:
    candidates: list[Path] = []
    for base in dirs:
        if not base.exists():
            continue
        try:
            for p in base.glob('*'):
                try:
                    if p.is_file() and datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= since:
                        candidates.append(p)
                except Exception:
                    continue
        except Exception:
            continue
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return candidates[:10]


def collect_malware_report(path: Path, since: datetime) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict):
            return None
        ts = _parse_utc_ts(str(payload.get('timestamp', '') or ''))
        if ts and ts < since:
            return None
        return payload
    except Exception:
        return None


def collect_chkrootkit_report(path: Path, since: datetime) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict):
            return None
        ts = _parse_utc_ts(str(payload.get('timestamp', '') or ''))
        if ts and ts < since:
            return None
        return payload
    except Exception:
        return None


def collect_virus_report(path: Path, since: datetime) -> dict[str, Any] | None:
    # Kept separate for explicit "virus" reporting even if sourced from same scanner payload.
    return collect_malware_report(path, since)



def format_report(settings: dict[str, str], days: int, color: bool = False) -> tuple[str, dict[str, Any]]:
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=days)
    host = os.uname().nodename
    report_dir = DEFAULT_REPORT_DIR
    self_heal = collect_self_heal_events(DEFAULT_SELF_HEAL_LOG, window_start)
    priv = collect_jsonl_events(DEFAULT_PRIV_LOG, window_start, ('ts', 'timestamp'))
    audit = collect_jsonl_events(DEFAULT_AUDIT_LOG, window_start, ('timestamp', 'ts'))
    malware = collect_malware_report(DEFAULT_MALWARE_REPORT, window_start)
    virus = collect_virus_report(DEFAULT_VIRUS_REPORT, window_start)
    chkrootkit = collect_chkrootkit_report(DEFAULT_CHKROOTKIT_REPORT, window_start)
    recent_reports = collect_recent_reports(DEFAULT_HEALTH_DIRS, window_start)
    roots = _default_scan_roots()
    mounts = collect_mount_free_space()
    large_files = collect_large_files(roots)
    duplicates = collect_duplicate_files(roots)
    gpu = collect_gpu_usage()
    bleachbit = collect_bleachbit_status()

    def h1(t: str) -> str:
        return _colorize(t, _Ansi.BOLD + _Ansi.CYAN, color)

    def good(t: str) -> str:
        return _colorize(t, _Ansi.GREEN, color)

    def warn(t: str) -> str:
        return _colorize(t, _Ansi.YELLOW, color)

    def bad(t: str) -> str:
        return _colorize(t, _Ansi.RED, color)

    lines: list[str] = []
    lines.append(h1('HAL Weekly Changes & Fixes Report'))
    lines.append('=' * 60)
    lines.append(f'Host: {host}')
    lines.append(f'Window: {window_start.strftime("%Y-%m-%d %H:%M UTC")} to {window_end.strftime("%Y-%m-%d %H:%M UTC")} ({days} days)')
    lines.append(f'Self-heal policy: {settings.get("HAL_SELF_HEAL_MODE", "approval")}')
    lines.append(f'Weekly report email: {settings.get("HAL_WEEKLY_REPORT_EMAIL", "<not set>") or "<not set>"}')
    lines.append(f'Email backend: {settings.get("HAL_EMAIL_BACKEND", "auto")}')
    lines.append('')
    lines.append(h1('Summary'))
    lines.append('-' * 60)
    lines.append(f"Self-heal events: {self_heal['count']}")
    lines.append(f"Privileged actions: {priv['count']}")
    lines.append(f"Audit events: {audit['count']}")
    malware_findings = int((malware or {}).get('clamav', {}).get('infected_count', 0)) if malware else 0
    virus_findings = int((virus or {}).get('clamav', {}).get('infected_count', 0)) if virus else 0
    chk_findings = int((chkrootkit or {}).get('scan', {}).get('findings_count', 0)) if chkrootkit else 0
    lines.append(f"Malware findings: {bad(str(malware_findings)) if malware_findings else good('0')}")
    lines.append(f"Virus findings: {bad(str(virus_findings)) if virus_findings else good('0')}")
    lines.append(f"chkrootkit findings: {bad(str(chk_findings)) if chk_findings else good('0')}")
    lines.append(f'Recent health reports: {len(recent_reports)}')
    lines.append('')

    lines.append(h1('Self-Heal Events'))
    lines.append('-' * 60)
    if self_heal['count']:
        for event_name, count in self_heal['counts'].most_common():
            lines.append(f'  {event_name}: {count}')
        lines.append('')
        for ts, event_name, detail in self_heal['recent']:
            lines.append(f'  - {ts.strftime("%Y-%m-%d %H:%M UTC")} [{event_name}] {detail}')
    else:
        lines.append('  No self-heal events recorded in this window.')
    lines.append('')

    lines.append(h1('Privileged Actions'))
    lines.append('-' * 60)
    if priv['count']:
        for event_name, count in priv['by_type'].most_common():
            lines.append(f'  {event_name}: {count}')
        lines.append('')
        for item in priv['events'][-8:]:
            when = item.get('ts') or item.get('timestamp') or item.get('time') or ''
            lines.append(f"  - {when} {item.get('action') or item.get('status') or 'action'} {item.get('command') or item.get('tool') or ''}".rstrip())
    else:
        lines.append('  No privileged actions recorded in this window.')
    lines.append('')

    lines.append(h1('Audit Events'))
    lines.append('-' * 60)
    if audit['count']:
        for event_name, count in audit['by_type'].most_common():
            lines.append(f'  {event_name}: {count}')
        lines.append('')
        for item in audit['events'][-8:]:
            when = item.get('timestamp') or item.get('ts') or ''
            lines.append(f"  - {when} {item.get('event') or 'audit'}")
    else:
        lines.append('  No audit events recorded in this window.')
    lines.append('')

    lines.append(h1('Malware Scan'))
    lines.append('-' * 60)
    if malware:
        clamav = malware.get('clamav') or {}
        redhat = malware.get('redhat_malware') or {}
        lines.append(f"  ClamAV available: {clamav.get('available', False)}")
        inf = int(clamav.get('infected_count', 0) or 0)
        lines.append(f"  Infected files: {bad(str(inf)) if inf else good('0')}")
        for infected in (clamav.get('infected_files') or [])[:20]:
            lines.append(f'  - {infected}')
        lines.append(f"  Red Hat malware collector: {redhat.get('collector_ran', False)}")
        if redhat.get('detail'):
            lines.append(f"  Collector detail: {str(redhat.get('detail'))[:300]}")
    else:
        lines.append('  No recent malware scan report found.')
    lines.append('')

    lines.append(h1('Virus Scan'))
    lines.append('-' * 60)
    if virus:
        clamav_v = virus.get('clamav') or {}
        lines.append(f"  Scanner available: {clamav_v.get('available', False)}")
        infv = int(clamav_v.get('infected_count', 0) or 0)
        lines.append(f"  Infected files: {bad(str(infv)) if infv else good('0')}")
        for infected in (clamav_v.get('infected_files') or [])[:20]:
            lines.append(f'  - {infected}')
    else:
        lines.append('  No recent virus scan report found.')
    lines.append('')

    lines.append(h1('chkrootkit Scan'))
    lines.append('-' * 60)
    if chkrootkit:
        maint = chkrootkit.get('maintenance') or {}
        scan = chkrootkit.get('scan') or {}
        lines.append(f"  Installer attempted: {maint.get('attempted', False)}")
        lines.append(f"  Installer updated: {maint.get('updated', False)}")
        cf = int(scan.get('findings_count', 0) or 0)
        lines.append(f"  Findings: {bad(str(cf)) if cf else good('0')}")
        for finding in (scan.get('findings') or [])[:20]:
            lines.append(f'  - {finding}')
        if maint.get('detail'):
            lines.append(f"  Update detail: {str(maint.get('detail'))[:300]}")
    else:
        lines.append('  No recent chkrootkit report found.')
    lines.append('')

    lines.append(h1('Storage and GPU Report'))
    lines.append('-' * 60)
    lines.append('Drive free space (per mounted filesystem):')
    for row in mounts[:20]:
        pct = float(row.get('used_pct', 0.0) or 0.0)
        sev = bad if pct >= 90 else (warn if pct >= 75 else good)
        lines.append(
            f"  - {row['mount']}: used {sev(f'{pct:.1f}%')} "
            f"({ _human_bytes(int(row['used'])) } / { _human_bytes(int(row['total'])) }), free { _human_bytes(int(row['free'])) }"
        )
    if not mounts:
        lines.append('  No mount information available.')

    lines.append('')
    lines.append('Large files:')
    if large_files:
        for item in large_files:
            lines.append(f"  - {_human_bytes(int(item['size']))}  {item['path']}")
    else:
        lines.append('  No large files above threshold were found in scanned paths.')

    lines.append('')
    lines.append('Duplicate files:')
    if duplicates:
        for grp in duplicates:
            lines.append(
                f"  - size {_human_bytes(int(grp['size']))}, copies {grp['count']}, hash {grp['hash']}"
            )
            for p in grp['paths']:
                lines.append(f'    {p}')
    else:
        lines.append('  No duplicate file groups found in scanned paths.')

    lines.append('')
    lines.append('GPU usage:')
    if gpu.get('available'):
        for row in gpu.get('rows', []):
            util = row.get('gpu_pct', '0')
            try:
                utilf = float(util)
            except Exception:
                utilf = 0.0
            sev = bad if utilf >= 90 else (warn if utilf >= 70 else good)
            lines.append(
                f"  - {row.get('name', 'GPU')}: util {sev(str(util) + '%')}, "
                f"VRAM {row.get('mem_used_mb', '?')}/{row.get('mem_total_mb', '?')} MB"
            )
    else:
        lines.append('  No GPU usage source detected (nvidia-smi unavailable).')
    lines.append('')
    lines.append('BleachBit:')
    if bleachbit.get('installed'):
        lines.append(f"  Installed: {good('yes')} ({bleachbit.get('path', '')})")
        lines.append(f"  Source: {bleachbit.get('source', 'native')}")
        lines.append(f"  Version: {bleachbit.get('version', 'unknown')}")
    else:
        lines.append(f"  Installed: {warn('no')} (optional)")
    lines.append('')

    lines.append(h1('Recent Health Reports'))
    lines.append('-' * 60)
    if recent_reports:
        for p in recent_reports:
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            except Exception:
                mtime = 'unknown time'
            lines.append(f'  - {mtime} {p}')
    else:
        lines.append('  No recent health reports found.')
    lines.append('')
    lines.append(h1('Notes'))
    lines.append('-' * 60)
    lines.append('  This report is generated from the local self-heal, audit, and privileged-action logs.')
    lines.append('  Review the recent health reports and audit log entries for exact fix details.')

    return '\n'.join(lines), {
        'window_start': window_start,
        'window_end': window_end,
        'report_dir': report_dir,
        'self_heal': self_heal,
        'priv': priv,
        'audit': audit,
        'malware': malware,
        'virus': virus,
        'chkrootkit': chkrootkit,
        'mounts': mounts,
        'large_files': large_files,
        'duplicates': duplicates,
        'gpu': gpu,
        'bleachbit': bleachbit,
        'recent_reports': recent_reports,
    }



def choose_mail_command(preferred: str = 'auto') -> tuple[str | None, str | None]:
    preferred = (preferred or 'auto').strip().lower()
    candidates = []
    if preferred and preferred != 'auto':
        candidates.append(preferred)
    candidates.extend(['mailx', 'mutt', 'mail', 'sendmail'])
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        path = shutil.which(candidate)
        if path:
            return path, candidate
    return None, None



def send_email(recipient: str, subject: str, body: str, preferred_backend: str = 'auto') -> tuple[bool, str]:
    cmd, backend = choose_mail_command(preferred_backend)
    if not cmd:
        return False, 'No mail backend found (mailx, mutt, mail, sendmail)'

    try:
        if backend == 'sendmail':
            payload = f"To: {recipient}\nSubject: {subject}\nContent-Type: text/plain; charset=UTF-8\n\n{body}\n"
            result = subprocess.run([cmd, '-t', '-oi'], input=payload, text=True, capture_output=True, timeout=30)
        elif backend == 'mutt':
            result = subprocess.run([cmd, '-s', subject, '--', recipient], input=body, text=True, capture_output=True, timeout=30)
        else:
            result = subprocess.run([cmd, '-s', subject, recipient], input=body, text=True, capture_output=True, timeout=30)
        if result.returncode == 0:
            return True, backend or 'mail'
        return False, (result.stderr or result.stdout or '').strip() or f'{backend} returned {result.returncode}'
    except Exception as exc:
        return False, str(exc)



def main() -> int:
    parser = argparse.ArgumentParser(description='Generate and optionally email a weekly HAL report')
    parser.add_argument('--days', type=int, default=None, help='Lookback window in days (default: HAL_WEEKLY_REPORT_DAYS or 7)')
    parser.add_argument('--recipient', help='Email recipient (default: HAL_WEEKLY_REPORT_EMAIL from env file)')
    parser.add_argument('--subject', help='Email subject (default derived from hostname)')
    parser.add_argument('--no-email', action='store_true', help='Generate report but do not send email')
    parser.add_argument('--stdout', action='store_true', help='Print the report to stdout')
    parser.add_argument('--no-color', action='store_true', help='Disable ANSI colors in stdout report output')
    parser.add_argument('--export-path', help='Path to export the report bundle (file or directory). Extension infers format (.zip, .tar.gz, .tgz, .tar, .json, .txt).')
    parser.add_argument('--export-format', choices=['zip', 'tar', 'tgz', 'json', 'dir', 'txt', 'copy'], help='Force export format. If omitted, inferred from --export-path.')
    args = parser.parse_args()

    settings = load_settings()
    try:
        days = int(args.days or settings.get('HAL_WEEKLY_REPORT_DAYS', '7') or 7)
    except Exception:
        days = 7
    recipient = (args.recipient or settings.get('HAL_WEEKLY_REPORT_EMAIL', '')).strip()
    backend = (settings.get('HAL_EMAIL_BACKEND', 'auto') or 'auto').strip().lower()
    report, meta = format_report(settings, days, color=False)

    report_dir = meta['report_dir']
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d')
    output_path = report_dir / f'weekly-report-{stamp}.txt'
    output_path.write_text(report + '\n', encoding='utf-8')
    print(f'Report written to {output_path}')

    # If export options were provided, create an export bundle
    if args.export_path or args.export_format:
        exported = _export_bundle(meta, output_path, args.export_path, args.export_format)
        if exported:
            print(f'Export written to {exported}')
        else:
            print('ERROR: report export failed')

    def _infer_format_from_path(p: str | Path) -> str | None:
        if not p:
            return None
        s = str(p).lower()
        if s.endswith('.zip'):
            return 'zip'
        if s.endswith('.tar.gz') or s.endswith('.tgz'):
            return 'tgz'
        if s.endswith('.tar'):
            return 'tar'
        if s.endswith('.json'):
            return 'json'
        if s.endswith('.txt'):
            return 'txt'
        # directory-like
        if s.endswith(os.path.sep) or s.endswith('/'):
            return 'dir'
        return None

    def _export_bundle(meta: dict[str, Any], out_text: Path, target: str | None, fmt: str | None) -> Path | None:
        # Determine target path and format
        target_path: Path | None = Path(target) if target else None
        if fmt is None and target_path is not None:
            fmt = _infer_format_from_path(target_path)
        if fmt is None and target_path is None:
            fmt = 'zip'
            target_path = meta.get('report_dir', report_dir) / f'weekly-report-{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}.zip'
        if target_path is None:
            # choose default filename
            target_path = meta.get('report_dir', report_dir) / f'weekly-report-{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}.zip'
        # Normalize formats
        fmt = (fmt or '').lower()

        # Collect source files to include
        sources: list[Path] = []
        try:
            if out_text and out_text.exists():
                sources.append(out_text)
        except Exception:
            pass

        for p in (DEFAULT_MALWARE_REPORT, DEFAULT_VIRUS_REPORT, DEFAULT_CHKROOTKIT_REPORT, DEFAULT_SELF_HEAL_LOG, DEFAULT_PRIV_LOG, DEFAULT_AUDIT_LOG):
            try:
                if p and p.exists():
                    sources.append(p)
            except Exception:
                continue

        for p in list(meta.get('recent_reports', []) or []):
            try:
                if isinstance(p, Path) and p.exists():
                    sources.append(p)
            except Exception:
                continue

        # Deduplicate
        uniq: dict[str, Path] = {}
        for s in sources:
            try:
                uniq[str(s.resolve())] = s
            except Exception:
                uniq[str(s)] = s
        sources = list(uniq.values())

        # Ensure parent exists
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Create exports
        if fmt == 'json':
            payload: dict[str, Any] = {
                'report_text': out_text.read_text(encoding='utf-8') if out_text.exists() else report,
                'malware': meta.get('malware'),
                'virus': meta.get('virus'),
                'chkrootkit': meta.get('chkrootkit'),
                'metadata': {
                    'window_start': str(meta.get('window_start')),
                    'window_end': str(meta.get('window_end')),
                }
            }
            target_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
            return target_path

        if fmt in ('dir',):
            d = target_path
            d.mkdir(parents=True, exist_ok=True)
            for s in sources:
                try:
                    shutil.copy2(s, d / s.name)
                except Exception:
                    try:
                        shutil.copy(s, d / s.name)
                    except Exception:
                        continue
            # manifest
            try:
                manifest = {'files': [p.name for p in d.iterdir()]}
                (d / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
            except Exception:
                pass
            return d

        if fmt in ('txt', 'copy'):
            # Copy the primary text report to the target
            tp = target_path
            if tp.is_dir():
                tp = tp / out_text.name
            try:
                shutil.copy2(out_text, tp)
                return tp
            except Exception:
                try:
                    shutil.copy(out_text, tp)
                    return tp
                except Exception:
                    return None

        # default: archive (zip or tar/tgz)
        items: list[tuple[Path, str]] = []
        name_set: set[str] = set()
        for s in sources:
            arcname = s.name
            if arcname in name_set:
                i = 1
                base = arcname
                while f'{base}.{i}' in name_set:
                    i += 1
                arcname = f'{base}.{i}'
            name_set.add(arcname)
            items.append((s, arcname))

        if fmt == 'zip' or fmt == '':
            try:
                with zipfile.ZipFile(target_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                    for src, arc in items:
                        try:
                            zf.write(str(src), arc)
                        except Exception:
                            continue
                return target_path
            except Exception:
                return None

        if fmt in ('tgz', 'tar'):
            mode = 'w:gz' if fmt == 'tgz' else 'w'
            try:
                with tarfile.open(str(target_path), mode) as tf:
                    for src, arc in items:
                        try:
                            tf.add(str(src), arc)
                        except Exception:
                            continue
                return target_path
            except Exception:
                return None

        return None

    if args.stdout:
        print()
        report_colored, _ = format_report(settings, days, color=(not args.no_color and _color_enabled()))
        print(report_colored)

    if args.no_email:
        return 0

    if not recipient:
        print('No report recipient configured; skipping email send.')
        return 0

    subject = args.subject or f'HAL weekly report: {os.uname().nodename}'
    ok, backend_used = send_email(recipient, subject, report, preferred_backend=backend)
    if ok:
        print(f'Email sent to {recipient} via {backend_used}')
        return 0

    print(f'WARNING: email send failed for {recipient}: {backend_used}')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
