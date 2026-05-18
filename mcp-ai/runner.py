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


DEFAULT_POLICY = {
    'allowed_bins': [
        '/usr/bin/systemctl',
        '/usr/bin/podman',
        '/usr/bin/tar',
        '/usr/bin/rsync',
        '/usr/bin/ansible-playbook',
        '/usr/bin/dnf',
        '/usr/bin/journalctl',
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

    commands = payload.get('commands', [])
    policy = _load_policy()
    allowed_bins = policy.get('allowed_bins', DEFAULT_POLICY['allowed_bins'])
    allowed_services = policy.get('allowed_services', DEFAULT_POLICY['allowed_services'])
    podman_patterns = _allowed_podman_patterns(policy)
    explicit_approved_images = _load_approved_podman_images(policy)

    results = []
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
            if exe not in allowed_bins:
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
            # execute the command
            proc = subprocess.run(parts, shell=False, capture_output=True, text=True)
            results.append({'cmd': c, 'rc': proc.returncode, 'out': proc.stdout, 'err': proc.stderr})
            log(f"EXEC {c} rc={proc.returncode}")
        except Exception as e:
            results.append({'cmd': c, 'rc': -1, 'out': '', 'err': str(e)})
            log(f"ERROR {c} {e}")

    outfn = os.path.join(AI_HOME, f'runner-results-{datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.json')
    with open(outfn, 'w', encoding='utf-8') as ofh:
        json.dump(results, ofh, indent=2)
    print(outfn)
    sys.exit(0)
