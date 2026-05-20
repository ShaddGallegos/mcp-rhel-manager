#!/usr/bin/env python3
"""Git helper for HAL: interactive repo operations and per-project env management.

Features:
- Scan for local git repositories (folders containing `.git`).
- Interactive menu to `clone`, `commit` (with preflight secret scan), `push`,
  create branches, list/create/accept PRs via GitHub API.
- Manage per-project env files under `~/.ansible/conf/<project>-env.yml`.

This is intended to be invoked from `mcp-ai/cli.py git` or from HAL workflows.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests
except Exception:
    requests = None

from dynamic_menu import choose
import subprocess
from pathlib import Path
from typing import Optional

try:
    import git_watcher
except Exception:
    git_watcher = None


ANSIBLE_CONF_DIR = Path(os.path.expanduser('~')) / '.ansible' / 'conf'


def ensure_conf_dir() -> Path:
    ANSIBLE_CONF_DIR.mkdir(parents=True, exist_ok=True)
    return ANSIBLE_CONF_DIR


def list_env_files() -> List[Path]:
    d = ensure_conf_dir()
    return sorted([p for p in d.iterdir() if p.is_file() and p.suffix in ('.yml', '.yaml')])


def load_env(path: Path) -> Dict[str, str]:
    # Lightweight YAML loader for simple key: value env files.
    data: Dict[str, str] = {}
    try:
        import yaml

        with path.open('r', encoding='utf-8') as fh:
            v = yaml.safe_load(fh) or {}
            if isinstance(v, dict):
                for k, val in v.items():
                    data[str(k)] = str(val)
            return data
    except Exception:
        # Fallback simple parser
        try:
            with path.open('r', encoding='utf-8') as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln or ln.startswith('#'):
                        continue
                    if ':' in ln:
                        k, v = ln.split(':', 1)
                        data[k.strip()] = v.strip().strip('"\'')
        except Exception:
            pass
    return data


def save_env(path: Path, data: Dict[str, str]) -> None:
    try:
        import yaml

        with path.open('w', encoding='utf-8') as fh:
            yaml.safe_dump(data, fh, default_flow_style=False)
        return
    except Exception:
        # Simple writer fallback
        with path.open('w', encoding='utf-8') as fh:
            for k, v in data.items():
                fh.write(f"{k}: '{v}'\n")


def run_cmd(cmd: List[str], cwd: Optional[Path] = None, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=capture, text=True)


def scan_repos(roots: Optional[List[Path]] = None) -> List[Path]:
    if roots is None:
        roots = [Path.cwd(), Path.home() / 'GIT']
    found: List[Path] = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for g in root.rglob('.git'):
            repo = g.parent
            key = str(repo.resolve())
            if key not in seen:
                seen.add(key)
                found.append(repo)
    return sorted(found)


_SECRET_PATTERNS = [
    re.compile(r'ghp_[A-Za-z0-9_\-]+'),
    re.compile(r'gho_[A-Za-z0-9_\-]+'),
    re.compile(r'ghs_[A-Za-z0-9_\-]+'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    re.compile(r"password\s*[:=]\s*['\"][^'\"]+['\"]", re.I),
    re.compile(r"token\s*[:=]\s*['\"][^'\"]+['\"]", re.I),
]


def find_secrets_in_text(text: str) -> List[str]:
    found = []
    for p in _SECRET_PATTERNS:
        for m in p.findall(text):
            if m and m not in found:
                found.append(m)
    return found


def preflight_scan_and_extract(repo_path: Path, project_env: Path, staged_files: List[Path]) -> bool:
    """Scan staged files for secrets. If found, optionally extract to env file and replace with placeholders.

    Returns True to proceed, False to abort.
    """
    matches: Dict[Path, List[str]] = {}
    for f in staged_files:
        try:
            txt = f.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        found = find_secrets_in_text(txt)
        if found:
            matches[f] = found

    if not matches:
        return True

    print('Potential secrets detected in staged files:')
    for f, items in matches.items():
        print(f'  - {f}:')
        for it in items:
            print(f'      {it}')

    ans = input('Move detected secrets to env file and redact in files? (y/N) ').strip().lower()
    if ans != 'y':
        print('Aborting commit (secrets present).')
        return False

    env_data = load_env(project_env) if project_env.exists() else {}
    changed_files: List[Path] = []
    for f, items in matches.items():
        txt = f.read_text(encoding='utf-8', errors='ignore')
        for i, secret in enumerate(items, start=1):
            # propose a var name
            base_name = f'{f.stem}_secret_{i}'
            var_name = input(f'Enter env var name for secret "{secret}" (default {base_name}): ').strip() or base_name
            env_data[var_name] = secret
            placeholder = f'REDACTED_ENV[{var_name}]'
            txt = txt.replace(secret, placeholder)
        f.write_text(txt, encoding='utf-8')
        changed_files.append(f)

    save_env(project_env, env_data)
    # stage changed files
    run_cmd(['git', 'add', '--all'], cwd=repo_path)
    print(f'Wrote env to {project_env} and redacted {len(changed_files)} files.')
    return True


def repo_remote_owner_repo(repo_path: Path) -> Optional[Tuple[str, str]]:
    r = run_cmd(['git', 'remote', 'get-url', 'origin'], cwd=repo_path, capture=True)
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    # handle git@github.com:owner/repo.git and https://github.com/owner/repo.git
    if url.startswith('git@'):
        try:
            _, tail = url.split(':', 1)
            owner, repo = tail.split('/', 1)
        except Exception:
            return None
    else:
        # strip protocol
        try:
            parts = url.split('/')
            owner = parts[-2]
            repo = parts[-1]
        except Exception:
            return None
    repo = repo.rstrip('.git')
    return owner, repo


def github_list_user_repos(username: str, token: Optional[str] = None) -> List[Dict]:
    if requests is None:
        raise RuntimeError('requests library required for GitHub operations')
    url = f'https://api.github.com/users/{username}/repos?per_page=100'
    headers = {}
    if token:
        headers['Authorization'] = f'token {token}'
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()


def github_list_repo_prs(owner: str, repo: str, token: Optional[str] = None) -> List[Dict]:
    if requests is None:
        raise RuntimeError('requests library required for GitHub operations')
    url = f'https://api.github.com/repos/{owner}/{repo}/pulls'
    headers = {}
    if token:
        headers['Authorization'] = f'token {token}'
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()


def github_create_pr(owner: str, repo: str, title: str, head: str, base: str = 'main', body: str = '', token: Optional[str] = None) -> Dict:
    if requests is None:
        raise RuntimeError('requests library required for GitHub operations')
    url = f'https://api.github.com/repos/{owner}/{repo}/pulls'
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if token:
        headers['Authorization'] = f'token {token}'
    payload = {'title': title, 'head': head, 'base': base, 'body': body}
    r = requests.post(url, json=payload, headers=headers)
    r.raise_for_status()
    return r.json()


def github_merge_pr(owner: str, repo: str, pr_number: int, token: Optional[str] = None) -> Dict:
    if requests is None:
        raise RuntimeError('requests library required for GitHub operations')
    url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge'
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if token:
        headers['Authorization'] = f'token {token}'
    r = requests.put(url, headers=headers)
    r.raise_for_status()
    return r.json()


def interactive_menu():
    roots = [Path.cwd(), Path.home() / 'GIT']
    repos = scan_repos(roots)
    options = [str(p) for p in repos] + ['Scan different path...', 'Clone a repo (URL)', 'Clone user repos (GitHub)', 'Exit']
    sel = choose(options, prompt='Select repository or action', multi=False)
    if not sel:
        return 0
    idx = sel[0]
    if idx < len(repos):
        repo = repos[idx]
        return repo_submenu(repo)
    action = options[idx]
    if action == 'Scan different path...':
        p = input('Enter path to scan: ').strip()
        if not p:
            return 0
        new_repos = scan_repos([Path(p)])
        for r in new_repos:
            print(r)
        return 0
    if action == 'Clone a repo (URL)':
        url = input('Repo URL (https://... or git@...): ').strip()
        if not url:
            return 0
        target = input('Target directory (optional): ').strip() or None
        branch = input('Checkout/create branch (leave blank for default): ').strip() or None
        clone_repo(url, target_dir=target, branch=branch)
        return 0
    if action == 'Clone user repos (GitHub)':
        uname = input('GitHub username: ').strip()
        if not uname:
            return 0
        token = input('GitHub token (leave blank for public only): ').strip() or None
        repos = github_list_user_repos(uname, token)
        opts = [r.get('full_name') for r in repos]
        sel = choose(opts, prompt='Select repos to clone (single choose)')
        if not sel:
            return 0
        chosen = repos[sel[0]]
        clone_repo(chosen.get('clone_url'), None, None)
        return 0
    return 0


def repo_submenu(repo_path: Path) -> int:
    opts = [
        'Status / show remotes',
        'Git add --all; commit & push',
        'Create / checkout branch',
        'List PRs',
        'Create PR',
        'Accept (merge) PR',
        'Open shell here',
        'Delete repo (danger)',
        'Back',
    ]
    sel = choose(opts, prompt=f'Actions for {repo_path}', multi=False)
    if not sel:
        return 0
    which = sel[0]
    if which == 0:
        r = run_cmd(['git', 'status'], cwd=repo_path, capture=True)
        print(r.stdout)
        r2 = run_cmd(['git', 'remote', '-v'], cwd=repo_path, capture=True)
        print(r2.stdout)
        return 0
    if which == 1:
        msg = input('Commit message: ').strip()
        if not msg:
            print('Aborting: empty commit message')
            return 0
        # stage
        run_cmd(['git', 'add', '--all'], cwd=repo_path)
        # get staged files
        r = run_cmd(['git', 'diff', '--cached', '--name-only'], cwd=repo_path, capture=True)
        files = [repo_path / Path(s.strip()) for s in r.stdout.splitlines() if s.strip()]
        env_name = repo_path.name + '-env.yml'
        env_path = ensure_conf_dir() / env_name
        ok = preflight_scan_and_extract(repo_path, env_path, files)
        if not ok:
            return 0
        # commit
        rc = run_cmd(['git', 'commit', '-m', msg], cwd=repo_path, capture=True)
        print(rc.stdout or rc.stderr)
        # push
        # determine branch
        rbranch = run_cmd(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo_path, capture=True)
        branch = rbranch.stdout.strip() if rbranch.returncode == 0 else 'main'
        push = run_cmd(['git', 'push', '-u', 'origin', branch], cwd=repo_path, capture=True)
        print(push.stdout or push.stderr)
        return 0
    if which == 2:
        name = input('Branch name to create/checkout: ').strip()
        if not name:
            return 0
        run_cmd(['git', 'checkout', '-b', name], cwd=repo_path)
        print(f'Checked out {name}')
        return 0
    if which == 3:
        owner_repo = repo_remote_owner_repo(repo_path)
        if not owner_repo:
            print('No origin remote found or unable to parse owner/repo')
            return 0
        owner, repo = owner_repo
        token = input('GitHub token (leave blank to use env file): ').strip() or None
        if not token:
            env = load_env(ensure_conf_dir() / (repo_path.name + '-env.yml'))
            token = env.get('github_token')
        prs = github_list_repo_prs(owner, repo, token)
        for p in prs:
            print(f"#{p['number']} {p['title']} by {p['user']['login']}")
        return 0
    if which == 4:
        owner_repo = repo_remote_owner_repo(repo_path)
        if not owner_repo:
            print('No origin remote found')
            return 0
        owner, repo = owner_repo
        head = input('Head branch (your branch name): ').strip()
        base = input('Base branch (default main): ').strip() or 'main'
        title = input('PR title: ').strip() or f'PR: {head} -> {base}'
        body = input('PR body (optional): ').strip()
        env = load_env(ensure_conf_dir() / (repo_path.name + '-env.yml'))
        token = env.get('github_token')
        if not token:
            token = input('GitHub token: ').strip() or None
        pr = github_create_pr(owner, repo, title=title, head=head, base=base, body=body, token=token)
        print(f"Created PR: {pr.get('html_url')}")
        return 0
    if which == 5:
        owner_repo = repo_remote_owner_repo(repo_path)
        if not owner_repo:
            print('No origin remote found')
            return 0
        owner, repo = owner_repo
        num = input('PR number to accept: ').strip()
        try:
            n = int(num)
        except Exception:
            print('Invalid number')
            return 0
        env = load_env(ensure_conf_dir() / (repo_path.name + '-env.yml'))
        token = env.get('github_token')
        if not token:
            token = input('GitHub token: ').strip() or None
        res = github_merge_pr(owner, repo, n, token)
        print('Merge result:', res)
        return 0
    if which == 6:
        # open shell
        subprocess.run([os.environ.get('SHELL', '/bin/bash')], cwd=str(repo_path))
        return 0
    if which == 7:
        confirm = input(f'Confirm delete {repo_path}? This is destructive (y/N): ').strip().lower()
        if confirm == 'y':
            import shutil

            shutil.rmtree(repo_path)
            print(f'Deleted {repo_path}')
        return 0
    return 0


def clone_repo(url: str, target_dir: Optional[str] = None, branch: Optional[str] = None) -> Optional[Path]:
    if not url:
        return None
    if target_dir:
        tgt = Path(target_dir).expanduser()
    else:
        name = Path(url.rstrip('/')).stem
        if name.endswith('.git'):
            name = name[:-4]
        tgt = Path.cwd() / name
    print(f'Cloning {url} -> {tgt}')
    rc = run_cmd(['git', 'clone', url, str(tgt)])
    if rc.returncode != 0:
        print('git clone failed')
        return None
    if branch:
        run_cmd(['git', 'checkout', branch], cwd=tgt)
    # create project env template
    env_path = ensure_conf_dir() / (tgt.name + '-env.yml')
    if not env_path.exists():
        save_env(env_path, {'github_token': ''})
        print(f'Created env template {env_path}')
    return tgt


def main():
    p = argparse.ArgumentParser(prog='git_manager')
    sp = p.add_subparsers(dest='cmd')
    sp.add_parser('scan', help='Scan for local git repositories')
    sp.add_parser('menu', help='Interactive repository menu')
    c = sp.add_parser('clone', help='Clone a single repo')
    c.add_argument('url', nargs='?', help='Repository URL')
    c.add_argument('--target', help='Target directory')
    c.add_argument('--branch', help='Branch to checkout')
    sp.add_parser('list-envs', help='List known project env files')
    w = sp.add_parser('watch', help='Watch filesystem for .git and .gitignore changes')
    w.add_argument('--roots', help='Comma-separated roots to watch', default='')
    w.add_argument('--interval', type=float, help='Polling interval seconds', default=30.0)

    sp.add_parser('run-github-manager', help='Run bundled .GitHubRepoManager.sh if present')
    sp.add_parser('run-clone-playbook', help='Run CloneGitHubReposByUser.yml playbook (ansible)')
    sp.add_parser('run-fix-ssh', help='Run fix_github_ssh.sh to prepare SSH keys for GitHub')

    args = p.parse_args()
    if args.cmd == 'scan':
        repos = scan_repos()
        for r in repos:
            print(r)
        return 0
    if args.cmd == 'menu' or args.cmd is None:
        return interactive_menu()
    if args.cmd == 'clone':
        url = getattr(args, 'url', None)
        if not url:
            url = input('Repo URL: ').strip()
        clone_repo(url, target_dir=args.target, branch=args.branch)
        return 0
    if args.cmd == 'list-envs':
        for pth in list_env_files():
            print(pth)
        return 0
    if args.cmd == 'watch':
        roots = [r for r in (args.roots.split(',') if args.roots else []) if r]
        roots = roots or None
        if git_watcher is None:
            print('git_watcher module not available')
            return 2

        def _cb(ev: Dict):
            print('Watcher event:')
            for k, v in ev.items():
                print(f'  {k}: {v}')

        gw = git_watcher.start_watch(roots=roots, interval=args.interval, callback=_cb)
        try:
            print('git watcher running (Ctrl-C to stop)')
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print('\nStopping watcher...')
            gw.stop()
            print('stopped')
        return 0
    if args.cmd == 'run-github-manager':
        script = Path.cwd() / '.GitHubRepoManager.sh'
        if not script.exists():
            script = Path(__file__).resolve().parents[1] / '.GitHubRepoManager.sh'
        if not script.exists():
            print('GitHub repo manager script not found (.GitHubRepoManager.sh)')
            return 2
        print('Running', script)
        subprocess.run(['bash', str(script)])
        return 0
    if args.cmd == 'run-clone-playbook':
        play = Path('/home/sgallego/GIT/Clone_All_Repos_for_a_GitHub/CloneGitHubReposByUser.yml')
        if not play.exists():
            print('Clone playbook not found at', play)
            return 2
        # Run via ansible-playbook when available
        if shutil.which('ansible-playbook'):
            subprocess.run(['ansible-playbook', str(play)])
        else:
            print('ansible-playbook not found in PATH. Run the playbook manually or install Ansible.')
        return 0
    if args.cmd == 'run-fix-ssh':
        script = Path('/home/sgallego/GIT/Clone_All_Repos_for_a_GitHub/fix_github_ssh.sh')
        if not script.exists():
            print('fix_github_ssh.sh not found')
            return 2
        subprocess.run(['bash', str(script)])
        return 0
    p.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
