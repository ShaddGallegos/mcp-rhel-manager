#!/usr/bin/env python3
"""Simple CLI to approve plan files created by remediate.py

Usage:
  approve.py --list
  approve.py --plan /path/to/plan-file.json --approve --approver dave
"""
import argparse
import json
import os
from pathlib import Path
import getpass
from datetime import datetime, timezone

HOME = os.path.expanduser('~')
AI_HOME = os.path.join(HOME, '.mcp-ai')
APPROVALS = os.path.join(AI_HOME, 'approvals')

os.makedirs(APPROVALS, exist_ok=True)


def list_plans():
    fixes = Path(AI_HOME) / 'fixes'
    if not fixes.exists():
        print('No plans found')
        return
    for p in sorted(fixes.glob('plan-*.json')):
        print(p)


def approve(plan_path, approver=None):
    if not os.path.exists(plan_path):
        print('Plan not found:', plan_path)
        return 2
    if approver is None:
        approver = getpass.getuser()
    with open(plan_path, 'r', encoding='utf-8') as fh:
        plan = json.load(fh)
    base = Path(plan_path).stem
    out = os.path.join(APPROVALS, f'{base}.approved.json')
    payload = {
        'plan': plan_path,
        'approver': approver,
        'ts': datetime.now(timezone.utc).isoformat() + 'Z'
    }
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2)
    print('Plan approved:', out)
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--plan')
    ap.add_argument('--approve', action='store_true')
    ap.add_argument('--approver')
    args = ap.parse_args()
    if args.list:
        list_plans()
        exit(0)
    if args.plan and args.approve:
        exit(approve(args.plan, args.approver))
    ap.print_help()
