#!/usr/bin/env python3
"""
hal_auto_improve.py

Conservative autosuggest scaffold for HAL self-improvement.

Scans Python files for simple heuristics (missing docstrings, top-level print(),
bare except:, and TODO comments) and writes suggestion reports to
.hal_suggestions/ (dry-run by default). Use `--apply` to emit suggestion files
that a human can review before applying.
"""
import argparse
import ast
import json
import os
import time
from pathlib import Path


def should_skip(path: Path, excludes: set[str]) -> bool:
    for part in path.parts:
        if part in excludes:
            return True
    return False


def analyze_file(fp: Path) -> list[dict]:
    suggestions: list[dict] = []
    try:
        src = fp.read_text(encoding='utf-8')
    except Exception as e:
        suggestions.append({'lineno': 0, 'type': 'read_error', 'detail': str(e)})
        return suggestions

    # TODO comments
    for i, line in enumerate(src.splitlines(), start=1):
        if 'TODO' in line.upper():
            suggestions.append({'lineno': i, 'type': 'todo', 'detail': line.strip()})

    # AST checks
    try:
        tree = ast.parse(src)
    except Exception as e:
        suggestions.append({'lineno': 0, 'type': 'parse_error', 'detail': str(e)})
        return suggestions

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if ast.get_docstring(node) is None:
                suggestions.append({'lineno': getattr(node, 'lineno', 0), 'type': 'missing_docstring', 'detail': getattr(node, 'name', '<anon>')})
        if isinstance(node, ast.Call):
            # detect bare print() usage
            func = node.func
            if isinstance(func, ast.Name) and func.id == 'print':
                suggestions.append({'lineno': getattr(node, 'lineno', 0), 'type': 'print_usage', 'detail': 'consider using logging instead of print()'})
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                suggestions.append({'lineno': getattr(node, 'lineno', 0), 'type': 'bare_except', 'detail': 'use specific exception types'})

    return suggestions


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', default='.', help='Repository root to scan')
    p.add_argument('--apply', action='store_true', help='Emit suggestion files for easy review')
    p.add_argument('--max-files', type=int, default=5000)
    args = p.parse_args()

    root = Path(args.root).resolve()
    excludes = set(('venv', '.venv', '.git', '__pycache__', '.mcp-ai', 'venv-bridge'))

    out_dir = root.joinpath('.hal_suggestions')
    out_dir.mkdir(parents=True, exist_ok=True)
    patches_dir = out_dir.joinpath('patches')
    if args.apply:
        patches_dir.mkdir(parents=True, exist_ok=True)

    report = {}
    scanned = 0
    for fp in root.rglob('*.py'):
        if scanned >= args.max_files:
            break
        if should_skip(fp.relative_to(root), excludes):
            continue
        # skip our own suggestions dir
        if out_dir in fp.parents:
            continue
        scanned += 1
        suggestions = analyze_file(fp)
        if suggestions:
            rel = str(fp.relative_to(root))
            report[rel] = suggestions
            if args.apply:
                patch_fp = patches_dir.joinpath(rel + '.suggest.txt')
                patch_fp.parent.mkdir(parents=True, exist_ok=True)
                with patch_fp.open('w', encoding='utf-8') as fh:
                    fh.write('Suggestions for ' + rel + '\n')
                    for s in suggestions:
                        fh.write(f"- line {s.get('lineno')}: {s.get('type')} - {s.get('detail')}\n")

    ts = time.strftime('%Y%m%dT%H%M%SZ')
    out_fp = out_dir.joinpath(f'suggestions-{ts}.json')
    with out_fp.open('w', encoding='utf-8') as fh:
        json.dump({'scanned': scanned, 'report': report}, fh, indent=2)

    print(f'Analyzed {scanned} Python files; suggestions written to {out_fp}')
    if args.apply:
        print(f'Suggestion files created under {patches_dir}')


if __name__ == '__main__':
    main()
