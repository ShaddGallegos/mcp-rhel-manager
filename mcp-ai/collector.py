#!/usr/bin/env python3
"""MCP AI: log collector -> produces raw logs and JSONL training entries.

Usage:
  collector.py --run    # invoked by systemd timer
  collector.py --once   # run once and exit
  collector.py --push   # run once and then invoke remediator (if available)
"""
import os
import sys
import json
import socket
import argparse
import subprocess
from datetime import datetime, timezone

HOME = os.path.expanduser('~')
AI_HOME = os.path.join(HOME, '.mcp-ai')
RAW_DIR = os.path.join(AI_HOME, 'raw-logs')
TRAIN_DIR = os.path.join(AI_HOME, 'training')
FIXES_DIR = os.path.join(AI_HOME, 'fixes')
REPORTS_DIR = os.path.join(AI_HOME, 'reports')

def ensure_dirs():
    for d in (RAW_DIR, TRAIN_DIR, FIXES_DIR, REPORTS_DIR):
        os.makedirs(d, exist_ok=True)

def run_cmd(cmd, timeout=60):
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout + proc.stderr
    except Exception as e:
        return 1, f"ERR: {str(e)}"

def collect_once():
    ensure_dirs()
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    host = socket.gethostname()
    raw_path = os.path.join(RAW_DIR, f'{host}-{ts}.log')

    probes = [
        ('journal_err', 'journalctl -b -p err..emerg --no-pager -n 1000'),
        ('journal_warn', 'journalctl -b -p warning --no-pager -n 1000'),
        ('systemctl_failed', 'systemctl --failed --no-legend --no-pager'),
        ('dmesg', 'dmesg -T | tail -n 400'),
        ('uname', 'uname -a'),
        ('df', 'df -h'),
    ]

    with open(raw_path, 'w', encoding='utf-8') as fh:
        fh.write(f'# MCP AI Raw Log Capture: {ts} {host}\n')
        for name, cmd in probes:
            fh.write('\n' + '='*20 + f' {name} ({cmd}) ' + '='*20 + '\n')
            rc, out = run_cmd(cmd)
            fh.write(out if out else f'<no-output rc={rc}>\n')

    # simple heuristics for training metadata
    with open(raw_path, 'r', encoding='utf-8') as fh:
        raw_text = fh.read()

    problem_count = raw_text.lower().count('error') + raw_text.lower().count('failed')
    summary = raw_text[:1000].replace('\n', '\\n')

    entry = {
        'timestamp': ts,
        'host': host,
        'raw_log': raw_path,
        'problem_count': problem_count,
        'summary': summary,
    }

    jsonl_path = os.path.join(TRAIN_DIR, f'entry-{host}-{ts}.jsonl')
    with open(jsonl_path, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(entry))

    print(f'Collected raw logs -> {raw_path}')
    print(f'Wrote training entry -> {jsonl_path}')
    return jsonl_path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--push', action='store_true')
    args = ap.parse_args()

    # load config
    cfg_path = os.path.join(HOME, '.mcp-ai', 'config.json')
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {}

    if args.once or args.push:
        entry = collect_once()
        # optional: invoke remediator if requested or configured
        do_auto = cfg.get('auto_remediate', False)
        allow_fix = cfg.get('allow_auto_fix', False)
        if args.push or do_auto:
            rem = os.path.join(os.path.dirname(__file__), 'remediate.py')
            if os.path.isfile(rem):
                cmd = ["/usr/bin/env", "python3", rem, "--input", entry]
                # decide exec mode
                if allow_fix:
                    cmd.append("--exec")
                env = os.environ.copy()
                if allow_fix:
                    env['ALLOW_AUTO_FIX'] = '1'
                subprocess.run(cmd, env=env)
        return 0

    if args.run:
        # run once for systemd timer invocation
        entry = collect_once()
        # honor auto_remediate config for timer runs
        do_auto = cfg.get('auto_remediate', False)
        allow_fix = cfg.get('allow_auto_fix', False)
        if do_auto:
            rem = os.path.join(os.path.dirname(__file__), 'remediate.py')
            if os.path.isfile(rem):
                cmd = ["/usr/bin/env", "python3", rem, "--input", entry]
                if allow_fix:
                    cmd.append("--exec")
                env = os.environ.copy()
                if allow_fix:
                    env['ALLOW_AUTO_FIX'] = '1'
                subprocess.run(cmd, env=env)
        return 0

    ap.print_help()

if __name__ == '__main__':
    sys.exit(main())
