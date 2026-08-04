"""Helper utilities for running linting, secrets scanning, and Jinja checks.

Designed to be lightweight and best-effort: tools are invoked only if
available on PATH. Results are returned as structured dicts for inclusion
in prompts or audit logs.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

try:
    import jinja2
    from jinja2 import StrictUndefined
except Exception:
    jinja2 = None


def _run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 60) -> Dict:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
        return {"cmd": " ".join(cmd), "rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except Exception as e:
        return {"cmd": " ".join(cmd), "rc": -1, "stdout": "", "stderr": str(e)}


def tool_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_linters(path: Path) -> Dict[str, Dict]:
    """Run a set of linters relevant to the given file or folder.

    Returns a mapping tool->result where result contains rc/stdout/stderr.
    """
    path = Path(path)
    results: Dict[str, Dict] = {}

    # Determine file types
    exts = {p.suffix.lower() for p in [path] if path.is_file()}
    if path.is_dir():
        # scan directory for common extensions
        for p in path.rglob("*"):
            if p.suffix.lower() in (".py",):
                exts.add(".py")
            if p.suffix.lower() in (".sh", ".bash"):
                exts.add(".sh")
            if p.suffix.lower() in (".yml", ".yaml"):
                exts.add(".yml")

    # YAML/Ansible checks
    if ".yml" in exts or ".yaml" in exts:
        if tool_exists("yamllint"):
            results["yamllint"] = _run(["yamllint", "-f", "parsable", str(path)])
        if tool_exists("ansible-lint"):
            results["ansible-lint"] = _run(["ansible-lint", str(path)])
        # ansible-playbook syntax check (best-effort for files)
        if tool_exists("ansible-playbook") and path.is_file():
            results["ansible-syntax-check"] = _run(["ansible-playbook", "--syntax-check", str(path)])

    # Python checks
    if ".py" in exts:
        if tool_exists("ruff"):
            results["ruff"] = _run(["ruff", "check", str(path)])
        elif tool_exists("flake8"):
            results["flake8"] = _run(["flake8", str(path)])
        if tool_exists("black"):
            results["black"] = _run(["black", "--check", "--quiet", str(path)])
        if tool_exists("bandit"):
            results["bandit"] = _run(["bandit", "-r", str(path)])
        if tool_exists("mypy"):
            results["mypy"] = _run(["mypy", str(path)])

    # Shell checks
    if ".sh" in exts or any(p.suffix.lower() in (".sh", ".bash") for p in path.rglob("*")):
        if tool_exists("shellcheck"):
            # run shellcheck on all shell-looking files under path
            files = [str(p) for p in (path.rglob("*.sh") if path.is_dir() else [path])]
            if files:
                results["shellcheck"] = _run(["shellcheck"] + files)
        if tool_exists("shfmt"):
            results["shfmt"] = _run(["shfmt", "-l", str(path)])

    return results


SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z-_]{35}"),
    re.compile(r"aws_secret_access_key\s*[:=]\s*[A-Za-z0-9/+=]{40}"),
    re.compile(r"ssh-rsa[ \t]+[A-Za-z0-9+/=]{100,}"),
    re.compile(r"password\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"api[_-]?key\s*[:=]\s*\S+", re.IGNORECASE),
]


def scan_secrets(path: Path, max_files: int = 1000) -> Dict[str, List[str]]:
    """Scan files under path for likely secrets. Returns mapping file->matches.

    This is a heuristic scanner — results may contain false positives.
    """
    findings: Dict[str, List[str]] = {}

    path = Path(path)
    files = []
    if path.is_file():
        files = [path]
    else:
        for p in path.rglob("*"):
            if p.is_file():
                files.append(p)
            if len(files) >= max_files:
                break

    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        matches = []
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                snippet = m.group(0)
                # mask long tokens
                if len(snippet) > 40:
                    snippet = snippet[:6] + "..." + snippet[-6:]
                matches.append(snippet)
        if matches:
            findings[str(f)] = matches

    return findings


def jinja_check(path: Path, context: Optional[Dict] = None) -> Dict[str, str]:
    """Attempt to render Jinja2 templates found under path using the provided context.

    Returns mapping template_file->error_message (empty string for success).
    """
    results: Dict[str, str] = {}
    if jinja2 is None:
        return {"_error": "jinja2 not installed"}

    env = jinja2.Environment(undefined=StrictUndefined)

    if context is None:
        context = {}

    path = Path(path)
    candidates = []
    if path.is_file():
        candidates = [path]
    else:
        for p in path.rglob("*"):
            if p.suffix.lower() in (".j2", ".jinja2") or (p.suffix.lower() in (".yml", ".yaml", ".tpl") and "{{" in p.read_text(errors="ignore")):
                candidates.append(p)

    for t in candidates:
        try:
            tpl = env.from_string(t.read_text())
            tpl.render(**context)
            results[str(t)] = ""
        except Exception as e:
            results[str(t)] = str(e)

    return results
