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
import hashlib
from pathlib import Path
import getpass
import time
import mcp_ai_config as config

HOME = os.path.expanduser('~')
AI_HOME = config.get_config('ai_home', os.path.join(HOME, '.mcp-ai'))
TRAIN_DIR = os.path.join(AI_HOME, 'training')
FIXES_DIR = os.path.join(AI_HOME, 'fixes')
REPORTS_DIR = os.path.join(AI_HOME, 'reports')
FIX_LIBRARY_DIR = os.path.join(AI_HOME, 'fix-library')
FIX_LIBRARY_FILE = os.path.join(FIX_LIBRARY_DIR, 'library.json')
# Default to configured ollama/bridge URL (normalized by config)
OLLAMA_URL = config.get_config('ollama_url', config.get_ollama_url())
if not OLLAMA_URL.endswith('/api/chat'):
    OLLAMA_URL = OLLAMA_URL.rstrip('/') + '/api/chat'
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
    # Support both legacy collector entries and HAL interaction entries.
    files = sorted(list(p.glob('entry-*.jsonl')) + list(p.glob('hal-*.jsonl')))
    return str(files[-1]) if files else None

def load_entry(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def load_runtime_config():
    """Load optional runtime config from ~/.mcp-ai/config.json."""
    cfg_path = os.path.join(AI_HOME, 'config.json')
    if not os.path.exists(cfg_path):
        return {}
    try:
        with open(cfg_path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def build_prompt(entry):
    raw_path = entry.get('raw_log')
    raw_text = ''
    if raw_path:
        try:
            with open(raw_path, 'r', encoding='utf-8') as fh:
                raw_text = fh.read()
        except Exception:
            raw_text = f'<unable to read {raw_path}>'
    else:
        # HAL writes inline payloads under ai_response_raw instead of raw_log files.
        inline = entry.get('ai_response_raw') or entry.get('response') or entry.get('content')
        if isinstance(inline, str) and inline.strip():
            raw_text = inline
        else:
            raw_text = json.dumps(entry, ensure_ascii=False)

    # If HAL supplied a full diagnostics JSON blob, compact it to key findings.
    compact_text = None
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict) and ('hardware' in parsed or 'security' in parsed):
            lines = []
            lines.append('DIAGNOSTIC_FINDINGS')
            for section in ('hardware', 'security'):
                issues = parsed.get(section) or []
                if not isinstance(issues, list):
                    continue
                lines.append(f'[{section.upper()}] count={len(issues)}')
                for idx, item in enumerate(issues[:20], start=1):
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get('title', '')).strip()
                    severity = str(item.get('severity', '')).strip()
                    error_text = str(item.get('error_text', '')).replace('\n', ' ').strip()
                    rem = item.get('remediations') or []
                    rem_text = '; '.join(str(r).strip() for r in rem[:3]) if isinstance(rem, list) else str(rem)
                    if len(error_text) > 260:
                        error_text = error_text[:260] + '...'
                    lines.append(f'{idx}. [{severity}] {title}')
                    if error_text:
                        lines.append(f'   evidence: {error_text}')
                    if rem_text:
                        lines.append(f'   suggested: {rem_text}')
            compact_text = '\n'.join(lines)
    except Exception:
        compact_text = None

    system = (
        'You are a system remediation assistant. Analyze the provided system logs and '
        'return JSON only. Preferred schema: '
        '{"solutions":[{"id":"s1","commands":[...],"verify_commands":[...],"confidence":0.0,"explanation":"..."}],'
        '"notes":"..."}. '
        'Each command should be directly executable, low-risk, and idempotent when possible. '
        'You may also provide structured commands as objects: '
        '{"type":"bash|python|ansible","name":"short-name","content":"script/playbook text"}. '
        'Include verify_commands that prove the issue is resolved. Do not return markdown.'
    )

    max_chars = 8000
    try:
        max_chars = int(os.environ.get('MCP_AI_LOG_CHARS', '8000'))
    except Exception:
        pass
    prompt_logs = compact_text if compact_text else raw_text
    user = f"METADATA: {json.dumps({'host': entry.get('host'), 'timestamp': entry.get('timestamp'), 'problem_count': entry.get('problem_count'), 'request': entry.get('request')})}\nLOGS:\n{prompt_logs[:max_chars]}"
    return system, user

