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
import subprocess
import sys
import shutil
import re
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
    p.add_argument('--generate-patches', action='store_true', help='Call LLM to generate patch suggestions for files with findings')
    p.add_argument('--patch-expert', default='code_fixer', help='Expert name to use for patch generation (llm_suggest.py --expert)')
    p.add_argument('--autonomous', action='store_true', help='Run full autonomous cycle: generate patches, apply, validate, rollback on failure')
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
        # Optionally generate unified patch files using LLM
        if args.generate_patches or os.environ.get('HAL_AUTO_IMPROVE_GENERATE_PATCHES', '').lower() in ('1', 'true', 'yes'):
            try:
                print('Generating patches via LLM for suggested files...')
                for rel in list(report.keys()):
                    target = root.joinpath(rel)
                    if not target.exists():
                        continue
                    tmp_out = patches_dir.joinpath(rel + '.llm.json')
                    tmp_out.parent.mkdir(parents=True, exist_ok=True)
                    cmd = [sys.executable, str(root.joinpath('scripts', 'llm_suggest.py')), '--expert', args.patch_expert, '--file', str(target), '--out', str(tmp_out)]
                    try:
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

        # If requested, apply any generated patches and create a PR or run autonomous cycle
        if args.autonomous or os.environ.get('HAL_AUTO_IMPROVE_APPLY_PATCHES', '').lower() in ('1', 'true', 'yes'):
            # Safety gate: require explicit env var to allow modifications
            allow_apply = os.environ.get('HAL_ALLOW_AUTO_IMPROVE_APPLY', '').lower() in ('1', 'true', 'yes')
            if not allow_apply:
                print('Auto-apply requested but HAL_ALLOW_AUTO_IMPROVE_APPLY not set; skipping apply for safety.')
            else:
                print('Applying generated patches (with backups, will validate and rollback on failure)')
                applied = []
                failed = []
                ts = time.strftime('%Y%m%dT%H%M%SZ')
                backup_root = out_dir.joinpath('backups', ts)
                backup_root.mkdir(parents=True, exist_ok=True)

                def parse_patch_targets(text: str) -> list[str]:
                    targets = []
                    for m in re.finditer(r"^diff --git a/([^ ]+) b/([^\n]+)", text, flags=re.M):
                        a = m.group(1).strip()
                        b = m.group(2).strip()
                        targets.append(b or a)
                    for m in re.finditer(r"^(?:\+\+\+|---)\s+(?:b/)?(.+)$", text, flags=re.M):
                        fp = m.group(1).strip()
                        if fp not in targets:
                            targets.append(fp)
                    # normalize
                    return [os.path.normpath(p) for p in targets if p and not p.startswith('/')]

                # find diff files
                for diff_fp in patches_dir.rglob('*.diff'):
                    try:
                        text = diff_fp.read_text(encoding='utf-8', errors='ignore')
                    except Exception:
                        print('Cannot read patch file', diff_fp)
                        failed.append(str(diff_fp))
                        continue
                    targets = parse_patch_targets(text)
                    # backup targets
                    for t in targets:
                        src = root.joinpath(t)
                        if src.exists():
                            dest = backup_root.joinpath(t + '.bak')
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dest)
                    # attempt to apply via git apply or patch
                    applied_ok = False
                    # try git apply --check -> git apply
                    try:
                        if (root.joinpath('.git')).exists() and shutil.which('git'):
                            chk = subprocess.run(['git', 'apply', '--check', str(diff_fp)], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                            if chk.returncode == 0:
                                ap = subprocess.run(['git', 'apply', str(diff_fp)], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                                applied_ok = ap.returncode == 0
                    except Exception:
                        applied_ok = False
                    if not applied_ok and shutil.which('patch'):
                        # try -p1 then -p0
                        for pflag in ('-p1', '-p0'):
                            try:
                                # dry run
                                dry = subprocess.run(['patch', pflag, '--dry-run', '-i', str(diff_fp)], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                                if dry.returncode == 0:
                                    ap = subprocess.run(['patch', pflag, '-i', str(diff_fp)], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                                    applied_ok = ap.returncode == 0
                                    break
                            except Exception:
                                continue
                    if applied_ok:
                        print('Applied patch:', diff_fp)
                        applied.append(str(diff_fp))
                    else:
                        print('Failed to apply patch:', diff_fp)
                        failed.append(str(diff_fp))

                # Run validation (syntax + pytest if present)
                print('Running validation: py_compile sweep...')
                py_errs = []
                for p in root.rglob('*.py'):
                    if any(x in str(p) for x in ('.venv', 'venv', '__pycache__', '.git')):
                        continue
                    try:
                        import py_compile as _pc
                        _pc.compile(str(p), doraise=True)
                    except Exception as e:
                        py_errs.append(f'{p}: {e}')

                test_failures = []
                pytest_exe = shutil.which('pytest')
                if pytest_exe and (root.joinpath('tests').exists()):
                    try:
                        print('Running pytest...')
                        res = subprocess.run([pytest_exe, '-q'], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        if res.returncode != 0:
                            test_failures.append(res.stdout + '\n' + res.stderr)
                    except Exception as e:
                        test_failures.append(str(e))

                # Optional LLM inference latency check (configurable)
                try:
                    ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:1776/api/chat')
                    require_llm = os.environ.get('HAL_AUTO_IMPROVE_REQUIRE_LLM', '').lower() in ('1', 'true', 'yes')
                    max_latency_ms = int(os.environ.get('HAL_AUTO_IMPROVE_MAX_LATENCY_MS', '1000'))
                    # Only attempt a lightweight latency check; don't fail the whole run if bridge unreachable
                    try:
                        import urllib.request
                        start = time.time()
                        payload = json.dumps({'model': 'qwen2.5-coder:7b', 'messages': [{'role': 'user', 'content': 'ping'}], 'stream': False}).encode()
                        req = urllib.request.Request(ollama_url, data=payload, headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(req, timeout=5) as r:
                            _ = r.read(1024)
                        elapsed_ms = int((time.time() - start) * 1000)
                        if elapsed_ms > max_latency_ms:
                            test_failures.append(f'LLM latency {elapsed_ms}ms exceeded threshold {max_latency_ms}ms')
                        else:
                            print(f'LLM latency: {elapsed_ms}ms (ok <= {max_latency_ms}ms)')
                    except Exception as e:
                        if require_llm:
                            test_failures.append(f'LLM check failed: {e}')
                        else:
                            print('LLM bridge not reachable or check failed (skipping):', e)
                except Exception:
                    pass

                if py_errs or test_failures or failed:
                    print('Validation failed after applying patches. Reverting backups...')
                    # revert
                    for b in backup_root.rglob('*.bak'):
                        try:
                            orig_rel = b.relative_to(backup_root)
                            target_rel = str(orig_rel)[:-4] if str(orig_rel).endswith('.bak') else str(orig_rel)
                            tgt = root.joinpath(target_rel)
                            shutil.copy2(str(b), str(tgt))
                            print('Restored', tgt)
                        except Exception as e:
                            print('Failed to restore', b, e)
                    print('Revert complete. See', backup_root)
                else:
                    print('Patches applied and validated successfully. Backups stored under', backup_root)

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
