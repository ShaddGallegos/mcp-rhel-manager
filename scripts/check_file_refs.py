#!/usr/bin/env python3
"""Scan repo for referenced .sh/.yml/.yaml files in source files and report missing ones."""
import re
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAT = re.compile(r"['\"]([^'\"]+\.(?:sh|yml|yaml))['\"]")

# Only scan these file types to reduce noise
SCAN_SUFFIXES = {'.py', '.sh', '.md', '.service', '.txt', '.rst', '.yaml', '.yml', '.ini'}

refs = set()
for p in ROOT.rglob('*'):
    if not p.is_file():
        continue
    if p.suffix.lower() not in SCAN_SUFFIXES:
        continue
    try:
        txt = p.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        continue
    for m in PAT.finditer(txt):
        refs.add(m.group(1))

missing = []
for r in sorted(refs):
    # skip obvious non-literal or templated references
    if any(x in r for x in ('$', '{', '}', '*', '?', '%')):
        continue
    if r.lower().startswith('http'):
        continue
    # skip URL-like or strings with spaces (not literal path)
    if '://' in r or ' ' in r:
        continue
    # require the string to be a path-like literal: contain a '/' or start with './' or be absolute
    if not (r.startswith('./') or os.path.isabs(r) or '/' in r):
        continue
    # resolve relative to repo root
    path = Path(r) if os.path.isabs(r) else (ROOT / r.lstrip('./'))
    if not path.exists():
        missing.append(r)

print(f"Checked {len(refs)} referenced files; missing: {len(missing)}")
if missing:
    for m in missing[:200]:
        print("MISSING:", m)

# Exit with non-zero if missing files found
import sys
if missing:
    sys.exit(2)
