#!/usr/bin/env python3
"""Utility helpers to run commands with `sudo` while prompting the user for a password.

Provides safe defaults that prefer the current TTY for interactive prompts and
fall back to a pty spawn when needed. Avoids storing passwords; uses the
system `sudo` credential cache (via `sudo -v`).

Usage:
    from scripts.sudo_helper import run_privileged
    run_privileged(['systemctl', 'restart', 'my-service'])
"""
from __future__ import annotations

import os
import sys
import subprocess
import pty
import typing as t


def _has_tty() -> bool:
    """Return True if this process has an attached interactive TTY."""
    try:
        return bool(sys.stdin.isatty() or sys.stdout.isatty() or os.path.exists('/dev/tty'))
    except Exception:
        return False


def ensure_sudo() -> bool:
    """Ensure the user has a valid sudo session.

    This will prompt for the user's password if needed (attached TTY required).
    Returns True on success, False otherwise.
    """
    try:
        if _has_tty():
            # This will prompt on the terminal if credentials are expired
            subprocess.check_call(['sudo', '-v'])
        else:
            # Try a pty spawn as a fallback so the password prompt can attach to a terminal
            pty.spawn(['sudo', '-v'])
        return True
    except Exception:
        return False


def run_privileged(cmd: t.List[str], check: bool = True, timeout: int | None = None, capture_output: bool = False):
    """Run `cmd` with sudo, prompting the user for their password if necessary.

    - `cmd` is a list of argv elements (e.g. ['systemctl', 'restart', 'svc']).
    - If a TTY is available, the command is run directly so `sudo` prompts normally.
    - Otherwise, falls back to `pty.spawn` to allow an interactive prompt.

    Returns the subprocess.CompletedProcess when run via subprocess, or the
    integer exit code when using `pty.spawn`.
    """
    if not ensure_sudo():
        raise RuntimeError('sudo authentication failed or was cancelled')

    if _has_tty():
        # When a TTY is present, run and optionally capture output
        if capture_output:
            return subprocess.run(['sudo'] + cmd, check=check, timeout=timeout, capture_output=True, text=True)
        return subprocess.run(['sudo'] + cmd, check=check, timeout=timeout)

    # No obvious TTY: use pty spawn so the password prompt can be interactive
    # No TTY: spawn pty so sudo can prompt; cannot capture output reliably via pty.spawn
    rc = pty.spawn(['sudo'] + cmd)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, ['sudo'] + cmd)
    return rc


__all__ = ['run_privileged', 'ensure_sudo', '_has_tty']
