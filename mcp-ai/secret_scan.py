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
    # Only flag explicit inline assignments like `password = '...'` (require word boundary)
    (re.compile(r"\bpassword\b\s*[:=]\s*['\"][^'\"]{4,}['\"]", re.I), "Inline password assignment"),
    # Require a standalone `token = '...'` (avoid matching `refresh_token` etc.)
    (re.compile(r"\btoken\b\s*[:=]\s*['\"][^'\"]{6,}['\"]", re.I), "Inline token"),
    # Require explicit quoted api keys
    (re.compile(r"\bapi[_-]?key\b\s*[:=]\s*['\"][^'\"]{6,}['\"]", re.I), "API key"),
    (re.compile(r"client[_-]?secret\b", re.I), "client_secret reference"),
]


def scan():
    found = []
    # If specific files are provided via argv, only scan those (pre-commit passes files).
    args = sys.argv[1:]
    if args:
        files = [ROOT.joinpath(a) for a in args]
    else:
        files = list(ROOT.rglob('*'))

    EXCLUDE_TOPDIRS = {'docs', '.hal_suggestions', 'packaging', 'monitoring', 'context', '.devcontainer'}

    for p in files:
        if not p.is_file():
            continue
        # skip virtualenvs and git dirs
        if any(part.startswith('.venv') or part == 'venv' for part in p.parts):
            continue
        if '/.git/' in str(p):
            continue
        # skip binary/compiled files
        if p.suffix.lower() in ('.pyc', '.png', '.jpg', '.jpeg', '.gif', '.db', '.sqlite'):
            continue

        # Exclude documentation, examples, and tooling scaffolds from secret scanning
        try:
            rel = p.relative_to(ROOT)
            if rel.parts and rel.parts[0] in EXCLUDE_TOPDIRS:
                continue
            # Avoid scanning this scanner script itself
            if rel.name == 'secret_scan.py':
                continue
            if str(rel).lower().endswith('env.yml.example'):
                continue
            if rel.name in ('README.md', 'README.rst'):
                continue
        except Exception:
            pass

        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            # Allow authors to opt-out of scanning a specific line
            if '# nosec' in line:
                continue
            # Ignore obvious example placeholders
            low = line.lower()
            if 'your-' in low or 'changeme' in low or '<your_' in low:
                continue
            # Ignore interactive prompts (safe)
            if 'read -s -p' in line or 'input(' in line:
                continue
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
