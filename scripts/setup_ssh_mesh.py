#!/usr/bin/env python3
"""Provision SSH key-based access to a set of hosts non-interactively.

Features:
- Prefer `paramiko` (pure-Python) if available for direct SFTP/SSH operations.
- Fallback to `ssh-copy-id` or `ssh` + `sshpass` when `paramiko` is not installed.
- Accept passwords via `--password` or `HAL_SSH_PASSWORD`/`SSH_PASS` env var for non-interactive use.
- Accept a comma-separated host list or a host file (one host per line). Hosts may be `user@host`.

Usage examples:
  # Use default public key (~/.ssh/id_ed25519.pub or id_rsa.pub)
  python3 scripts/setup_ssh_mesh.py --hosts host1,host2 --user root --password Secr3t

  # Use a pubkey file and a hostfile
  python3 scripts/setup_ssh_mesh.py --hosts-file hosts.txt --pubkey ~/.ssh/mykey.pub

Notes:
- For password-based non-interactive installs this script will try to use `sshpass` if present.
- Installing `paramiko` (pip install paramiko) gives the most robust behavior.
"""

from __future__ import annotations

import argparse
import os
import sys
import shutil
import subprocess
import socket
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None


def find_default_pubkey() -> Path | None:
    home = Path.home()
    candidates = [home / '.ssh' / 'id_ed25519.pub', home / '.ssh' / 'id_rsa.pub', home / '.ssh' / 'id_ecdsa.pub']
    for p in candidates:
        if p.exists():
            return p
    return None


def read_pubkey(path: Path | str) -> str:
    with open(str(path), 'r', encoding='utf-8') as fh:
        data = fh.read().strip()
    return data


def parse_hosts(hosts_arg: str | None, hosts_file: str | None) -> list[str]:
    hosts: list[str] = []
    if hosts_file:
        p = Path(hosts_file)
        if not p.exists():
            raise SystemExit(f'Hosts file not found: {hosts_file}')
        for line in p.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            hosts.append(line)
    if hosts_arg:
        for h in hosts_arg.split(','):
            h = h.strip()
            if h:
                hosts.append(h)
    # dedupe while preserving order
    seen = set()
    out = []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _default_user() -> str:
    for env_name in ('SUDO_USER', 'LOGNAME', 'USER', 'USERNAME'):
        value = os.environ.get(env_name, '').strip()
        if value:
            return value
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return 'root'


def _load_ansible_env(path: str | None = None) -> dict[str, str]:
    candidates = []
    if path:
        candidates.append(Path(path).expanduser())
    env_path = os.environ.get('ANSIBLE_ENV_PATH')
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(Path.home() / '.ansible' / 'conf' / 'env.yml')
    for candidate in candidates:
        try:
            if not candidate.exists() or not candidate.is_file():
                continue
            if yaml is not None:
                data = yaml.safe_load(candidate.read_text(encoding='utf-8')) or {}
                if isinstance(data, dict):
                    flat: dict[str, str] = {}
                    for key, value in data.items():
                        if isinstance(value, dict):
                            for nested_key, nested_value in value.items():
                                if nested_value is not None:
                                    flat[str(nested_key)] = str(nested_value)
                        elif value is not None:
                            flat[str(key)] = str(value)
                    return flat
        except Exception:
            continue
    return {}