def call_llm(system, user, timeout=60, model=None, max_tokens=None):
    model = model or os.environ.get('MCP_AI_MODEL', 'qwen2.5-coder:7b')
    try:
        timeout = int(os.environ.get('MCP_AI_TIMEOUT_SEC', str(timeout)))
    except Exception:
        timeout = 60

    payload_obj = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user}
        ],
        'stream': False
    }
    if max_tokens:
        try:
            payload_obj['max_tokens'] = int(max_tokens)
        except Exception:
            pass

    # Prefer centralized client if available (pooling + failover)
    try:
        import llm_client
        client = llm_client.get_client()
        resp = client.call_with_failover(payload_obj, stream=False, timeout=timeout)
        return resp
    except Exception:
        # Fall back to the previous candidate scanning logic using urllib
        payload = json.dumps(payload_obj).encode('utf-8')
    # Build a prioritized list of endpoints to try. Use config and env overrides.
    candidates = []
    env_list = os.environ.get('OLLAMA_URLS') or config.get_config('OLLAMA_URLS')
    if env_list:
        candidates = [u.strip() for u in env_list.split(',') if u.strip()]
    else:
        primary = config.get_config('ollama_url') or OLLAMA_URL
        if primary:
            if primary.endswith('/api/chat'):
                candidates.append(primary)
            else:
                candidates.append(primary.rstrip('/') + '/api/chat')
        # add common bridge/ollama fallbacks based on host
        try:
            from urllib.parse import urlparse
            p = urlparse(primary)
            host = p.hostname or 'localhost'
        except Exception:
            host = 'localhost'
        alt_candidates = [f'http://{host}:1776/api/chat', f'http://{host}:1776/chat', f'http://{host}:11434/api/chat', f'http://127.0.0.1:11434/api/chat']
        for c in alt_candidates:
            if c not in candidates:
                candidates.append(c)

    last_err = None
    for endpoint in candidates:
        if not endpoint:
            continue
        try:
            print(f'Trying LLM endpoint: {endpoint}', file=sys.stderr)
            req = urllib.request.Request(endpoint, data=payload, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode('utf-8')
                return text
        except Exception as e:
            last_err = e
            print(f'LLM call failed for {endpoint}: {e}', file=sys.stderr)
            try:
                time.sleep(0.5)
            except Exception:
                pass

    if last_err:
        print('All LLM endpoints failed, returning None', file=sys.stderr)
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
        try:
            import ingest_common
            try:
                ingest_common.append_jsonl(alog, entry)
            except Exception:
                with open(alog, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(entry) + '\n')
        except Exception:
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
            verify_cmds = s.get('verify_commands') or []
            sols.append({'id': sid, 'commands': cmds, 'verify_commands': verify_cmds, 'meta': s})
        return sols
    # fallback: single commands array
    if isinstance(json_resp, dict) and 'commands' in json_resp:
        return [{'id': 's1', 'commands': json_resp.get('commands', []), 'verify_commands': json_resp.get('verify_commands', []), 'meta': json_resp}]
    # Support single-tool responses like {"name": "architect.foo", ...}
    if isinstance(json_resp, dict) and 'name' in json_resp:
        name = json_resp.get('name')
        cmds = json_resp.get('commands') if isinstance(json_resp.get('commands'), list) else [name]
        return [{'id': 's1', 'commands': cmds, 'verify_commands': json_resp.get('verify_commands', []), 'meta': json_resp}]
    return sols


def ensure_fix_library_dirs():
    Path(FIX_LIBRARY_DIR).mkdir(parents=True, exist_ok=True)
    Path(os.path.join(FIX_LIBRARY_DIR, 'artifacts')).mkdir(parents=True, exist_ok=True)


def load_fix_library():
    ensure_fix_library_dirs()
    if not os.path.exists(FIX_LIBRARY_FILE):
        return {'items': []}
    try:
        with open(FIX_LIBRARY_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get('items'), list):
                return data
    except Exception:
        pass
    return {'items': []}


def save_fix_library(data):
    ensure_fix_library_dirs()
    tmp = FIX_LIBRARY_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, FIX_LIBRARY_FILE)


