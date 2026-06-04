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
        # Optionally generate unified patch files using LLM and apply them + create PR
        if os.environ.get('HAL_AUTO_IMPROVE_GENERATE_PATCHES', '').lower() in ('1', 'true', 'yes'):
            try:
                print('Generating patches via LLM for suggested files...')
                for rel in list(report.keys()):
                    target = root.joinpath(rel)
                    if not target.exists():
                        continue
                    tmp_out = patches_dir.joinpath(rel + '.llm.json')
                    tmp_out.parent.mkdir(parents=True, exist_ok=True)
                    cmd = [sys.executable, str(root.joinpath('scripts', 'llm_suggest.py')), '--expert', 'code_fixer', '--file', str(target), '--out', str(tmp_out)]
                    try:
                        import subprocess
                        subprocess.run(cmd, check=False)
                        if tmp_out.exists():
                            data = json.loads(tmp_out.read_text(encoding='utf-8'))
                            out = data.get('output', '')
                            if out and (out.strip().startswith('diff --git') or out.strip().startswith('***') or out.strip().startswith('---')):
                                patch_fp = patches_dir.joinpath(rel + '.diff')
                                patch_fp.parent.mkdir(parents=True, exist_ok=True)
                                patch_fp.write_text(out)
                                print('Wrote patch:', patch_fp)
                    except Exception as e:
                        print('LLM patch generation failed for', rel, e)
            except Exception as e:
                print('Patch generation step failed:', e)

        # If requested, apply any generated patches and create a PR
        if os.environ.get('HAL_AUTO_IMPROVE_APPLY_PATCHES', '').lower() in ('1', 'true', 'yes'):
            try:
                apply_script = root.joinpath('scripts', 'apply_patches_and_create_pr.sh')
                if apply_script.exists():
                    print('Applying patches and creating PR (auto-enabled)')
                    import subprocess
                    create_pr_flag = ['--create-pr'] if os.environ.get('HAL_AUTO_IMPROVE_CREATE_PR', '').lower() in ('1', 'true', 'yes') else []
                    subprocess.run([str(apply_script), '--repo', str(root), '--patch-dir', str(patches_dir), '--title', 'HAL auto-improve patches'] + create_pr_flag, check=False)
                else:
                    print('apply_patches_and_create_pr.sh not found; skipping auto-apply')
            except Exception as e:
                print('Failed to apply patches:', e)

        # Optionally create a PR from suggestion files when explicitly enabled via env var
        if os.environ.get('HAL_AUTO_IMPROVE_CREATE_PR', '').lower() in ('1', 'true', 'yes'):
            try:
                cp_script = root.joinpath('scripts', 'create_pr_from_suggestions.sh')
                if cp_script.exists():
                    print('Creating PR from suggestions (auto-enabled)')
                    import subprocess
                    subprocess.run([str(cp_script), '--repo', str(root), '--src-dir', str(patches_dir), '--create-pr'], check=False)
                else:
                    print('create_pr_from_suggestions.sh not found; skipping PR creation')
            except Exception as e:
                print('Failed to create PR from suggestions:', e)


if __name__ == '__main__':
    main()
