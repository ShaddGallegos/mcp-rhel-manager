#!/usr/bin/env python3
"""
CLI wrapper to run MCP audio diagnostics and remediation tools.

Usage:
  python3 scripts/mcp_audio_cli.py check
  python3 scripts/mcp_audio_cli.py remediate

This loads `scripts/server.py` by path and invokes `mcp_audio_check` / `mcp_audio_remediate`.
"""
import sys
import os
import importlib.util

THIS_DIR = os.path.dirname(os.path.realpath(__file__))
SERVER_PATH = os.path.join(THIS_DIR, 'server.py')

def load_server():
    spec = importlib.util.spec_from_file_location('mcp_server', SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def main():
    if len(sys.argv) < 2:
        print('Usage: mcp_audio_cli.py check|remediate', file=sys.stderr)
        return 2
    cmd = sys.argv[1].lower()
    mod = load_server()
    if cmd in ('check', 'diagnose'):
        print(mod.mcp_audio_check())
        return 0
    if cmd in ('remediate', 'fix'):
        print(mod.mcp_audio_remediate())
        return 0
    print(f'Unknown command: {cmd}', file=sys.stderr)
    return 2

if __name__ == '__main__':
    sys.exit(main())
