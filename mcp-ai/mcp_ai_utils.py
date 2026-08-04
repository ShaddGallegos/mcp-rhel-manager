#!/usr/bin/env python3
"""Common utilities for mcp-ai scripts.

Small helpers for running commands, installing pip packages into the repo
venv, and simple interactive helpers used by installer scripts.
"""
from __future__ import annotations

import shlex
import subprocess
from typing import List

import mcp_ai_config as config


def run_cmd(cmd: List[str], dry_run: bool = False) -> int:
    print('>>', shlex.join(cmd))
    if dry_run:
        return 0
    try:
        r = subprocess.run(cmd, check=True)
        return r.returncode
    except subprocess.CalledProcessError as e:
        print('Command failed with', e.returncode)
        return e.returncode


def pip_install(packages: List[str], dry_run: bool = False, venv_python: str | None = None) -> int:
    if not venv_python:
        venv_python = config.get_venv_python()
    cmd = [venv_python, '-m', 'pip', 'install'] + packages
    return run_cmd(cmd, dry_run)


def show_text_block(title: str, text: str) -> None:
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
