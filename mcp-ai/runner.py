#!/usr/bin/env python3
"""mcp-ai-runner: validate and execute allowed remediation commands.

Usage: mcp-ai-runner /path/to/commands.json
commands.json format: {"commands": ["/usr/bin/systemctl restart mcp-bridge.service"]}

Behavior:
 - Validates base executable against whitelist
 - For `systemctl`, restricts allowed services
 - Logs attempts to AI home and writes results JSON
"""
import sys
import os
import json
import shlex
import subprocess
import datetime
import fnmatch
import re
from pathlib import Path


DEFAULT_POLICY = {
    'allowed_bins': [
        '/usr/bin/systemctl',
        '/usr/bin/podman',
        '/usr/bin/tar',
        '/usr/bin/rsync',
        '/usr/bin/ansible-playbook',
        '/usr/bin/python3',
        '/bin/bash',
        '/usr/bin/dnf',
        '/usr/bin/journalctl',
        # Filesystem and helper utilities allowed for safe file operations
        '/bin/mkdir',
        '/bin/rm',
        '/bin/mv',
        '/usr/bin/install',
        '/bin/chmod',
        '/bin/chown',
        '/bin/ln',
        '/usr/bin/tee',
    ],
    'allowed_services': [
        'mcp-bridge.service',
        'mcp-sentinel.service',
        'mcp-manager.service',
        'mcp-ai-collector.service',
        'mcp-ai-collector.timer',
        'mcp-ai-remediator.service',
        'mcp-ai-remediator.path',
        'mcp-ai-hal-brain.service',
        'mcp-ai-dashboard.service',
        'mcp-ai-indexer.service',
        'mcp-ai-indexer.timer',
    ],
    'allowed_image_patterns': [
        'registry.redhat.io/*',
        'quay.io/redhat*/*',
        'quay.io/ansible*/*',
        'docker.io/library/*',
    ],
    'allowed_mcp_calls': [
        'server.*',
        'architect.*',
    ],
    'allowed_cmd_patterns': [],
    'approved_images_file': '~/.mcp-ai/approved-podman-images.json',
}


def find_ai_home():
    sudo_user = os.environ.get('SUDO_USER')
    candidates = []
    if sudo_user:
        candidates.append(os.path.join('/home', sudo_user, '.mcp-ai'))
    candidates.append(os.path.expanduser('~/.mcp-ai'))
    candidates.append('/root/.mcp-ai')
    for c in candidates:
        if c and os.path.exists(c):
            return c
    if sudo_user:
        path = os.path.join('/home', sudo_user, '.mcp-ai')
        os.makedirs(path, exist_ok=True)
        return path
    d = os.path.expanduser('~/.mcp-ai')
    os.makedirs(d, exist_ok=True)
    return d

AI_HOME = find_ai_home()
LOGFILE = os.path.join(AI_HOME, 'runner.log')


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z'
    with open(LOGFILE, 'a') as fh:
        fh.write(f"{ts} {msg}\n")


def _policy_candidates():
    """Ordered policy file candidates (first readable+valid wins)."""
    env_override = os.environ.get('MCP_AI_RUNNER_POLICY_FILE', '').strip()
    candidates = []
    if env_override:
        candidates.append(env_override)
    # system-level preferred location
    candidates.append('/etc/mcp-ai/runner-policy.json')
    # default install path used by this project
    candidates.append('/opt/mcp-rhel-manager/mcp-ai/runner-policy.json')
    # local development fallback
    candidates.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'runner-policy.json'))
    return candidates


def _load_policy():
    """Load runner policy from JSON and merge over safe defaults."""
    policy = {
        'allowed_bins': list(DEFAULT_POLICY['allowed_bins']),
        'allowed_services': list(DEFAULT_POLICY['allowed_services']),
        'allowed_image_patterns': list(DEFAULT_POLICY['allowed_image_patterns']),
        'approved_images_file': DEFAULT_POLICY['approved_images_file'],
    }
    for fn in _policy_candidates():
        try:
            if not fn or not os.path.exists(fn):
                continue
            with open(fn, 'r', encoding='utf-8') as fh:
                user_policy = json.load(fh)
            if not isinstance(user_policy, dict):
                continue
            for key in ('allowed_bins', 'allowed_services', 'allowed_image_patterns'):
                val = user_policy.get(key)
                if isinstance(val, list) and val:
                    policy[key] = [str(x).strip() for x in val if str(x).strip()]
            # optional global allowed command patterns
            valp = user_policy.get('allowed_cmd_patterns')
            if isinstance(valp, list) and valp:
                policy['allowed_cmd_patterns'] = [str(x).strip() for x in valp if str(x).strip()]
            approved_images_file = user_policy.get('approved_images_file')
            if isinstance(approved_images_file, str) and approved_images_file.strip():
                policy['approved_images_file'] = approved_images_file.strip()
            log(f'POLICY_LOADED {fn}')
            break
        except Exception as e:
            log(f'WARN policy load failed for {fn}: {e}')
    return policy


