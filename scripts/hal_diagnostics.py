#!/usr/bin/env python3
"""HAL diagnostics helpers extracted from `scripts/hal.py`.

This module contains a small set of self-contained diagnostics and
auto-healing helpers so they can be tested independently and imported
by the main `hal.py` CLI as lightweight wrappers.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import List, Tuple


def _verify_auto_update_timer() -> Tuple[List[str], List[str]]:
    """Verify HAL auto-update timer is properly configured.

    Returns a tuple of (repaired, errors).
    """
    repaired: List[str] = []
    errors: List[str] = []

    try:
        # Check if timer service exists in several likely locations
        script_dir = os.path.dirname(os.path.realpath(__file__))
        candidates = [
            os.path.join(script_dir, 'hal-auto-update.timer'),
            os.path.join(script_dir, '..', 'hal-auto-update.timer'),
            os.path.join(script_dir, '..', 'systemd', 'hal-auto-update.timer'),
            os.path.join(script_dir, '..', '..', 'hal-auto-update.timer'),
            os.path.join(os.getcwd(), 'hal-auto-update.timer'),
            '/etc/systemd/system/hal-auto-update.timer',
            '/lib/systemd/system/hal-auto-update.timer',
        ]

        timer_file = None
        for c in candidates:
            try:
                p = os.path.abspath(os.path.expanduser(c))
            except Exception:
                p = c
            if os.path.isfile(p):
                timer_file = p
                break

        if not timer_file:
            errors.append('hal-auto-update.timer file not found')
            return repaired, errors

        repaired.append(f'hal-auto-update.timer configuration file exists ({timer_file})')

        # Try to check systemd timer status (if running with sudo)
        try:
            status_result = subprocess.run(
                ['systemctl', 'is-enabled', 'hal-auto-update.timer'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if status_result.returncode == 0:
                repaired.append('hal-auto-update.timer is enabled')
            else:
                errors.append('hal-auto-update.timer is not enabled (run: sudo systemctl enable hal-auto-update.timer)')
        except Exception:
            # Non-root, just note the file exists
            repaired.append('(timer status check requires sudo)')

    except Exception as e:
        errors.append(f'failed to verify auto-update timer: {e}')

    return repaired, errors


def _manage_vault_password() -> Tuple[List[str], List[str]]:
    """Ensure ANSIBLE_VAULT_PASSWORD_FILE exists and has secure perms."""
    repaired: List[str] = []
    errors: List[str] = []

    try:
        home = os.path.expanduser('~')
        vault_dir = os.path.join(home, '.ansible', 'conf')
        vault_file = os.path.join(vault_dir, '.vaultpass.txt')

        # Create directory if needed
        if not os.path.isdir(vault_dir):
            try:
                os.makedirs(vault_dir, mode=0o700, exist_ok=True)
                repaired.append(f'created {vault_dir} with secure permissions')
            except Exception as e:
                errors.append(f'failed to create vault directory: {e}')
                return repaired, errors

        # Check if vault password file exists
        if os.path.isfile(vault_file):
            # Verify it has content
            try:
                with open(vault_file, 'r') as f:
                    content = f.read().strip()
                    if content:
                        repaired.append('vault password file already exists and contains a password')
                    else:
                        errors.append('vault password file exists but is empty')
            except Exception as e:
                errors.append(f'failed to read vault password: {e}')

            # Ensure correct permissions (600)
            try:
                stat = os.stat(vault_file)
                if stat.st_mode & 0o777 != 0o600:
                    os.chmod(vault_file, 0o600)
                    repaired.append('corrected vault password file permissions to 600')
            except Exception as e:
                errors.append(f'failed to fix vault password permissions: {e}')
        else:
            # Generate new secure password (32 random chars)
            import secrets
            import string

            password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))

            try:
                with open(vault_file, 'w') as f:
                    f.write(password)
                os.chmod(vault_file, 0o600)
                repaired.append(f'created new vault password at {vault_file}')
                repaired.append('vault password generated with 32 secure random characters')
            except Exception as e:
                errors.append(f'failed to create vault password file: {e}')

    except Exception as e:
        errors.append(f'failed to manage vault password: {e}')

    return repaired, errors


def _scan_git_repos_for_secrets() -> Tuple[List[str], List[str]]:
    """Scan git repositories for common unencrypted secrets patterns."""
    repaired: List[str] = []
    errors: List[str] = []

    try:
        home = os.path.expanduser('~')
        secrets_found: List[str] = []
        repos_scanned = 0

        # Common patterns that indicate secrets
        secret_patterns = [
            r'password\s*[:=]\s*["\']?\w+["\']?',
            r'api[_-]?key\s*[:=]\s*["\']?[a-zA-Z0-9]+["\']?',
            r'secret\s*[:=]\s*["\']?\w+["\']?',
            r'token\s*[:=]\s*["\']?[a-zA-Z0-9]+["\']?',
            r'aws[_-]?secret\s*[:=]',
            r'private[_-]?key',
            r'\.pem\s*$',
            r'\.key\s*$',
        ]

        def find_git_repos(start_path, max_depth=3, current_depth=0):
            repos = []
            if current_depth >= max_depth:
                return repos
            try:
                for entry in os.listdir(start_path):
                    entry_path = os.path.join(start_path, entry)
                    if os.path.isdir(entry_path):
                        if entry in {'.git', 'git'}:
                            repos.append(os.path.dirname(entry_path))
                        elif not entry.startswith('.') and entry not in {'__pycache__', 'node_modules', '.venv', 'venv'}:
                            repos.extend(find_git_repos(entry_path, max_depth, current_depth + 1))
            except (PermissionError, OSError):
                pass
            return repos

        git_repos = find_git_repos(home, max_depth=4)
        repos_scanned = len(git_repos)

        for repo_path in git_repos[:10]:
            try:
                result = subprocess.run(
                    ['git', 'ls-files'],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    continue

                tracked_files = result.stdout.strip().split('\n')
                for tracked_file in tracked_files[:100]:
                    file_path = os.path.join(repo_path, tracked_file)
                    if not os.path.isfile(file_path):
                        continue

                    try:
                        stat = os.stat(file_path)
                        if stat.st_size > 1000000:
                            continue
                    except OSError:
                        continue

                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            for pattern in secret_patterns:
                                if re.search(pattern, content, re.IGNORECASE):
                                    secrets_found.append(f'{tracked_file} (repo: {os.path.basename(repo_path)})')
                                    break
                    except (IOError, OSError):
                        pass

            except Exception:
                pass

        if repos_scanned > 0:
            repaired.append(f'scanned {repos_scanned} git repository(ies) for secrets')

        if secrets_found:
            errors.append(f'found {len(secrets_found)} file(s) with potential unencrypted secrets:')
            for secret_file in secrets_found[:5]:
                errors.append(f'  → {secret_file}')
            if len(secrets_found) > 5:
                errors.append(f'  ... and {len(secrets_found) - 5} more')
        else:
            repaired.append('no obvious unencrypted secrets detected in tracked files')

    except Exception as e:
        errors.append(f'failed to scan git repos for secrets: {e}')

    return repaired, errors
