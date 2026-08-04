#!/usr/bin/env python3
"""Restore the most recent backup created by `remediate.py`.

Usage: rollback.py [--list] [--restore <file>] [--yes]

Note: This script uses `sudo` to extract the backup into `/` and MUST be run by
an administrator who understands the impact.
"""
import argparse, os, subprocess
from pathlib import Path

BACKUP_DIR = '/var/lib/mcp/backups'


def list_backups():
    p = Path(BACKUP_DIR)
    if not p.exists():
        print('No backups found')
        return
    for f in sorted(p.glob('etc-backup-*.tar.gz')):
        print(f)


def restore(file, yes=False):
    if not os.path.exists(file):
        print('Backup not found:', file)
        return 2
    if not yes:
        resp = input(f"About to restore {file} into / (destructive). Continue? [y/N]: ")
        if resp.lower() != 'y':
            print('Aborted by user')
            return 1
    try:
        proc = subprocess.run(['sudo', 'tar', '-xzf', file, '-C', '/'], check=False)
        return proc.returncode
    except Exception as e:
        print('Restore failed:', e)
        return 3


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--restore')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if args.list:
        list_backups(); exit(0)
    if args.restore:
        exit(restore(args.restore, yes=args.yes))
    ap.print_help()
