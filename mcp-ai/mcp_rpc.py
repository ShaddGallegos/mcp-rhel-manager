#!/usr/bin/env python3
"""Wrapper to execute server module functions under the venv Python.

Usage: mcp_rpc.py <module> <function>
Prints a JSON object: {'rc':int, 'out': str, 'err': str}
"""
import sys, json
sys.path.insert(0, '/opt/mcp-rhel-manager')
sys.path.insert(0, '/home/sgallego/mcp-rhel-manager')

if len(sys.argv) < 3:
    print(json.dumps({'rc': -1, 'out': '', 'err': 'usage: mcp_rpc.py <module> <function>'}))
    sys.exit(2)

module = sys.argv[1]
func = sys.argv[2]
try:
    mod = __import__(module)
    fn = getattr(mod, func)
    out = fn()
    print(json.dumps({'rc': 0, 'out': str(out), 'err': ''}))
    sys.exit(0)
except Exception as e:
    print(json.dumps({'rc': -1, 'out': '', 'err': str(e)}))
    sys.exit(1)
