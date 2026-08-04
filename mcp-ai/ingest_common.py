#!/usr/bin/env python3
"""Common helpers for supplemental ingestion scripts."""

from __future__ import annotations

import json
import os
import re
import errno
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Iterable, Any, List, Optional


def redact_text(text: str) -> str:
    """Redact obvious secrets from plain text."""
    if not isinstance(text, str):
        return text
    text = re.sub(
        r"-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----",
        "[REDACTED_PRIVATE_KEY]",
        text,
        flags=re.S,
    )
    text = re.sub(
        r"(?i)(password|passwd|pwd|token|secret|api[_-]?key|apikey)\s*[:=]\s*\S+",
        r"\1: [REDACTED]",
        text,
    )
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9\-_.]+", "Bearer [REDACTED]", text)
    return text


# -- GStack-inspired injection patterns and JSONL helpers -----------------
# These provide lightweight, POSIX-friendly append/read and simple injection
# detection useful for safe append-only stores.

# Injection-like instruction patterns (ported to Python regexes).
INJECTION_PATTERNS: List[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+(instructions|context|rules)", re.I),
    re.compile(r"you\s+are\s+now\s+", re.I),
    re.compile(r"always\s+output\s+no\s+findings", re.I),
    re.compile(r"skip\s+(all\s+)?(security|review|checks)", re.I),
    re.compile(r"override[:\s]", re.I),
    re.compile(r"\\bsystem\\s*:", re.I),
    re.compile(r"\\bassistant\\s*:", re.I),
    re.compile(r"\\buser\\s*:", re.I),
    re.compile(r"\\bhuman\\s*:", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|above|prior)", re.I),
    re.compile(r"from\s+now\s+on\\b", re.I),
    re.compile(r"do\s+not\s+(report|flag|mention)", re.I),
    re.compile(r"approve\s+(all|every|this)", re.I),
]


def has_injection(text: str) -> bool:
    """Return True if `text` contains an instruction-like injection pattern."""
    if not isinstance(text, str):
        return False
    for p in INJECTION_PATTERNS:
        if p.search(text):
            return True
    return False


def first_injection_match(text: str) -> Optional[re.Pattern]:
    """Return the first matching pattern or None."""
    if not isinstance(text, str):
        return None
    for p in INJECTION_PATTERNS:
        if p.search(text):
            return p
    return None


def append_jsonl(path: str, obj: Any) -> None:
    """Atomically append a single JSON object as one line to `path`.

    Raises ValueError if the serialized JSON contains a newline.
    """
    line = json.dumps(obj, ensure_ascii=False)
    if "\n" in line:
        raise ValueError("append_jsonl: record serialized to multiple lines (embedded newline)")
    b = (line + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    mode = 0o644
    # Use low-level os.open + os.write with O_APPEND for atomic append semantics
    fd = os.open(path, flags, mode)
    try:
        os.write(fd, b)
    finally:
        os.close(fd)


def read_jsonl(path: str) -> List[Any]:
    """Tolerant reader for JSONL: skip malformed lines, return list of objects."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return []
    out: List[Any] = []
    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            continue
        try:
            out.append(json.loads(trimmed))
        except Exception:
            # Skip malformed lines (partial tail / corruption)
            continue
    return out


# -- Optional: wrapper to call gstack's `gstack-redact` CLI when available.
def _gstack_redact_executable() -> Optional[str]:
    """Return the path to a bundled `gstack-redact` CLI if present and runnable.

    This looks for `external/gstack/bin/gstack-redact` relative to the
    repository root (one level above this module). The function also ensures
    the `bun` runtime is present on PATH since the script uses a `#!/usr/bin/env bun` shebang.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidate = os.path.join(repo_root, "external", "gstack", "bin", "gstack-redact")
    if not os.path.exists(candidate) or not os.access(candidate, os.X_OK):
        return None
    if shutil.which("bun") is None:
        return None
    return candidate


def call_gstack_redact_json(text: str, repo_visibility: str = "unknown") -> Optional[dict]:
    """Call the gstack-redact CLI and return parsed JSON result, or None if unavailable."""
    exe = _gstack_redact_executable()
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "--repo-visibility", repo_visibility, "--json"], input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if proc.returncode is None:
            return None
        out = proc.stdout.decode("utf-8").strip()
        if not out:
            return None
        return json.loads(out)
    except Exception:
        return None


def call_gstack_auto_redact(text: str, repo_visibility: str = "unknown") -> Optional[str]:
    """Use the bundled `gstack-redact` to auto-redact auto-redactable findings.

    Returns the redacted body as a string, or None if the CLI is unavailable
    or no auto-redactable findings were present.
    """
    scan = call_gstack_redact_json(text, repo_visibility=repo_visibility)
    if not scan or "findings" not in scan:
        return None
    ids = sorted({f.get("id") for f in scan.get("findings", []) if f.get("autoRedactable")})
    if not ids:
        return None
    exe = _gstack_redact_executable()
    if not exe:
        return None
    try:
        arg = ",".join(ids)
        proc = subprocess.run([exe, "--repo-visibility", repo_visibility, "--auto-redact", arg], input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8")
    except Exception:
        return None


def write_jsonl(entries: Iterable[Any], outdir: str, prefix: str) -> str:
    """Write entries as JSONL and return output path."""
    outdir = os.path.expanduser(outdir)
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outpath = os.path.join(outdir, f"{prefix}-{ts}.jsonl")
    # Use append_jsonl per-entry to get safe single-line atomic appends.
    for entry in entries:
        append_jsonl(outpath, entry)
    return outpath