def try_paramiko(host: str, user: str, port: int, pubkey_str: str, password: str | None, key_filename: str | None, timeout: int = 10) -> tuple[bool, str]:
    try:
        import paramiko
    except Exception:
        return False, 'paramiko not installed'

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs = dict(hostname=host, port=port, username=user, timeout=timeout, allow_agent=True, look_for_keys=True)
        if password:
            connect_kwargs['password'] = password
        if key_filename:
            connect_kwargs['key_filename'] = key_filename

        client.connect(**connect_kwargs)
        sftp = client.open_sftp()
        try:
            # Ensure ~/.ssh exists
            try:
                sftp.stat('.ssh')
            except IOError:
                sftp.mkdir('.ssh', mode=0o700)
            # Read existing authorized_keys (if any)
            auth_path = '.ssh/authorized_keys'
            existing = ''
            try:
                with sftp.open(auth_path, 'r') as fh:
                    raw = fh.read()
                    if isinstance(raw, bytes):
                        existing = raw.decode('utf-8', errors='ignore')
                    else:
                        existing = raw
            except IOError:
                existing = ''
            if pubkey_str.strip() not in existing:
                with sftp.open(auth_path, 'a') as fh:
                    if isinstance(pubkey_str, str):
                        fh.write(pubkey_str.rstrip() + '\n')
                    else:
                        fh.write(str(pubkey_str).rstrip() + '\n')
            try:
                sftp.chmod('.ssh', 0o700)
                sftp.chmod(auth_path, 0o600)
            except Exception:
                pass
        finally:
            sftp.close()
            client.close()
        return True, 'paramiko: key installed'
    except paramiko.AuthenticationException:
        return False, 'paramiko: authentication failed'
    except (paramiko.SSHException, socket.error) as e:
        return False, f'paramiko: SSH error: {e}'
    except Exception as e:
        return False, f'paramiko: error: {e}'


