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
    allowed_bins = ['/usr/bin/systemctl', '/usr/bin/podman', '/usr/bin/tar', '/usr/bin/rsync', '/usr/bin/ansible-playbook', '/usr/bin/dnf', '/usr/bin/journalctl']
    allowed_services = ['mcp-bridge.service', 'mcp-sentinel.service', 'mcp-ai-collector.service', 'mcp-ai-collector.timer']

    results = []
    for c in commands:
        try:
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
            # execute the command
            proc = subprocess.run(c, shell=True, capture_output=True, text=True)
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
