#!/usr/bin/env python3
"""Create a simulated training entry and an accompanying raw log file.

This helps test the remediator without needing a live LLM by creating a matching suggestion file.
"""
import os, json, argparse
from pathlib import Path
from datetime import datetime

HOME = os.path.expanduser('~')
AI_HOME = os.path.join(HOME, '.mcp-ai')
TRAIN_DIR = os.path.join(AI_HOME, 'training')
FIXES_DIR = os.path.join(AI_HOME, 'fixes')

os.makedirs(TRAIN_DIR, exist_ok=True)
os.makedirs(FIXES_DIR, exist_ok=True)


def make_entry(name='kaso.prod.spg'):
    ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    raw = os.path.join(TRAIN_DIR, f'raw-{name}-{ts}.log')
    entry_path = os.path.join(TRAIN_DIR, f'entry-{name}-{ts}.jsonl')
    # sample raw log
    with open(raw, 'w') as fh:
        fh.write('ERROR: NVMe SMART warning: Percentage Used: 45%\n')
        fh.write('Jan 01 Kernel panic - test')
    entry = {
        'host': name,
        'timestamp': ts,
        'problem_count': 2,
        'raw_log': raw
    }
    with open(entry_path, 'w') as fh:
        json.dump(entry, fh)
    # create a dummy suggestion (so remediator can run without LLM)
    suggestion = {
        'solutions': [
            {'id': 's1', 'commands': ['mcp-call://server.hardware_diagnostics']}
        ]
    }
    sugg_path = os.path.join(FIXES_DIR, f'suggestion-{Path(entry_path).stem}.json')
    with open(sugg_path, 'w') as fh:
        json.dump({'entry': entry_path, 'suggestion': suggestion}, fh, indent=2)
    print('Created entry:', entry_path)
    print('Created suggestion:', sugg_path)

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--name', default='kaso.prod.spg')
    args = ap.parse_args()
    make_entry(args.name)
