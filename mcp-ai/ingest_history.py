#!/usr/bin/env python3
"""Ingest Copilot chat transcripts into the local supplemental training data.

Creates a JSONL file under ~/.mcp-ai/training with one entry per message
from the transcript. Performs light redaction of obvious secrets.

Usage: ingest_history.py [--transcript PATH] [--outdir DIR]
"""
from __future__ import annotations
import os
import sys
import json
import glob
import re
import argparse
from datetime import datetime, timezone


def redact(text: str) -> str:
    if not isinstance(text, str):
        return text
    # redact private key blocks
    text = re.sub(r"-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]", text, flags=re.S)
    # redact inline secrets like password/token/api_key
    text = re.sub(r"(?i)(password|passwd|pwd|token|secret|api[_-]?key|apikey)\s*[:=]\s*\S+", r"\1: [REDACTED]", text)
    # redact Bearer tokens
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9\-_.]+", "Bearer [REDACTED]", text)
    return text


def find_latest_transcript() -> str | None:
    base = os.path.expanduser("~/.config/Code/User/workspaceStorage")
    pattern = os.path.join(base, "*", "GitHub.copilot-chat", "transcripts", "*.jsonl")
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def parse_transcript(path: str) -> list:
    messages = []
    session_id = None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            typ = obj.get("type", "")
            if typ == "session.start":
                session_id = obj.get("data", {}).get("sessionId")
            if typ.endswith(".message"):
                role = typ.split(".")[0]
                data = obj.get("data", {})
                # common locations for text
                content = data.get("content") or (data.get("message") and data.get("message").get("content")) or data.get("text") or ""
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                content = redact(content)
                timestamp = obj.get("timestamp") or data.get("timestamp") or datetime.now(timezone.utc).isoformat()
                messages.append({
                    "source": "copilot-chat-transcript",
                    "session_id": session_id,
                    "role": role,
                    "timestamp": timestamp,
                    "content": content,
                    "meta": {"raw_type": typ},
                })
    return messages


def write_jsonl(entries: list, outdir: str, prefix: str = "supplemental-history") -> str:
    os.makedirs(os.path.expanduser(outdir), exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{prefix}-{ts}.jsonl"
    outpath = os.path.join(os.path.expanduser(outdir), fname)
    with open(outpath, "w", encoding="utf-8") as out:
        for e in entries:
            out.write(json.dumps(e, ensure_ascii=False) + "\n")
    return outpath


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", help="Path to transcript.jsonl (auto-detect if omitted)")
    parser.add_argument("--outdir", default="~/.mcp-ai/training", help="Output training directory")
    parser.add_argument("--prefix", default="supplemental-history", help="Output filename prefix")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    transcript = args.transcript or find_latest_transcript()
    if not transcript:
        print("No copilot transcript found under workspaceStorage/*/GitHub.copilot-chat/transcripts/", file=sys.stderr)
        return 2

    print("Using transcript:", transcript)
    entries = parse_transcript(transcript)
    if not entries:
        print("No messages parsed from transcript.")
        return 0
    if args.dry_run:
        print(f"Dry run: parsed {len(entries)} messages; would write to {args.outdir}")
        return 0

    outpath = write_jsonl(entries, args.outdir, args.prefix)
    print(f"Wrote {len(entries)} entries to: {outpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
