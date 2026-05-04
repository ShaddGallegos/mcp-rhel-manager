#!/usr/bin/env python3
"""Ingest repository-generated artifacts into supplemental training data.

Targets fix plans, patch files, and generated scripts. Writes a JSONL
file to the training dir (default: ~/.mcp-ai/training).

Usage: ingest_artifacts.py [--patterns PATTERN ...] [--outdir DIR]
"""
from __future__ import annotations
import os
import sys
import json
import glob
import argparse
import hashlib
import re
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def redact(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = re.sub(r"-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]", text, flags=re.S)
    text = re.sub(r"(?i)(password|passwd|pwd|token|secret|api[_-]?key|apikey)\s*[:=]\s*\S+", r"\1: [REDACTED]", text)
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9\-_.]+", "Bearer [REDACTED]", text)
    return text


def collect_files(patterns: list[str]) -> list[str]:
    files = []
    seen = set()
    for p in patterns:
        p_exp = os.path.expanduser(p)
        matches = glob.glob(p_exp, recursive=True)
        for m in matches:
            if os.path.isfile(m) and m not in seen:
                seen.add(m)
                files.append(m)
    files.sort()
    return files


def file_type(path: str) -> str:
    name = os.path.basename(path).lower()
    if "/.mcp-ai/fixes/" in path or name.startswith("plan-") or name.endswith(".json"):
        return "fix_plan"
    if name.endswith(".patch") or name.endswith(".diff"):
        return "patch"
    if name.endswith(".py"):
        return "generated_script"
    return "artifact"


def read_and_prepare(path: str, max_bytes: int = 512 * 1024) -> dict:
    st = os.stat(path)
    size = st.st_size
    truncated = False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        if size > max_bytes:
            content = fh.read(max_bytes)
            truncated = True
        else:
            content = fh.read()
    content = redact(content)
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "path": path,
        "type": file_type(path),
        "size": size,
        "truncated": truncated,
        "sha256": h,
        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "content": content,
    }


def write_jsonl(entries: list[dict], outdir: str, prefix: str = "supplemental-solutions") -> str:
    outdir = os.path.expanduser(outdir)
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{prefix}-{ts}.jsonl"
    outpath = os.path.join(outdir, fname)
    with open(outpath, "w", encoding="utf-8") as out:
        for e in entries:
            out.write(json.dumps(e, ensure_ascii=False) + "\n")
    return outpath


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--patterns", nargs="+", help="Glob patterns to collect files", default=None)
    parser.add_argument("--outdir", default="~/.mcp-ai/training")
    parser.add_argument("--prefix", default="supplemental-solutions")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    default_patterns = [
        "~/.mcp-ai/fixes/*",
        "~/.mcp-ai/fixes/**/*.json",
        os.path.join(_REPO_ROOT, "mcp-ai/*.py"),
        os.path.join(_REPO_ROOT, "scripts", "hal.py"),
        os.path.join(_REPO_ROOT, "*.py"),
        os.path.join(_REPO_ROOT, "*.sh"),
    ]
    patterns = args.patterns or default_patterns
    files = collect_files(patterns)
    if not files:
        print("No files found for patterns:", patterns, file=sys.stderr)
        return 2

    print(f"Found {len(files)} files to ingest")
    entries = []
    for p in files:
        try:
            rec = read_and_prepare(p)
            entries.append({
                "source": "repo-artifact",
                "path": rec.pop("path"),
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                **rec,
            })
        except Exception as e:
            print(f"Failed to read {p}: {e}", file=sys.stderr)

    if args.dry_run:
        print(f"Dry run: would write {len(entries)} entries to {args.outdir}")
        return 0

    outpath = write_jsonl(entries, args.outdir, args.prefix)
    print(f"Wrote {len(entries)} entries to: {outpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