def compute_issue_signature(entry):
    """Create a stable issue signature from entry metadata and symptom text."""
    host = str(entry.get('host', ''))
    summary = str(entry.get('summary', ''))
    problem_count = str(entry.get('problem_count', ''))
    request = str(entry.get('request', ''))
    payload = {
        'host': host,
        'summary': summary[:1000],
        'problem_count': problem_count,
        'request': request[:400],
    }
    raw = json.dumps(payload, sort_keys=True).encode('utf-8', errors='ignore')
    return hashlib.sha256(raw).hexdigest()[:24]


def library_solutions_for_signature(issue_sig, max_items=5):
    data = load_fix_library()
    out = []
    for item in data.get('items', []):
        if item.get('issue_signature') != issue_sig:
            continue
        if not item.get('successful', False):
            continue
        out.append({
            'id': item.get('id', f'lib-{len(out)+1}'),
            'commands': item.get('commands', []),
            'verify_commands': item.get('verify_commands', []),
            'meta': {'source': 'library', 'library_item': item},
        })
        if len(out) >= max_items:
            break
    return out


def record_library_success(issue_sig, solution, results, entry_path):
    data = load_fix_library()
    items = data.setdefault('items', [])
    sol_id = str(solution.get('id', 's1'))
    commands = solution.get('commands', [])
    verify_commands = solution.get('verify_commands', [])
    cmd_fingerprint = hashlib.sha256(json.dumps(commands, sort_keys=True).encode('utf-8', errors='ignore')).hexdigest()[:24]

    existing = None
    for it in items:
        if it.get('issue_signature') == issue_sig and it.get('command_fingerprint') == cmd_fingerprint:
            existing = it
            break

    payload = {
        'id': f'{issue_sig}-{sol_id}',
        'issue_signature': issue_sig,
        'command_fingerprint': cmd_fingerprint,
        'commands': commands,
        'verify_commands': verify_commands,
        'successful': True,
        'last_success_ts': datetime_now_iso(),
        'entry': entry_path,
        'result_sample': results[:3] if isinstance(results, list) else results,
    }

    if existing is None:
        items.append(payload)
    else:
        existing.update(payload)

    save_fix_library(data)


def _command_fingerprint(commands):
    try:
        raw = json.dumps(commands or [], sort_keys=True, ensure_ascii=False)
    except Exception:
        raw = str(commands)
    return hashlib.sha256(raw.encode('utf-8', errors='ignore')).hexdigest()[:24]


def record_library_failure(issue_sig, solution, results, entry_path):
    """Persist failed fixes so future runs can skip known-bad attempts."""
    data = load_fix_library()
    items = data.setdefault('items', [])
    commands = solution.get('commands', [])
    verify_commands = solution.get('verify_commands', [])
    fp = _command_fingerprint(commands)
    existing = None
    for it in items:
        if it.get('issue_signature') == issue_sig and it.get('command_fingerprint') == fp:
            existing = it
            break

    payload = {
        'id': f"{issue_sig}-{solution.get('id', 's1')}",
        'issue_signature': issue_sig,
        'command_fingerprint': fp,
        'commands': commands,
        'verify_commands': verify_commands,
        'successful': False,
        'last_failure_ts': datetime_now_iso(),
        'failed_count': 1,
        'entry': entry_path,
        'last_error_sample': results[:3] if isinstance(results, list) else results,
    }
    if existing is None:
        items.append(payload)
    else:
        existing['successful'] = False
        existing['last_failure_ts'] = payload['last_failure_ts']
        existing['entry'] = entry_path
        existing['last_error_sample'] = payload['last_error_sample']
        existing['failed_count'] = int(existing.get('failed_count', 0) or 0) + 1
    save_fix_library(data)


def is_known_bad_fix(issue_sig, commands):
    """Return True when this command fingerprint has previously failed for the same issue signature."""
    data = load_fix_library()
    fp = _command_fingerprint(commands)
    for it in data.get('items', []):
        if it.get('issue_signature') != issue_sig:
            continue
        if it.get('command_fingerprint') != fp:
            continue
        if bool(it.get('successful', False)):
            continue
        return True
    return False


