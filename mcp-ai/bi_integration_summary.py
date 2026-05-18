#!/usr/bin/env python3
"""Summarize training documents for BI-related keywords.

Writes a CSV with file, path, matched keyword and an excerpt to help find useful docs.
"""
import os
import re
import json
import csv
import argparse


def find_docs(training_dir, keywords):
    out = []
    if not os.path.isdir(training_dir):
        raise SystemExit(f"Training dir not found: {training_dir}")
    for fname in os.listdir(training_dir):
        if not fname.endswith('.json'):
            continue
        path = os.path.join(training_dir, fname)
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                j = json.load(fh)
        except Exception:
            continue
        text = j.get('text') or j.get('content') or ''
        if not isinstance(text, str):
            text = str(text)
        for kw in keywords:
            if re.search(kw, text, re.I):
                excerpt = text.replace('\n', ' ')[:400]
                out.append({'file': fname, 'path': path, 'keyword': kw, 'excerpt': excerpt})
                break
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--training-dir', default=os.path.expanduser('~/.mcp-ai/training'))
    p.add_argument('--keywords', nargs='+', default=['linkedin', 'company', 'employee', 'profile', 'contact', 'email', 'phone', 'address', 'linkedin.com'])
    p.add_argument('--out', default='bi_summary.csv')
    args = p.parse_args()

    rows = find_docs(args.training_dir, args.keywords)
    with open(args.out, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=['file', 'path', 'keyword', 'excerpt'])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"Wrote {len(rows)} matches to {args.out}")


if __name__ == '__main__':
    main()
