#!/usr/bin/env python3
"""Simple secret scanner for the repository.

Scans for common secret patterns (GitHub tokens, AWS keys, private keys,
inline passwords, API keys). This is intended as a lightweight preflight
tool and is NOT a replacement for more advanced secret-scanning tooling.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS = [
    (re.compile(r"ghp_[A-Za-z0-9_\-]+"), "GitHub PAT (ghp_*)"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id (AKIA...)"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key header"),
    (re.compile(r"password\s*[:=]\s*['\"]?[^'\"]{4,}['\"]?", re.I), "Inline password assignment"),
    (re.compile(r"token\s*[:=]\s*['\"]?[^'\"]{6,}['\"]?", re.I), "Inline token"),
    (re.compile(r"api[_-]?key\s*[:=]\s*['\"]?[^'\"]{6,}['\"]?", re.I), "API key"),
    (re.compile(r"client[_-]?secret\b", re.I), "client_secret reference"),
]


def scan():
    found = []
    for p in ROOT.rglob('*'):
        if not p.is_file():
            continue
        # skip virtualenvs and git dirs
        if any(part.startswith('.venv') or part == 'venv' for part in p.parts):
            continue
        if '/.git/' in str(p):
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            for regex, desc in PATTERNS:
                if regex.search(line):
                    found.append((p.relative_to(ROOT), i, line.strip(), desc))
    return found


def main():
    hits = scan()
    if not hits:
        print('No likely secrets found (quick scan).')
        return 0
    print('Possible secrets found:')
    for f, ln, line, desc in hits:
        print(f'  {f}:{ln}: {desc} -> {line}')
    print('\nReview each match; false positives are possible.')
    return 2


if __name__ == '__main__':
    sys.exit(main())