def normalize_solution_commands(solution):
    """Normalize commands in a solution to executable strings.

    Supports:
    - tool names (architect.foo/server.foo) -> mcp-call URI
    - structured command objects with type/content -> generated artifact command
    """
    cmds = solution.get('commands', []) or []
    out_cmds = []
    for c in cmds:
        if isinstance(c, dict):
            artifact_cmd = materialize_structured_command(c)
            if artifact_cmd:
                out_cmds.append(artifact_cmd)
                continue
            name = c.get('name') or c.get('command') or ''
        else:
            name = str(c)

        if not name:
            continue

        if '.' in name and not name.startswith('/') and not name.startswith('http'):
            func = name.split('.')[-1]
            out_cmds.append(f'mcp-call://server.{func}')
        else:
            out_cmds.append(name)
    solution['commands'] = out_cmds

    # Also normalize verify commands if provided as structured objects
    verify_cmds = solution.get('verify_commands', []) or []
    out_verify = []
    for v in verify_cmds:
        if isinstance(v, dict):
            artifact_cmd = materialize_structured_command(v)
            if artifact_cmd:
                out_verify.append(artifact_cmd)
                continue
            name = v.get('name') or v.get('command') or ''
            if name:
                out_verify.append(str(name))
        elif isinstance(v, str) and v.strip():
            out_verify.append(v)
    solution['verify_commands'] = out_verify


def _safe_artifact_name(raw_name, suffix):
    cleaned = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(raw_name or 'artifact')).strip('-')
    if not cleaned:
        cleaned = 'artifact'
    ts = datetime_now_iso().replace(':', '').replace('-', '')
    return f'{cleaned}-{ts}.{suffix}'


def materialize_structured_command(cmd_obj):
    """Render structured bash/python/ansible command objects into executable artifacts."""
    if not isinstance(cmd_obj, dict):
        return None
    ctype = str(cmd_obj.get('type', '')).strip().lower()
    content = cmd_obj.get('content')
    name = cmd_obj.get('name') or ctype or 'artifact'
    if not ctype or not isinstance(content, str) or not content.strip():
        return None

    ensure_fix_library_dirs()
    artifact_dir = os.path.join(FIX_LIBRARY_DIR, 'artifacts')

    if ctype == 'bash':
        fn = _safe_artifact_name(name, 'sh')
        path = os.path.join(artifact_dir, fn)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('#!/usr/bin/env bash\nset -euo pipefail\n')
            fh.write(content.strip() + '\n')
        os.chmod(path, 0o755)
        return f'/bin/bash {shlex.quote(path)}'

    if ctype == 'python':
        fn = _safe_artifact_name(name, 'py')
        path = os.path.join(artifact_dir, fn)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('#!/usr/bin/env python3\n')
            fh.write(content.strip() + '\n')
        os.chmod(path, 0o755)
        return f'/usr/bin/python3 {shlex.quote(path)}'

    if ctype == 'ansible':
        fn = _safe_artifact_name(name, 'yml')
        path = os.path.join(artifact_dir, fn)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(content.strip() + '\n')
        return f'/usr/bin/ansible-playbook -i localhost, -c local {shlex.quote(path)}'

    return None


def default_verify_commands(entry, solution):
    """Best-effort verification commands when model did not provide explicit checks."""
    verify = solution.get('verify_commands')
    if isinstance(verify, list) and verify:
        return [v for v in verify if isinstance(v, str) and v.strip()]

    checks = []
    summary = str(entry.get('summary', '')).lower()
    if 'failed to start' in summary:
        m = re.search(r'failed to start\s+([\w@.-]+)', summary)
        if m:
            unit = m.group(1)
            if not unit.endswith('.service'):
                unit = f'{unit}.service'
            checks.append(f'/usr/bin/systemctl is-active {unit}')
    if 'oom' in summary or 'out of memory' in summary:
        checks.append('/usr/bin/systemctl is-active systemd-oomd.service')
    return checks


def verify_solution(entry, solution, allow):
    """Run verify commands and return True when all checks pass.

    If no checks are available, returns None to indicate "not evaluated".
    """
    verify_cmds = default_verify_commands(entry, solution)
    if not verify_cmds:
        return None, []
    if not allow:
        return False, [{'cmd': c, 'rc': -1, 'out': '', 'err': 'verification requires ALLOW_AUTO_FIX'} for c in verify_cmds]

    verify_results = attempt_execute(verify_cmds, entry=None, solution=f"{solution.get('id', 's1')}-verify")
    ok = all(r.get('rc', 1) == 0 for r in verify_results)
    return ok, verify_results

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