def run_subprocess(cmd: list[str], input_text: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, input=input_text, text=True, capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout or '', proc.stderr or ''
    except subprocess.TimeoutExpired as e:
        return 124, '', f'timeout: {e}'
    except Exception as e:
        return 1, '', str(e)


def try_ssh_copy_id(host: str, user: str, port: int, pubkey_path: str, password: str | None, timeout: int = 60) -> tuple[bool, str]:
    scid = shutil.which('ssh-copy-id')
    sshpass = shutil.which('sshpass')
    target = f'{user}@{host}'

    if scid:
        if password and sshpass:
            cmd = ['sshpass', '-p', password, scid, '-i', pubkey_path, '-p', str(port), target]
        else:
            cmd = [scid, '-i', pubkey_path, '-p', str(port), target]
        rc, out, err = run_subprocess(cmd, timeout=timeout)
        if rc == 0:
            return True, out.strip() or 'ssh-copy-id succeeded'
        return False, f'ssh-copy-id failed: {err.strip() or out.strip()} (rc={rc})'

    # Fallback to ssh + cat (requires sshpass for password mode)
    ssh = shutil.which('ssh')
    if not ssh:
        return False, 'ssh not found on PATH'

    remote_cmd = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    if password and sshpass:
        cmd = ['sshpass', '-p', password, ssh, '-p', str(port), '-o', 'StrictHostKeyChecking=no', f'{user}@{host}', remote_cmd]
        rc, out, err = run_subprocess(cmd, input_text=read_pubkey(pubkey_path), timeout=timeout)
        if rc == 0:
            return True, out.strip() or 'sshpass+ssh succeeded'
        return False, f'sshpass+ssh failed: {err.strip() or out.strip()} (rc={rc})'
    elif not password:
        cmd = [ssh, '-p', str(port), '-o', 'StrictHostKeyChecking=no', f'{user}@{host}', remote_cmd]
        rc, out, err = run_subprocess(cmd, input_text=read_pubkey(pubkey_path), timeout=timeout)
        if rc == 0:
            return True, out.strip() or 'ssh succeeded'
        return False, f'ssh failed: {err.strip() or out.strip()} (rc={rc})'
    else:
        return False, 'password provided but sshpass not found; install sshpass or paramiko for non-interactive password mode'


def normalize_host(h: str) -> tuple[str, str, int]:
    """Return (host, user, port). Input may be user@host:port or host:port or host."""
    user = None
    port = 22
    host = h.strip()
    if '@' in host:
        user, host = host.split('@', 1)
    if ':' in host:
        # support IPv6 in brackets [::1]:22
        if host.startswith('[') and ']:' in host:
            hp, p = host.split(']:', 1)
            host = hp.lstrip('[')
            try:
                port = int(p)
            except Exception:
                port = 22
        else:
            parts = host.rsplit(':', 1)
            try:
                port = int(parts[1])
                host = parts[0]
            except Exception:
                pass
    return host, user or '', port


def main() -> int:
    ap = argparse.ArgumentParser(description='Provision SSH key-based access to multiple hosts non-interactively')
    ap.add_argument('--hosts', help='Comma-separated host list (user@host or host[:port])')
    ap.add_argument('--hosts-file', help='File with hosts, one per line')
    ap.add_argument('--user', help='Remote username (overrides user@host entries)')
    ap.add_argument('--pubkey', help='Public key file to install (default: first available under ~/.ssh)')
    ap.add_argument('--key-file', help='Private key filename to use for SSH auth when connecting (optional)')
    ap.add_argument('--password', help='Remote account password (use env HAL_SSH_PASSWORD or SSH_PASS to avoid putting on CLI)')
    ap.add_argument('--timeout', type=int, default=15, help='Connection timeout seconds (default: 15)')
    ap.add_argument('--check', action='store_true', help='Only check whether key is already installed')
    ap.add_argument('--yes', '-y', action='store_true', help='Assume yes for any prompts')
    args = ap.parse_args()

    pubkey_path = None
    if args.pubkey:
        pubkey_path = os.path.expanduser(args.pubkey)
        if not os.path.exists(pubkey_path):
            print(f'Public key not found: {pubkey_path}', file=sys.stderr)
            return 2
    else:
        found = find_default_pubkey()
        if not found:
            print('No public key found under ~/.ssh (generate with ssh-keygen or pass --pubkey)', file=sys.stderr)
            return 2
        pubkey_path = str(found)

    hosts = parse_hosts(args.hosts, args.hosts_file)
    if not hosts:
        print('No hosts provided. Use --hosts or --hosts-file', file=sys.stderr)
        return 2

    env_values = _load_ansible_env()
    password = (
        args.password
        or os.environ.get('HAL_SSH_PASSWORD')
        or os.environ.get('SSH_PASS')
        or env_values.get('HAL_SSH_PASSWORD')
        or env_values.get('SSH_PASS')
        or env_values.get('SSH_PASSWORD')
    )

    pubkey_str = read_pubkey(Path(pubkey_path))

    overall_ok = True
    for raw in hosts:
        host, maybe_user, port = normalize_host(raw)
        user = args.user or maybe_user or _default_user()
        print(f'-> {user}@{host}:{port}')

        # First try paramiko path
        ok, msg = try_paramiko(host, user, port, pubkey_str, password, args.key_file, timeout=args.timeout)
        if ok:
            print('   ✓', msg)
            continue
        else:
            print('   - paramiko:', msg)

        if args.check:
            ssh = shutil.which('ssh')
            if not ssh:
                print('   ✗ cannot check: ssh not available', file=sys.stderr)
                overall_ok = False
                continue
            remote_cmd = f'grep -F "{pubkey_str.split()[0]}" ~/.ssh/authorized_keys >/dev/null && echo present || echo absent'
            cmd = [ssh, '-p', str(port), '-o', 'BatchMode=yes', f'{user}@{host}', remote_cmd]
            rc, out, err = run_subprocess(cmd, timeout=args.timeout)
            if rc == 0 and out.strip() == 'present':
                print('   ✓ key already present')
                continue
            print('   ✗ key not present (check failed)')
            overall_ok = False
            continue

        ok2, msg2 = try_ssh_copy_id(host, user, port, pubkey_path, password, timeout=args.timeout)
        if ok2:
            print('   ✓', msg2)
            continue
        else:
            print('   ✗ fallback failed:', msg2)
            overall_ok = False

    return 0 if overall_ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