def _load_approved_podman_images(policy):
    """Load optional explicit podman image approvals from AI home.

    JSON format:
      {"images": ["quay.io/org/image:tag", "docker.io/library/busybox:latest"]}
    """
    approved_file = str(policy.get('approved_images_file', DEFAULT_POLICY['approved_images_file']))
    approved_file = os.path.expanduser(approved_file)
    if not os.path.isabs(approved_file):
        approved_file = os.path.join(AI_HOME, approved_file)
    fn = approved_file
    if not os.path.exists(fn):
        return set()
    try:
        with open(fn, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        images = payload.get('images', []) if isinstance(payload, dict) else []
        return {str(x).strip() for x in images if str(x).strip()}
    except Exception as e:
        log(f'WARN approved image file parse failed: {e}')
        return set()


def _allowed_podman_patterns(policy):
    """Return podman image whitelist patterns.

    Override with env var MCP_AI_ALLOWED_IMAGE_PATTERNS (comma-separated globs).
    """
    from_env = os.environ.get('MCP_AI_ALLOWED_IMAGE_PATTERNS', '').strip()
    if from_env:
        return [p.strip() for p in from_env.split(',') if p.strip()]
    patterns = policy.get('allowed_image_patterns', []) if isinstance(policy, dict) else []
    if isinstance(patterns, list) and patterns:
        return [str(p).strip() for p in patterns if str(p).strip()]
    return list(DEFAULT_POLICY['allowed_image_patterns'])


def _write_privileged_audit(entry: dict):
    try:
        repdir = os.path.join(AI_HOME, 'reports')
        os.makedirs(repdir, exist_ok=True)
        fn = os.path.join(repdir, 'privileged_actions.log')
        entry.setdefault('ts', datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z')
        try:
            import ingest_common
            try:
                ingest_common.append_jsonl(fn, entry)
            except Exception:
                with open(fn, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(entry) + '\n')
        except Exception:
            with open(fn, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry) + '\n')
    except Exception:
        pass


def _matches_allowed_pattern(cmd: str, patterns: list) -> bool:
    if not patterns:
        return False
    for p in patterns:
        if not p:
            continue
        try:
            if str(p).startswith('re:'):
                if re.search(str(p)[3:], cmd):
                    return True
            else:
                if fnmatch.fnmatch(cmd, str(p)):
                    return True
        except Exception:
            continue
    return False


def _is_allowed_mcp_call(uri: str, policy: dict) -> bool:
    """Validate an mcp-call:// URI against policy patterns.

    URI format: mcp-call://module.func (dotted path)
    """
    if not uri or not uri.startswith('mcp-call://'):
        return False
    target = uri[len('mcp-call://'):].strip()
    if not target or '.' not in target:
        return False
    patterns = policy.get('allowed_mcp_calls', DEFAULT_POLICY.get('allowed_mcp_calls', []))
    if not patterns:
        return False
    for pat in patterns:
        try:
            if fnmatch.fnmatch(target, pat) or fnmatch.fnmatch(target.split('.', 1)[0], pat):
                return True
        except Exception:
            continue
    return False


def _execute_mcp_call(uri: str):
    """Execute a mcp-call://module.func by invoking the project's venv python.

    Returns (rc, stdout, stderr)
    """
    target = uri[len('mcp-call://'):].strip()
    if not target or '.' not in target:
        raise RuntimeError('INVALID_MCP_CALL missing module.func')
    module, func = target.rsplit('.', 1)
    # Prefer project venv python if present
    venv_py = '/opt/mcp-rhel-manager/venv/bin/python'
    if not os.path.exists(venv_py):
        venv_py = '/usr/bin/python3'

    # Build a small wrapper script that imports the project module and calls the function.
    # Print JSON on success or error info on failure.
    wrapper = (
        'import sys, json\n'
        "sys.path.insert(0, '/opt/mcp-rhel-manager')\n"
        f"mod = __import__('{module}', fromlist=['{func}'])\n"
        f"fn = getattr(mod, '{func}')\n"
        "try:\n"
        "    out = fn()\n"
        "    # If function returns a string (often JSON), print it directly; else JSON-encode the result.\n"
        "    if isinstance(out, str):\n"
        "        print(out)\n"
        "    else:\n"
        "        try:\n"
        "            print(json.dumps(out, default=str))\n"
        "        except Exception:\n"
        "            print(str(out))\n"
        "except Exception as e:\n"
        "    import traceback\n"
        "    print(json.dumps({'error': str(e), 'trace': traceback.format_exc()}))\n"
    )

    proc = subprocess.run([venv_py, '-c', wrapper], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _is_image_allowed(image: str, patterns, explicit_approvals):
    image = (image or '').strip()
    if not image:
        return False
    if image in explicit_approvals:
        return True
    return any(fnmatch.fnmatch(image, pat) for pat in patterns)


def _validate_podman(parts, patterns, explicit_approvals):
    """Validate podman command and gate image pulls/runs by whitelist."""
    if len(parts) < 2:
        raise RuntimeError('DISALLOWED_PODMAN missing subcommand')
    subcmd = parts[1]
    allowed_subcmds = {'pull', 'run', 'stop', 'start', 'restart', 'rm', 'ps', 'images', 'inspect', 'logs'}
    if subcmd not in allowed_subcmds:
        raise RuntimeError(f'DISALLOWED_PODMAN_SUBCMD {subcmd}')

    # Protect image acquisition/launch paths.
    if subcmd == 'pull':
        if len(parts) < 3:
            raise RuntimeError('DISALLOWED_PODMAN pull missing image')
        image = parts[2]
        if not _is_image_allowed(image, patterns, explicit_approvals):
            raise RuntimeError(f'DISALLOWED_IMAGE {image}')


def _safe_fix_artifacts_root() -> str:
    """Absolute path for generated fix artifacts allowed for interpreter execution."""
    root = os.environ.get('MCP_AI_FIX_ARTIFACTS_DIR', '').strip()
    if root:
        return os.path.realpath(os.path.expanduser(root))
    return os.path.realpath(os.path.join(AI_HOME, 'fix-library', 'artifacts'))


def _validate_interpreter_artifact(parts):
    """Allow /usr/bin/python3 and /bin/bash only for generated local fix artifacts."""
    exe = parts[0]
    if exe not in ('/usr/bin/python3', '/bin/bash'):
        return
    if len(parts) < 2:
        raise RuntimeError(f'DISALLOWED_{os.path.basename(exe).upper()} missing script path')

    script_path = parts[1]
    if script_path.startswith('-'):
        raise RuntimeError(f'DISALLOWED_{os.path.basename(exe).upper()} flags are not allowed')

    abs_script = os.path.realpath(os.path.expanduser(script_path))
    allowed_root = _safe_fix_artifacts_root()
    if not abs_script.startswith(allowed_root + os.sep):
        raise RuntimeError(f'DISALLOWED_SCRIPT_PATH {abs_script}')
    if not os.path.isfile(abs_script):
        raise RuntimeError(f'SCRIPT_NOT_FOUND {abs_script}')

    if subcmd == 'run':
        # podman run [opts] IMAGE [cmd...]: first non-flag token after options is image
        image = ''
        i = 2
        while i < len(parts):
            tok = parts[i]
            if tok == '--':
                i += 1
                break
            if tok.startswith('-'):
                # Best-effort skip value-taking flags used in this environment.
                if tok in ('--name', '--network', '--hostname', '--env', '-e', '-p', '--publish', '-v', '--volume') and i + 1 < len(parts):
                    i += 2
                    continue
                i += 1
                continue
            image = tok
            break
        if not image:
            raise RuntimeError('DISALLOWED_PODMAN run missing image')
        if not _is_image_allowed(image, patterns, explicit_approvals):
            raise RuntimeError(f'DISALLOWED_IMAGE {image}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: mcp-ai-runner /path/to/commands.json', file=sys.stderr)
        sys.exit(2)
    inp = sys.argv[1]
    if not os.path.exists(inp):
        print('Input file not found', file=sys.stderr)
        sys.exit(2)
    try:
        with open(inp, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
    except Exception as e:
        print('Invalid input JSON: ' + str(e), file=sys.stderr)
        sys.exit(3)

    # optional metadata: entry and solution allow the runner to consult approvals
    entry_meta = payload.get('entry')
    solution_meta = payload.get('solution')
    approval_patterns = []
    approval_solution_patterns = {}
    approval_file = None
    approval_approved_by = None
    if entry_meta:
        try:
            plan_stem = Path(entry_meta).stem
            approvals_dir = os.path.join(AI_HOME, 'approvals')
            candidate = os.path.join(approvals_dir, f'plan-{plan_stem}.approved.json')
            if os.path.exists(candidate):
                approval_file = candidate
                with open(candidate, 'r', encoding='utf-8') as afh:
                    apr = json.load(afh)
                approval_patterns = apr.get('allowed_cmd_patterns', []) or []
                approval_solution_patterns = apr.get('allowed_cmd_patterns_by_solution', {}) or {}
                approval_approved_by = apr.get('approved_by')
                log(f'APPROVAL_LOADED {candidate}')
        except Exception as e:
            log(f'WARN approval load failed: {e}')

    commands = payload.get('commands', [])
    policy = _load_policy()
    allowed_bins = policy.get('allowed_bins', DEFAULT_POLICY['allowed_bins'])
    allowed_services = policy.get('allowed_services', DEFAULT_POLICY['allowed_services'])
    podman_patterns = _allowed_podman_patterns(policy)
    explicit_approved_images = _load_approved_podman_images(policy)

    results = []
    # Combine global policy patterns and optional approval-derived patterns
    merged_patterns = []
    merged_patterns.extend(policy.get('allowed_cmd_patterns', []) or [])
    merged_patterns.extend(approval_patterns or [])
    # If solution-specific patterns exist, merge those for the provided solution id
    if solution_meta and isinstance(approval_solution_patterns, dict):
        sp = approval_solution_patterns.get(solution_meta)
        if sp:
            merged_patterns.extend(sp)

    for c in commands:
        try:
            # Special-case: handle mcp-call:// URIs directly (validated via policy)
            if isinstance(c, str) and c.strip().startswith('mcp-call://'):
                uri = c.strip()
                if not _is_allowed_mcp_call(uri, policy):
                    msg = f"DISALLOWED_MCP_CALL {uri}"
                    log(msg)
                    print(msg, file=sys.stderr)
                    sys.exit(8)
                try:
                    rc, out, err = _execute_mcp_call(uri)
                    results.append({'cmd': c, 'rc': rc, 'out': out, 'err': err})
                    log(f"MCP_CALL {c} rc={rc}")
                except Exception as e:
                    results.append({'cmd': c, 'rc': -1, 'out': '', 'err': str(e)})
                    log(f"MCP_CALL_ERROR {c} {e}")
                continue

            parts = shlex.split(c)
            if not parts:
                raise RuntimeError('empty command')
            exe = parts[0]

            # If command matches an allowed pattern (policy or approval), bypass binary whitelist
            skip_validation = False
            try:
                if _matches_allowed_pattern(c, merged_patterns):
                    skip_validation = True
                    log(f'PATTERN_ALLOW {c}')
                    # write privileged audit record (allowed via pattern)
                    _write_privileged_audit({'cmd': c, 'entry': entry_meta, 'solution': solution_meta, 'allowed_by': 'approval_pattern' if approval_file else 'policy_pattern', 'approval_file': approval_file, 'approved_by': approval_approved_by})
            except Exception:
                skip_validation = False
            if not skip_validation and exe not in allowed_bins:
                msg = f"DISALLOWED_EXE {exe} CMD {c}"
                log(msg)
                print(msg, file=sys.stderr)
                sys.exit(4)
            if exe == '/usr/bin/systemctl':
                if len(parts) < 3 or parts[1] not in ('start', 'stop', 'restart', 'status', 'enable', 'disable'):
                    msg = f"DISALLOWED_SYSC_CMD {c}"
                    log(msg)
                    print(msg, file=sys.stderr)
                    sys.exit(5)
                if parts[2] not in allowed_services:
                    msg = f"DISALLOWED_SERVICE {parts[2]} CMD {c}"
                    log(msg)
                    print(msg, file=sys.stderr)
                    sys.exit(6)
            if exe == '/usr/bin/podman':
                try:
                    _validate_podman(parts, podman_patterns, explicit_approved_images)
                except Exception as e:
                    msg = f"{e} CMD {c}"
                    log(msg)
                    print(msg, file=sys.stderr)
                    sys.exit(7)
            if exe in ('/usr/bin/python3', '/bin/bash'):
                try:
                    _validate_interpreter_artifact(parts)
                except Exception as e:
                    msg = f"{e} CMD {c}"
                    log(msg)
                    print(msg, file=sys.stderr)
                    sys.exit(9)
            # execute the command
            proc = subprocess.run(parts, shell=False, capture_output=True, text=True)
            results.append({'cmd': c, 'rc': proc.returncode, 'out': proc.stdout, 'err': proc.stderr})
            log(f"EXEC {c} rc={proc.returncode}")
            # Audit executed privileged action with metadata
            try:
                _write_privileged_audit({'cmd': c, 'rc': proc.returncode, 'out': (proc.stdout or '')[:2000], 'err': (proc.stderr or '')[:2000], 'entry': entry_meta, 'solution': solution_meta, 'approval_file': approval_file, 'approved_by': approval_approved_by})
            except Exception:
                pass
        except Exception as e:
            results.append({'cmd': c, 'rc': -1, 'out': '', 'err': str(e)})
            log(f"ERROR {c} {e}")

    outfn = os.path.join(AI_HOME, f'runner-results-{datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.json')
    with open(outfn, 'w', encoding='utf-8') as ofh:
        json.dump(results, ofh, indent=2)
    print(outfn)
    sys.exit(0)
