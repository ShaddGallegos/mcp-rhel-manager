#!/usr/bin/env python3
"""
HAL Security Audit & Encryption Tool
Finds unencrypted secrets in git repos and encrypts them using ansible-vault
"""

import os
import sys
import subprocess
import json
import argparse
import re
from pathlib import Path

VAULT_PASS_FILE = os.path.expanduser('~/.ansible/conf/.vaultpass.txt')

SECRET_PATTERNS = {
    'password': r'password\s*[:=]\s*["\']?(\w+)["\']?',
    'api_key': r'api[_-]?key\s*[:=]\s*["\']?([a-zA-Z0-9]+)["\']?',
    'secret': r'secret\s*[:=]\s*["\']?(\w+)["\']?',
    'token': r'token\s*[:=]\s*["\']?([a-zA-Z0-9]+)["\']?',
    'aws_secret': r'aws[_-]?secret\s*[:=]',
    'private_key': r'private[_-]?key',
}

SENSITIVE_FILE_PATTERNS = [
    '*.pem',
    '*.key',
    '*.crt',
    '*credentials*',
    '*secret*',
    '*password*',
    '*.pfx',
    '*.p12',
]


def check_vault_password() -> bool:
    """Verify vault password file exists and is readable."""
    if not os.path.isfile(VAULT_PASS_FILE):
        print(f"ERROR: Vault password file not found at {VAULT_PASS_FILE}")
        print("Run this to create it: python3 hal.py 'system health' -> mode 3")
        return False
    
    try:
        with open(VAULT_PASS_FILE, 'r') as f:
            pwd = f.read().strip()
            if not pwd:
                print(f"ERROR: Vault password file is empty: {VAULT_PASS_FILE}")
                return False
        return True
    except Exception as e:
        print(f"ERROR: Cannot read vault password: {e}")
        return False


def find_git_repos(start_path: str, max_depth: int = 3) -> list:
    """Find all git repositories."""
    repos = []
    
    def _search(path, depth=0):
        if depth >= max_depth:
            return
        try:
            for entry in os.listdir(path):
                entry_path = os.path.join(path, entry)
                if os.path.isdir(entry_path) and not entry.startswith('.'):
                    if os.path.isdir(os.path.join(entry_path, '.git')):
                        repos.append(entry_path)
                    elif entry not in {'__pycache__', 'node_modules', '.venv', 'venv'}:
                        _search(entry_path, depth + 1)
        except (PermissionError, OSError):
            pass
    
    _search(start_path)
    return repos


def scan_file_for_secrets(file_path: str) -> dict:
    """Scan a file for secret patterns."""
    findings = {}
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            for secret_type, pattern in SECRET_PATTERNS.items():
                matches = re.findall(pattern, content, re.IGNORECASE)
                if matches:
                    findings[secret_type] = len(matches)
    except Exception:
        pass
    
    return findings


