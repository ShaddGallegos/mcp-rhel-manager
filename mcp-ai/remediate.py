#!/usr/bin/env python3
"""MCP AI: remediation helper that consults a local LLM endpoint and writes suggested fixes.

This script is intentionally conservative: it will NOT execute suggested commands
unless the environment variable `ALLOW_AUTO_FIX` is set to '1'. By default it
only writes suggestions to the fixes directory for operator review.

Usage:
  remediate.py --input <jsonl_entry>    # analyze a specific training entry
  remediate.py --latest                 # analyze the most recent entry
  remediate.py --latest --exec         # analyze and execute suggestions (requires ALLOW_AUTO_FIX=1)
"""
import os
import sys
import json
import argparse
import urllib.request
import urllib.error
import subprocess
import shlex
import re
from pathlib import Path
import getpass
import time

HOME = os.path.expanduser('~')
AI_HOME = os.path.join(HOME, '.mcp-ai')
TRAIN_DIR = os.path.join(AI_HOME, 'training')
FIXES_DIR = os.path.join(AI_HOME, 'fixes')
REPORTS_DIR = os.path.join(AI_HOME, 'reports')
OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:1776/api/chat')
ATTEMPTS_FILE = os.path.join(AI_HOME, 'cache', 'attempts.json')

def ensure_cache_dir():
    os.makedirs(os.path.dirname(ATTEMPTS_FILE), exist_ok=True)

def load_attempts():
    ensure_cache_dir()
    if os.path.exists(ATTEMPTS_FILE):
        try:
            with open(ATTEMPTS_FILE, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}

def save_attempts(data):
    ensure_cache_dir()
    with open(ATTEMPTS_FILE, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2)

def find_latest_entry():
    p = Path(TRAIN_DIR)
    files = sorted(p.glob('entry-*.jsonl'))
    return str(files[-1]) if files else None

def load_entry(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)

def build_prompt(entry):
    raw_path = entry.get('raw_log')
    raw_text = ''
    try:
        with open(raw_path, 'r', encoding='utf-8') as fh:
            raw_text = fh.read()
    except Exception:
        raw_text = f'<unable to read {raw_path}>'

    system = (
        'You are a system remediation assistant. Analyze the provided system logs and ' 
        'return a JSON object only, with keys: commands (array of shell commands), ' 
        'explanation (string), confidence (0.0-1.0). Do not return additional prose.'
    )

    user = f"METADATA: {json.dumps({'host': entry.get('host'), 'timestamp': entry.get('timestamp'), 'problem_count': entry.get('problem_count')})}\nLOGS:\n{raw_text[:20000]}"
    return system, user

