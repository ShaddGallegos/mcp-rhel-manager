#!/usr/bin/env python3
"""Wrapper to execute server module functions under the venv Python.

Usage: mcp_rpc.py <module> <function>
Prints a JSON object: {'rc':int, 'out': str, 'err': str}
"""
import sys
import json
import os
import time
import traceback
import importlib
_script_dir = os.path.dirname(os.path.realpath(__file__))
_repo_root = os.path.dirname(_script_dir)
sys.path.insert(0, '/opt/mcp-rhel-manager')
sys.path.insert(0, _repo_root)


def _parse_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


TIMEOUT_SEC = _parse_int_env('MCP_RPC_TIMEOUT_SEC', 30)
RETRIES = _parse_int_env('MCP_RPC_RETRIES', 1)

if len(sys.argv) < 3:
    print(json.dumps({'rc': -1, 'out': '', 'err': 'usage: mcp_rpc.py <module> <function>'}))
    sys.exit(2)

module = sys.argv[1]
func = sys.argv[2]
last_err = None
last_tb = ''

for attempt in range(1, max(1, RETRIES) + 1):
    started = time.time()
    try:
        mod = importlib.import_module(module)
        fn = getattr(mod, func)
        out = fn()
        elapsed = time.time() - started
        if elapsed > TIMEOUT_SEC:
            raise TimeoutError(f'mcp_rpc call exceeded timeout ({elapsed:.2f}s > {TIMEOUT_SEC}s)')
        print(json.dumps({'rc': 0, 'out': str(out), 'err': '', 'attempt': attempt, 'elapsed_sec': round(elapsed, 4)}))
        sys.exit(0)
    except Exception as e:
        last_err = e
        last_tb = traceback.format_exc()
        if attempt < max(1, RETRIES):
            # Lightweight exponential backoff
            time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
            continue

print(json.dumps({
    'rc': -1,
    'out': '',
    'err': str(last_err) if last_err else 'unknown error',
    'attempts': max(1, RETRIES),
    'module': module,
    'function': func,
    'traceback': last_tb,
}))
sys.exit(1)
