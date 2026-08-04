#!/usr/bin/env python3
"""Move Ansible-vault-encrypted plan files into a quarantine subdirectory.

Usage: python3 scripts/quarantine_encrypted_plans.py
"""
import os
import shutil
from pathlib import Path

try:
    import mcp_config as cfg
except Exception:
    cfg = None

def get_fixes_dir():
    home = os.path.expanduser('~')
    if cfg is not None:
        return os.path.expanduser(getattr(cfg, 'FIXES_DIR', os.path.join(home, '.ansible', '.trainingdata', 'fixes')))
    return os.path.join(home, '.ansible', '.trainingdata', 'fixes')

def main():
    fixes = Path(get_fixes_dir())
    if not fixes.exists():
        print('No fixes dir found:', fixes)
        return
    qdir = fixes / 'quarantine'
    qdir.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in sorted(fixes.glob('plan-*.json')):
        try:
            with open(p, 'rb') as fh:
                hdr = fh.read(16)
                if not hdr.startswith(b'$ANSIBLE_VAULT;'):
                    continue
        except Exception:
            continue
        target = qdir / (p.name + '.quarantined')
        shutil.move(str(p), str(target))
        moved.append((str(p), str(target)))
    if not moved:
        print('No encrypted plans found to quarantine.')
    else:
        print('Quarantined files:')
        for s,t in moved:
            print(s, '->', t)

if __name__ == '__main__':
    main()
