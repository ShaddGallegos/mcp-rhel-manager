#!/usr/bin/env python3
"""Interactive install assistant for optional components.

Presents a numeric menu (0 = back/exit) of optional installs and actions and
always shows pros/cons before running any commands. Uses a dry-run mode so the
script can be tested without making system changes.

Designed to be run from the repo root as:
  ./.venv/bin/python mcp-ai/install_assistant.py

Flags:
  --dry-run    : print actions without executing them
  --yes        : answer yes to prompts (non-interactive)
  --recommend  : one of 'embeddings', 'health', 'metrics', 'full' to preselect
                 recommended options (still asks for confirmation)

This script intentionally prints commands it will run and requires explicit
approval before executing privileged operations (e.g. systemctl with sudo).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from typing import Callable, List, Optional

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, '..'))


# Load the bundled dynamic menu helper without relying on package imports
def _load_dynamic_menu():
    path = os.path.join(HERE, 'dynamic_menu.py')
    spec = importlib.util.spec_from_file_location('dynamic_menu', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod

_menu = _load_dynamic_menu()
choose = _menu.choose


def find_venv_python():
    venv_python = os.path.join(REPO_ROOT, '.venv', 'bin', 'python')
    if os.path.exists(venv_python):
        return venv_python
    return sys.executable


def run_cmd(cmd: List[str], dry_run: bool) -> int:
    print('>>', shlex.join(cmd))
    if dry_run:
        return 0
    try:
        r = subprocess.run(cmd, check=True)
        return r.returncode
    except subprocess.CalledProcessError as e:
        print('Command failed with', e.returncode)
        return e.returncode


def pip_install(packages: List[str], dry_run: bool) -> int:
    py = find_venv_python()
    cmd = [py, '-m', 'pip', 'install'] + packages
    return run_cmd(cmd, dry_run)


def show_text_block(title: str, text: str):
    print('\n' + title)
    print('-' * len(title))
    print(text + '\n')


def confirm(prompt: str, default: bool, auto_yes: bool) -> bool:
    if auto_yes:
        print(f'{prompt} [auto-yes]')
        return True
    yn = 'Y/n' if default else 'y/N'
    try:
        r = input(f'{prompt} ({yn}): ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        print('\nAborted')
        return False
    if not r:
        return default
    return r[0] == 'y'


def option_embeddings(dry_run: bool, auto_yes: bool):
    show_text_block('Embeddings support',
                    'Installs `sentence-transformers`, `faiss-cpu` and `scikit-learn` into the project venv.\n'
                    'Pros: local embeddings, faster RAG, no external embedding API costs.\n'
                    'Cons: `faiss-cpu` can be heavy to build on some distros (requires cmake/openblas), large wheel size, may need system packages.\n')
    sub_opts = ['Install Python packages into venv', 'Show system package instructions (apt/yum)']
    sel = choose(sub_opts, prompt='Embeddings options', multi=False)
    if not sel:
        return
    if sel[0] == 0:
        pkgs = ['sentence-transformers>=2.2.2', 'faiss-cpu', 'scikit-learn>=1.2.0']
        print('Will install packages:', ', '.join(pkgs))
        if confirm('Proceed with pip install in venv?', default=False, auto_yes=auto_yes):
            pip_install(pkgs, dry_run)
        else:
            print('Skipped installing embeddings packages')
    else:
        distro_hints = (
            'Debian/Ubuntu:
  sudo apt update && sudo apt install -y build-essential cmake libopenblas-dev libomp-dev

RedHat/CentOS/RHEL:
  sudo yum groupinstall -y "Development Tools" && sudo yum install -y cmake openblas-devel libgomp-devel

If you prefer, use conda to install faiss (recommended on some platforms):
  conda install -c pytorch faiss-cpu'
        )
        print(distro_hints)


def option_precompute(dry_run: bool, auto_yes: bool):
    show_text_block('Precompute embeddings',
                    'Runs `mcp-ai/precompute_embeds.py` to build an on-disk FAISS index of profiles.\n'
                    'Requires embeddings support installed.\n')
    if not confirm('Run precompute now?', default=False, auto_yes=auto_yes):
        print('Skipped precompute')
        return
    py = find_venv_python()
    cmd = [py, os.path.join(REPO_ROOT, 'mcp-ai', 'precompute_embeds.py')]
    run_cmd(cmd, dry_run)


def option_prometheus(dry_run: bool, auto_yes: bool):
    show_text_block('Prometheus client',
                    'Installs `prometheus_client` into the venv for metrics export.\n'
                    'Pros: integrates with Prometheus for observability.\n'
                    'Cons: requires Prometheus server and scrape configuration to be useful.'
                    )
    if confirm('Install `prometheus_client` into venv?', default=False, auto_yes=auto_yes):
        pip_install(['prometheus_client'], dry_run)
    else:
        print('Skipped prometheus_client')


def option_enable_health(dry_run: bool, auto_yes: bool):
    show_text_block('Enable llm health systemd service',
                    'Enables and starts `mcp-ai-llm-health.service` via systemd (requires sudo).\n'
                    'Pros: service supervision and restart on failure.\n'
                    'Cons: requires systemd and sudo rights.'
                    )
    if not confirm('Enable and start the systemd service now?', default=False, auto_yes=auto_yes):
        print('Skipped enabling service')
        return
    cmds = [
        ['sudo', 'systemctl', 'daemon-reload'],
        ['sudo', 'systemctl', 'enable', '--now', 'mcp-ai-llm-health.service'],
        ['sudo', 'systemctl', 'status', 'mcp-ai-llm-health.service']
    ]
    for c in cmds:
        run_cmd(c, dry_run)


def option_install_requirements(dry_run: bool, auto_yes: bool):
    show_text_block('Install project requirements',
                    'Installs top-level `requirements.txt` into the venv.\n'
                    'Useful to ensure tests and other scripts run locally.')
    if confirm('Install requirements.txt into venv?', default=False, auto_yes=auto_yes):
        py = find_venv_python()
        req = os.path.join(REPO_ROOT, 'requirements.txt')
        if not os.path.exists(req):
            print('requirements.txt not found')
            return
        cmd = [py, '-m', 'pip', 'install', '-r', req]
        run_cmd(cmd, dry_run)
    else:
        print('Skipped requirements install')


def option_run_tests(dry_run: bool, auto_yes: bool):
    show_text_block('Run smoke tests', 'Runs the small mock tests in `mcp-ai/tests` (may require packages).')
    if not confirm('Run tests now?', default=False, auto_yes=auto_yes):
        print('Skipped tests')
        return
    py = find_venv_python()
    # Try to run pytest if available
    cmd = [py, '-m', 'pytest', os.path.join(REPO_ROOT, 'mcp-ai', 'test_llm_client.py')]
    run_cmd(cmd, dry_run)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true', help='Show commands without executing')
    p.add_argument('--yes', action='store_true', help='Auto-accept prompts')
    p.add_argument('--recommend', choices=['embeddings', 'health', 'metrics', 'full'], help='Preselect recommended options')
    args = p.parse_args()

    dry_run = args.dry_run
    auto_yes = args.yes

    # Top-level menu loop
    while True:
        opts = [
            'Embeddings support (sentence-transformers, faiss-cpu, scikit-learn)',
            'Precompute embeddings (run precompute_embeds.py)',
            'Prometheus metrics (prometheus_client)',
            'Enable/Start llm health systemd service',
            'Install project requirements (requirements.txt into venv)',
            'Run smoke tests (mcp-ai/test_llm_client.py)'
        ]
        sel = choose(opts, prompt='Optional components (0=exit/back)', multi=False)
        if not sel:
            print('Exiting installer assistant')
            return
        idx = sel[0]
        if idx == 0:
            option_embeddings(dry_run, auto_yes)
        elif idx == 1:
            option_precompute(dry_run, auto_yes)
        elif idx == 2:
            option_prometheus(dry_run, auto_yes)
        elif idx == 3:
            option_enable_health(dry_run, auto_yes)
        elif idx == 4:
            option_install_requirements(dry_run, auto_yes)
        elif idx == 5:
            option_run_tests(dry_run, auto_yes)


if __name__ == '__main__':
    main()
