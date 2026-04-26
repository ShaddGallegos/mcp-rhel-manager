#!/usr/bin/env python3
"""Redact potentially sensitive fields from JSONL training files.

This script performs conservative redaction of obvious secrets before using
training data for model fine-tuning or sharing. It is intentionally simple
and heuristic-based — review the output before using it.
"""
from __future__ import annotations
import re
import json
import argparse
from pathlib import Path
from typing import Any


PEM_RE = re.compile(r"-----BEGIN [^-]+PRIVATE KEY-----.*?-----END [^-]+PRIVATE KEY-----", re.S)
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-\._~\+/=]+")
EMAIL_RE = re.compile(r"[\w\.-]+@[\w\.-]+")
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{20,}\b")
BASE64_LIKE_RE = re.compile(r"[A-Za-z0-9+/=]{60,}")
SENSITIVE_KEY_NAME_RE = re.compile(r"(?i)(password|secret|token|apikey|api_key|access_key|private_key|secret_key|credential)")


def redact_text(s: str) -> str:
    if not isinstance(s, str):
        return s
    out = s
    out = PEM_RE.sub("<PRIVATE_KEY_REDACTED>", out)
    out = BEARER_RE.sub("Bearer <REDACTED>", out)
    out = EMAIL_RE.sub("<REDACTED_EMAIL>", out)
    out = IP_RE.sub("<REDACTED_IP>", out)
    out = LONG_HEX_RE.sub("<REDACTED_HEX>", out)
    out = BASE64_LIKE_RE.sub("<REDACTED_BASE64>", out)
    return out


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            if SENSITIVE_KEY_NAME_RE.search(str(k)):
                new[k] = "<REDACTED>"
            else:
                new[k] = redact_obj(v)
        return new
    elif isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    elif isinstance(obj, str):
        return redact_text(obj)
    else:
        return obj


def process(infile: Path, outfile: Path) -> int:
    count = 0
    with infile.open("r", encoding="utf-8") as inf, outfile.open("w", encoding="utf-8") as outf:
        for line in inf:
            line = line.rstrip("\n\r")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                # If not valid JSON, redact as raw text
                red = redact_text(line)
                outf.write(json.dumps({"_raw": red}, ensure_ascii=False) + "\n")
                continue
            obj = redact_obj(obj)
            outf.write(json.dumps(obj, ensure_ascii=False) + "\n")
            count += 1
    return count


def main() -> None:
    p = argparse.ArgumentParser(description="Redact training JSONL files")
    p.add_argument("--infile", required=True, type=Path)
    p.add_argument("--outfile", required=True, type=Path)
    args = p.parse_args()

    args.outfile.parent.mkdir(parents=True, exist_ok=True)
    written = process(args.infile, args.outfile)
    print(f"Wrote {written} redacted entries to: {args.outfile}")


if __name__ == "__main__":
    main()