def scan_repo(repo_path: str, limit: int = 100) -> dict:
    """Scan a git repo for secrets."""
    results = {
        'repo': repo_path,
        'files_with_secrets': [],
        'total_issues': 0,
    }
    
    try:
        # Get tracked files
        result = subprocess.run(
            ['git', 'ls-files'],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode != 0:
            return results
        
        tracked_files = result.stdout.strip().split('\n')
        
        for tracked_file in tracked_files[:limit]:
            file_path = os.path.join(repo_path, tracked_file)
            if not os.path.isfile(file_path):
                continue
            
            # Skip large files
            try:
                if os.path.getsize(file_path) > 1000000:
                    continue
            except OSError:
                continue
            
            findings = scan_file_for_secrets(file_path)
            if findings:
                results['files_with_secrets'].append({
                    'file': tracked_file,
                    'issues': findings,
                })
                results['total_issues'] += sum(findings.values())
    
    except Exception as e:
        results['error'] = str(e)
    
    return results


def encrypt_file(file_path: str, vault_pass_file: str = VAULT_PASS_FILE) -> bool:
    """Encrypt a file using ansible-vault."""
    if not os.path.isfile(vault_pass_file):
        print(f"ERROR: Vault password file not found: {vault_pass_file}")
        return False
    
    try:
        result = subprocess.run(
            ['ansible-vault', 'encrypt', '--vault-password-file', vault_pass_file, file_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            return True
        else:
            print(f"ERROR encrypting {file_path}: {result.stderr}")
            return False
    except Exception as e:
        print(f"ERROR: ansible-vault not found or error: {e}")
        return False


def decrypt_file(file_path: str, vault_pass_file: str = VAULT_PASS_FILE) -> bool:
    """Decrypt a file using ansible-vault."""
    if not os.path.isfile(vault_pass_file):
        print(f"ERROR: Vault password file not found: {vault_pass_file}")
        return False
    
    try:
        result = subprocess.run(
            ['ansible-vault', 'decrypt', '--vault-password-file', vault_pass_file, file_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            return True
        else:
            print(f"ERROR decrypting {file_path}: {result.stderr}")
            return False
    except Exception as e:
        print(f"ERROR: ansible-vault not found or error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='HAL Security Audit & Encryption Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Audit all git repos for secrets
  %(prog)s audit

  # Scan specific repo
  %(prog)s audit /path/to/repo

  # Encrypt a file
  %(prog)s encrypt /path/to/file

  # Decrypt a file
  %(prog)s decrypt /path/to/file

  # Generate report
  %(prog)s report > security_audit.json
        '''
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # Audit subcommand
    audit_parser = subparsers.add_parser('audit', help='Audit git repos for secrets')
    audit_parser.add_argument('repo', nargs='?', help='Specific repo to scan (default: home directory)')
    audit_parser.add_argument('--depth', type=int, default=3, help='Max search depth')
    
    # Encrypt subcommand
    enc_parser = subparsers.add_parser('encrypt', help='Encrypt a file with vault')
    enc_parser.add_argument('file', help='File to encrypt')
    
    # Decrypt subcommand
    dec_parser = subparsers.add_parser('decrypt', help='Decrypt a file with vault')
    dec_parser.add_argument('file', help='File to decrypt')
    
    # Report subcommand
    rep_parser = subparsers.add_parser('report', help='Generate security report')
    rep_parser.add_argument('--repos', type=int, default=10, help='Max repos to scan')
    
    # Check subcommand
    subparsers.add_parser('check', help='Check vault setup')
    
    args = parser.parse_args()
    
    if args.command == 'check':
        if check_vault_password():
            print(f"✓ Vault password file ready: {VAULT_PASS_FILE}")
            return 0
        else:
            return 1
    
    elif args.command == 'audit':
        if not check_vault_password():
            return 1
        
        start_path = args.repo or os.path.expanduser('~')
        repos = find_git_repos(start_path, max_depth=args.depth)
        
        print(f"Found {len(repos)} git repository(ies)")
        print("=" * 70)
        
        total_repos_with_issues = 0
        total_issues = 0
        
        for repo in repos[:20]:  # Limit to first 20 for performance
            results = scan_repo(repo)
            if results['total_issues'] > 0:
                total_repos_with_issues += 1
                total_issues += results['total_issues']
                print(f"\n[{os.path.basename(repo)}]")
                print(f"  Issues found: {results['total_issues']}")
                for file_info in results['files_with_secrets'][:5]:
                    print(f"    - {file_info['file']}: {file_info['issues']}")
                if len(results['files_with_secrets']) > 5:
                    print(f"    ... and {len(results['files_with_secrets']) - 5} more")
        
        print("\n" + "=" * 70)
        print(f"Summary: {total_issues} issue(s) in {total_repos_with_issues} repo(s)")
        return 0
    
    elif args.command == 'encrypt':
        if not check_vault_password():
            return 1
        
        if not os.path.isfile(args.file):
            print(f"ERROR: File not found: {args.file}")
            return 1
        
        if encrypt_file(args.file):
            print(f"✓ Encrypted: {args.file}")
            return 0
        else:
            return 1
    
    elif args.command == 'decrypt':
        if not check_vault_password():
            return 1
        
        if not os.path.isfile(args.file):
            print(f"ERROR: File not found: {args.file}")
            return 1
        
        if decrypt_file(args.file):
            print(f"✓ Decrypted: {args.file}")
            return 0
        else:
            return 1
    
    elif args.command == 'report':
        if not check_vault_password():
            return 1
        
        repos = find_git_repos(os.path.expanduser('~'), max_depth=3)
        report = {
            'vault_status': 'ready',
            'repos_scanned': min(len(repos), args.repos),
            'repos': []
        }
        
        for repo in repos[:args.repos]:
            results = scan_repo(repo)
            if results['total_issues'] > 0:
                report['repos'].append(results)
        
        print(json.dumps(report, indent=2))
        return 0
    
    else:
        parser.print_help()
        return 0


if __name__ == '__main__':
    sys.exit(main())