def call_llm(system, user, timeout=60, model=None, max_tokens=None):
    model = model or os.environ.get('MCP_AI_MODEL', 'qwen2.5-coder:7b')
    payload_obj = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user}
        ]
    }
    if max_tokens:
        try:
            payload_obj['max_tokens'] = int(max_tokens)
        except Exception:
            pass
    payload = json.dumps(payload_obj).encode('utf-8')
    req = urllib.request.Request(OLLAMA_URL, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode('utf-8')
            return text
    except urllib.error.URLError as e:
        print('LLM call failed:', e, file=sys.stderr)
        return None

def find_suggestion_for_entry(entry_path):
    # Look for existing suggestion files for this entry in FIXES_DIR
    base = Path(entry_path).stem
    Path(FIXES_DIR).mkdir(parents=True, exist_ok=True)
    matches = sorted(Path(FIXES_DIR).glob(f'suggestion-{base}*.json'))
    if not matches:
        return None
    try:
        with open(matches[-1], 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
            return payload.get('suggestion') or payload
    except Exception:
        return None

def audit_event(ev):
    Path(AI_HOME).mkdir(parents=True, exist_ok=True)
    alog = os.path.join(AI_HOME, 'audit.log')
    entry = {'ts': datetime_now_iso(), 'user': getpass.getuser(), 'event': ev}
    try:
        with open(alog, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry) + '\n')
    except Exception:
        pass

def ensure_backup_dir():
    bd = '/var/lib/mcp/backups'
    try:
        os.makedirs(bd, exist_ok=True)
    except Exception:
        pass
    return bd

def create_etc_backup():
    bd = ensure_backup_dir()
    ts = datetime_now_iso().replace(':','').replace('-','')
    fn = os.path.join(bd, f'etc-backup-{ts}.tar.gz')
    try:
        # Use sudo to read /etc when needed
        proc = subprocess.run(['sudo', 'tar', '-czf', fn, '/etc'], capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and os.path.exists(fn):
            return fn
        else:
            return None
    except Exception:
        return None

def metrics_inc(key, delta=1):
    Path(AI_HOME).mkdir(parents=True, exist_ok=True)
    mfn = os.path.join(AI_HOME, 'metrics.json')
    data = {}
    try:
        if os.path.exists(mfn):
            with open(mfn, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
    except Exception:
        data = {}
    data[key] = int(data.get(key, 0)) + int(delta)
    try:
        with open(mfn, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass

def extract_json(text):
    if not text:
        return None
    m = re.search(r'\{\s*"commands".*\}', text, re.S)
    if not m:
        m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def normalize_solutions(json_resp):
    # Return a list of solutions with id and commands
    sols = []
    if not json_resp:
        return sols
    if isinstance(json_resp, dict) and 'solutions' in json_resp and isinstance(json_resp['solutions'], list):
        for i, s in enumerate(json_resp['solutions']):
            sid = s.get('id') or f's{i+1}'
            cmds = s.get('commands') or s.get('commands_list') or []
            sols.append({'id': sid, 'commands': cmds, 'meta': s})
        return sols
    # fallback: single commands array
    if isinstance(json_resp, dict) and 'commands' in json_resp:
        return [{'id': 's1', 'commands': json_resp.get('commands', []), 'meta': json_resp}]
    # Support single-tool responses like {"name": "architect.foo", ...}
    if isinstance(json_resp, dict) and 'name' in json_resp:
        name = json_resp.get('name')
        cmds = json_resp.get('commands') if isinstance(json_resp.get('commands'), list) else [name]
        return [{'id': 's1', 'commands': cmds, 'meta': json_resp}]
    return sols

def write_suggestion(entry_path, suggestion):
    Path(FIXES_DIR).mkdir(parents=True, exist_ok=True)
    base = Path(entry_path).stem
    ts = entry_path.split('-')[-1]
    out_path = os.path.join(FIXES_DIR, f'suggestion-{base}-{ts}.json')
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump({'entry': entry_path, 'suggestion': suggestion}, fh, indent=2)
    # also attempt to annotate the original training entry with a pointer to the suggestion
    try:
        e = load_entry(entry_path)
        e.setdefault('suggestions', []).append({'file': out_path, 'ts': datetime_now_iso()})
        with open(entry_path, 'w', encoding='utf-8') as fh:
            json.dump(e, fh, indent=2)
    except Exception:
        pass
    return out_path

def attempt_execute(commands):
    # Execute commands via the validated runner wrapper (/usr/local/bin/mcp-ai-runner).
    results = []
    cache_dir = os.path.join(AI_HOME, 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    tmp_fn = os.path.join(cache_dir, f'cmds-{datetime_now_iso().replace(":", "").replace("-","")}.json')
    try:
        with open(tmp_fn, 'w', encoding='utf-8') as fh:
            json.dump({'commands': commands}, fh)
    except Exception as e:
        return [{'cmds': commands, 'rc': -1, 'out': '', 'err': f'write-failed: {e}'}]

    try:
        # Call the runner via sudo (requires sudoers to allow this binary)
        proc = subprocess.run(['sudo', '-n', '/usr/local/bin/mcp-ai-runner', tmp_fn], capture_output=True, text=True)
        if proc.returncode != 0:
            return [{'cmds': commands, 'rc': proc.returncode, 'out': proc.stdout, 'err': proc.stderr}]

        out = proc.stdout.strip()
        # Runner prints the results file path as its stdout
        res_path = out.splitlines()[-1].strip() if out else ''
        if res_path and os.path.exists(res_path):
            try:
                with open(res_path, 'r', encoding='utf-8') as fh:
                    reslist = json.load(fh)
                return reslist
            except Exception as e:
                return [{'cmds': commands, 'rc': -1, 'out': proc.stdout, 'err': f'failed-to-read-results: {e}'}]

        # Fallback: return runner stdout/stderr as a single result
        return [{'cmds': commands, 'rc': proc.returncode, 'out': proc.stdout, 'err': proc.stderr}]
    except Exception as e:
        return [{'cmds': commands, 'rc': -1, 'out': '', 'err': str(e)}]


def mark_attempt(entry_path, solution_id, status, details=None):
    attempts = load_attempts()
    e = attempts.setdefault(entry_path, {})
    sols = e.setdefault('solutions', {})
    s = sols.setdefault(solution_id, {})
    s['status'] = status
    s.setdefault('history', []).append({'ts': datetime_now_iso(), 'status': status, 'details': details})
    save_attempts(attempts)


def datetime_now_iso():
    from datetime import datetime
    return datetime.utcnow().isoformat() + 'Z'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', help='Path to JSONL training entry')
    ap.add_argument('--latest', action='store_true')
    ap.add_argument('--plan', action='store_true', help='Validate and write plan (dry-run) without executing')
    ap.add_argument('--exec', action='store_true', dest='do_exec')
    ap.add_argument('--no-exec', action='store_true', dest='no_exec')
    args = ap.parse_args()

    entry_path = args.input
    if args.latest:
        entry_path = find_latest_entry()

    if not entry_path:
        print('No training entry found', file=sys.stderr)
        return 2

    entry = load_entry(entry_path)
    system, user = build_prompt(entry)

    # load config early so model selection and cost controls are respected
    cfg_path = os.path.join(AI_HOME, 'config.json')
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {}

    selected_model = cfg.get('model')
    max_tokens = cfg.get('max_tokens')
    resp_text = call_llm(system, user, model=selected_model, max_tokens=max_tokens)
    json_resp = extract_json(resp_text)

    # Some LLM bridges wrap the assistant output inside a JSON envelope
    # (e.g. {"message": {"role":"assistant","content":"```json {...}```"}}).
    # If the top-level JSON is present but doesn't contain actionable fields,
    # attempt to extract JSON from nested `message.content`.
    if isinstance(json_resp, dict):
        # Try nested assistant content
        nested = None
        msg = json_resp.get('message') if isinstance(json_resp.get('message'), dict) else None
        if msg:
            content = msg.get('content')
            if isinstance(content, str):
                nested = extract_json(content)
        if nested:
            json_resp = nested

    if not json_resp:
        # Try to fall back to an existing suggestion file for this entry
        suggestion = find_suggestion_for_entry(entry_path)
        if suggestion:
            print('Using existing suggestion file as fallback for entry', entry_path)
            # The stored suggestion may be an LLM envelope with message.content containing JSON.
            # Attempt to extract and normalize it into the expected shape.
            if isinstance(suggestion, dict) and isinstance(suggestion.get('message'), dict):
                content = suggestion['message'].get('content')
                if isinstance(content, str):
                    # try to parse as JSON array/object
                    try:
                        parsed = json.loads(content)
                        # If parsed is list of tool entries, convert to solutions
                        if isinstance(parsed, list):
                            # convert list of {'name':..} -> dict with solutions
                            sols = []
                            for i, it in enumerate(parsed):
                                nid = it.get('id') or f's{i+1}'
                                cmds = []
                                if isinstance(it.get('commands'), list) and it.get('commands'):
                                    cmds = it.get('commands')
                                elif it.get('name'):
                                    cmds = [it.get('name')]
                                sols.append({'id': nid, 'commands': cmds, 'meta': it})
                            json_resp = {'solutions': sols}
                        elif isinstance(parsed, dict):
                            json_resp = parsed
                        else:
                            json_resp = suggestion
                    except Exception:
                        json_resp = suggestion
                else:
                    json_resp = suggestion
            else:
                json_resp = suggestion
        else:
            print('LLM did not return parseable JSON. Saving raw response to report.', file=sys.stderr)
            rpt = os.path.join(REPORTS_DIR, f'report-{Path(entry_path).stem}.txt')
            Path(REPORTS_DIR).mkdir(parents=True, exist_ok=True)
            with open(rpt, 'w', encoding='utf-8') as fh:
                fh.write(resp_text or '')
            print('Raw LLM response written to', rpt)
            return 3
    out_path = write_suggestion(entry_path, json_resp)
    print('Suggestion saved to', out_path)

    # load config (auto_remediate default behavior) early so plan/dry-run behavior can check it
    cfg_path = os.path.join(AI_HOME, 'config.json')
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {}

    allow_env = os.environ.get('ALLOW_AUTO_FIX', None)
    if allow_env is None:
        allow = cfg.get('allow_auto_fix', False)
    else:
        allow = (allow_env == '1')

    # Lightweight schema validation (ensure structure is actionable)
    def validate_remediation_schema(resp):
        if not isinstance(resp, dict):
            return False, 'top-level response must be a JSON object'
        if 'solutions' in resp and isinstance(resp['solutions'], list):
            for i, s in enumerate(resp['solutions']):
                if not isinstance(s, dict):
                    return False, f'solutions[{i}] must be an object'
                cmds = s.get('commands')
                if not isinstance(cmds, list) or not cmds:
                    return False, f'solutions[{i}].commands must be a non-empty array'
                for j, c in enumerate(cmds):
                    if not (isinstance(c, str) or isinstance(c, dict)):
                        return False, f'solutions[{i}].commands[{j}] must be string or object'
            return True, None
        if 'commands' in resp and isinstance(resp['commands'], list):
            if not resp['commands']:
                return False, 'commands must be a non-empty array'
            for j, c in enumerate(resp['commands']):
                if not (isinstance(c, str) or isinstance(c, dict)):
                    return False, f'commands[{j}] must be string or object'
            return True, None
        return False, 'no solutions or commands found in response'

    def write_plan_file(entry_path, resp, validated, reason=None):
        Path(FIXES_DIR).mkdir(parents=True, exist_ok=True)
        base = Path(entry_path).stem
        out = os.path.join(FIXES_DIR, f'plan-{base}.json')
        payload = {
            'entry': entry_path,
            'validated': bool(validated),
            'validation_error': reason,
            'suggestion': resp,
            'ts': datetime_now_iso(),
            'dry_run': True
        }
        with open(out, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=2)
        return out

    # validate and optionally write a plan (dry-run) for operator review
    is_valid, reason = validate_remediation_schema(json_resp)
    if args.plan or not allow:
        plan_path = write_plan_file(entry_path, json_resp, is_valid, reason)
        audit_event({'action': 'plan_written', 'entry': entry_path, 'plan': plan_path, 'valid': bool(is_valid)})
        metrics_inc('plans_written', 1)
        approvals_dir = os.path.join(AI_HOME, 'approvals')
        Path(approvals_dir).mkdir(parents=True, exist_ok=True)
        approved_file = os.path.join(approvals_dir, f'plan-{Path(entry_path).stem}.approved.json')
        if is_valid:
            print('Plan written to', plan_path)
            if cfg.get('require_approval', True):
                print('Plan requires operator approval. Approve via the approve CLI or create', approved_file)
            else:
                print('Plan is valid according to local schema; no execution performed (dry-run).')
            return 0
        else:
            print('Plan written to', plan_path)
            print('Plan FAILED schema validation:', reason, file=sys.stderr)
            return 3

    # CLI flag overrides config: ensure execution only allowed when configured
    if args.do_exec:
        if not allow:
            print('Execution requested but ALLOW_AUTO_FIX not enabled (env or config); aborting execution.')
            return 4
        # If approval is required, ensure the plan has been approved
        if cfg.get('require_approval', True):
            approvals_dir = os.path.join(AI_HOME, 'approvals')
            approved_file = os.path.join(approvals_dir, f'plan-{Path(entry_path).stem}.approved.json')
            if not os.path.exists(approved_file):
                print('Execution requires operator approval. Approve the plan before running. Expected approval file:', approved_file, file=sys.stderr)
                audit_event({'action': 'exec_blocked_no_approval', 'entry': entry_path})
                return 8

    # Normalize suggestion into solutions list
    solutions = normalize_solutions(json_resp)

    # Normalize tool-style commands (e.g., dicts or 'architect.foo') into
    # shell-invokable commands that the runner accepts. We translate
    # architect.<func> -> venv python call that imports server.<func>()
    # Map tool-like names (architect.foo or server.foo) to an RPC URI that the runner understands.
    for sol in solutions:
        cmds = sol.get('commands', []) or []
        out_cmds = []
        for c in cmds:
            if isinstance(c, dict):
                name = c.get('name') or c.get('command') or ''
            else:
                name = str(c)

            # Recognize dotted tool names and convert to an mcp-call URI
            if '.' in name and not name.startswith('/') and not name.startswith('http'):
                # Accept formats like 'architect.optimize_ai_performance' or 'server.optimize_ai_performance'
                parts = name.split('.')
                func = parts[-1]
                # Use server module as target for RPC
                out_cmds.append(f'mcp-call://server.{func}')
            else:
                out_cmds.append(name)

        sol['commands'] = out_cmds

    if not solutions:
        print('No solutions parsed from LLM output; aborting.', file=sys.stderr)
        return 5

    attempts_cache = load_attempts()
    entry_attempts = attempts_cache.get(entry_path, {}).get('solutions', {})

    max_retries = int(cfg.get('max_retries', 2))
    retries = 0
    final_results = {'attempts': []}

    while True:
        for sol in solutions:
            sid = sol.get('id')
            if entry_attempts.get(sid, {}).get('status') == 'failed':
                print(f"Skipping previously-failed solution {sid}")
                final_results['attempts'].append({'solution': sid, 'status': 'skipped'})
                continue

            if not allow:
                print(f"Autoremediate disabled; recorded suggestion {sid} for operator review.")
                final_results['attempts'].append({'solution': sid, 'status': 'recorded'})
                continue

            # Execute solution commands
            cmds = sol.get('commands', [])
            if not cmds:
                print(f"Solution {sid} has no commands; skipping.")
                mark_attempt(entry_path, sid, 'skipped', {'reason': 'no-commands'})
                final_results['attempts'].append({'solution': sid, 'status': 'skipped'})
                continue

            print(f"Executing solution {sid} (commands: {len(cmds)})")
            # create an /etc backup before attempting changes (best-effort)
            bpath = create_etc_backup()
            if bpath:
                audit_event({'action': 'backup_created', 'path': bpath, 'entry': entry_path, 'solution': sid})
                metrics_inc('backups_created', 1)
            else:
                audit_event({'action': 'backup_failed', 'entry': entry_path, 'solution': sid})

            audit_event({'action': 'execution_started', 'entry': entry_path, 'solution': sid, 'commands': cmds})
            metrics_inc('executions_started', 1)
            results = attempt_execute(cmds)
            # determine success if all rc == 0
            success = all(r.get('rc', 1) == 0 for r in results)
            mark_attempt(entry_path, sid, 'success' if success else 'failed', {'results': results})
            # audit and metrics
            audit_event({'action': 'execution_finished', 'entry': entry_path, 'solution': sid, 'success': bool(success)})
            metrics_inc('executions_total', 1)
            if success:
                metrics_inc('executions_success', 1)
            else:
                metrics_inc('executions_failed', 1)
            final_results['attempts'].append({'solution': sid, 'status': 'success' if success else 'failed', 'results': results})
            res_path = os.path.join(FIXES_DIR, f'result-{Path(entry_path).stem}-{sid}.json')
            Path(FIXES_DIR).mkdir(parents=True, exist_ok=True)
            with open(res_path, 'w', encoding='utf-8') as fh:
                json.dump({'results': results}, fh, indent=2)
            print('Execution results saved to', res_path)
            # annotate the original training entry with the remediation result and (optional) HAL interaction
            try:
                entry_obj = load_entry(entry_path)
                rem_record = {
                    'ts': datetime_now_iso(),
                    'solution_id': sid,
                    'status': 'success' if success else 'failed',
                    'results_file': res_path,
                    'results': results
                }
                # If this remediation was initiated from a HAL interaction, include it
                hal_id = os.environ.get('HAL_INTERACTION_ID')
                if hal_id and os.path.exists(hal_id):
                    try:
                        with open(hal_id, 'r', encoding='utf-8') as hfh:
                            hal_entry = json.load(hfh)
                        rem_record['hal_interaction'] = hal_entry
                    except Exception:
                        pass

                entry_obj.setdefault('remediations', []).append(rem_record)
                with open(entry_path, 'w', encoding='utf-8') as fh:
                    json.dump(entry_obj, fh, indent=2)
            except Exception:
                pass

            if success:
                print(f"Solution {sid} succeeded; stopping.")
                # record final summary and exit
                summary_path = os.path.join(FIXES_DIR, f'summary-{Path(entry_path).stem}.json')
                with open(summary_path, 'w', encoding='utf-8') as fh:
                    json.dump(final_results, fh, indent=2)
                return 0
            # else continue to next solution

        # if we reach here, all current solutions failed or were skipped
        if not allow:
            print('Autoremediate disabled; no execution performed.')
            return 0

        if retries >= max_retries:
            print('Max retries reached; aborting further alternative attempts.')
            summary_path = os.path.join(FIXES_DIR, f'summary-{Path(entry_path).stem}.json')
            with open(summary_path, 'w', encoding='utf-8') as fh:
                json.dump(final_results, fh, indent=2)
            return 6

        # Ask LLM for alternatives, include failed attempt details
        retries += 1
        prev_failures = [a for a in final_results['attempts'] if a.get('status') == 'failed']
        alt_user = user + '\n\nPREVIOUS_FAILURES:\n' + json.dumps(prev_failures)
        alt_text = call_llm(system, alt_user)
        alt_json = extract_json(alt_text)
        if not alt_json:
            print('LLM did not provide alternate solutions; aborting.')
            summary_path = os.path.join(FIXES_DIR, f'summary-{Path(entry_path).stem}.json')
            with open(summary_path, 'w', encoding='utf-8') as fh:
                json.dump(final_results, fh, indent=2)
            return 7
        solutions = normalize_solutions(alt_json)
        print(f"LLM returned {len(solutions)} alternative solutions (retry {retries}/{max_retries})")
        # loop and attempt these

if __name__ == '__main__':
    sys.exit(main())
