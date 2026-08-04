#!/usr/bin/env python3
"""Load YAML env file and print shell export statements.

Usage: eval "$(scripts/load_ansble_env.py)"
This prints `export KEY='value'` lines for each top-level key in the YAML.
Do NOT commit your real `~/.ansible/conf/env.yml` to git.
Uses environment variable `ANSIBLE_ENV_PATH` and defaults to
`~/.ansible/conf/env.yml`.
"""
import os
import sys

try:
    import yaml
except Exception:
    print("# PyYAML not available: install pyyaml to use this helper", file=sys.stderr)
    sys.exit(1)

env_path = os.path.expanduser(os.getenv('ANSIBLE_ENV_PATH', '~/.ansible/conf/env.yml'))
if not os.path.exists(env_path):
    # nothing to load
    sys.exit(0)

with open(env_path, 'r', encoding='utf-8') as f:
    data = yaml.safe_load(f) or {}

for k, v in data.items():
    if v is None:
        continue
    # Ensure key is shell-safe
    key = str(k).strip()
    val = str(v).replace("'", "'\\''")
    print(f"export {key}='{val}'")