def attempt_execute(commands, entry=None, solution=None):
    """Execute commands via the validated runner wrapper (/usr/local/bin/mcp-ai-runner).

    Writes a temporary JSON containing `commands` and optional `entry` and
    `solution` metadata so the runner can consult per-plan approvals.
    """
    results = []
    cache_dir = os.path.join(AI_HOME, 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    tmp_fn = os.path.join(cache_dir, f'cmds-{datetime_now_iso().replace(":", "").replace("-","")}.json')
    payload = {'commands': commands}
    if entry:
        payload['entry'] = entry
    if solution:
        payload['solution'] = solution
    try:
        with open(tmp_fn, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh)
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
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat() + 'Z'

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
    issue_sig = compute_issue_signature(entry)
    library_first = library_solutions_for_signature(issue_sig)
    system, user = build_prompt(entry)

    # Load config early so model selection and execution policy are respected.
    cfg = load_runtime_config()

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

    allow_env = os.environ.get('ALLOW_AUTO_FIX', None)
    if allow_env is None:
        allow = cfg.get('allow_auto_fix', False)
    else:
        allow = (allow_env == '1')

    if args.no_exec:
        allow = False

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

    # Normalize suggestion into solutions list and prepend reusable library fixes.
    solutions = []
    if library_first:
        print(f'Loaded {len(library_first)} reusable fix(es) from library for issue signature {issue_sig}')
        solutions.extend(library_first)
    solutions.extend(normalize_solutions(json_resp))

    for sol in solutions:
        normalize_solution_commands(sol)

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

            if is_known_bad_fix(issue_sig, sol.get('commands', [])):
                print(f"Skipping known-bad fix for issue signature {issue_sig}: {sid}")
                final_results['attempts'].append({'solution': sid, 'status': 'skipped-known-bad'})
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
            results = attempt_execute(cmds, entry_path, sid)
            # determine success if all rc == 0
            success = all(r.get('rc', 1) == 0 for r in results)
            verify_ok, verify_results = verify_solution(entry, sol, allow)
            if verify_ok is False:
                success = False
            mark_attempt(entry_path, sid, 'success' if success else 'failed', {'results': results})
            # audit and metrics
            audit_event({'action': 'execution_finished', 'entry': entry_path, 'solution': sid, 'success': bool(success)})
            metrics_inc('executions_total', 1)
            if success:
                metrics_inc('executions_success', 1)
                if verify_ok is True:
                    metrics_inc('verifications_success', 1)
            else:
                metrics_inc('executions_failed', 1)
                if verify_ok is False:
                    metrics_inc('verifications_failed', 1)
            final_results['attempts'].append({
                'solution': sid,
                'status': 'success' if success else 'failed',
                'results': results,
                'verification': {
                    'evaluated': verify_ok is not None,
                    'ok': verify_ok,
                    'results': verify_results,
                },
            })
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
                    'results': results,
                    'verification': {
                        'evaluated': verify_ok is not None,
                        'ok': verify_ok,
                        'results': verify_results,
                    },
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
                record_library_success(issue_sig, sol, results, entry_path)
                print(f"Solution {sid} succeeded; stopping.")
                # record final summary and exit
                summary_path = os.path.join(FIXES_DIR, f'summary-{Path(entry_path).stem}.json')
                with open(summary_path, 'w', encoding='utf-8') as fh:
                    json.dump(final_results, fh, indent=2)
                return 0
            else:
                record_library_failure(issue_sig, sol, results, entry_path)
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
            # Heuristic fallback: attempt to extract plausible command lines from raw LLM text
            def _guess_commands_from_text(t):
                if not t:
                    return []
                cmds = []
                in_code = False
                for line in (t or '').splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith('```'):
                        in_code = not in_code
                        continue
                    if in_code or s.startswith('/') or s.startswith('sudo ') or any(s.startswith(k) for k in ('systemctl', 'podman', 'tar', 'ansible-playbook', 'rsync', 'dnf', 'journalctl', 'python3', '/bin/bash')):
                        s = re.sub(r'^[\-\*\>\s]+', '', s)
                        if len(s) > 1 and not re.search(r'\b(solution|explanation|note)\b', s, re.I):
                            cmds.append(s)
                return cmds

            guessed = _guess_commands_from_text(alt_text or '')
            if guessed:
                alt_json = {'solutions': [{'id': f'alter-{retries}', 'commands': guessed}]}
            else:
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
