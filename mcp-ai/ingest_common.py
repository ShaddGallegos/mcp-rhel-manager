#!/usr/bin/env python3
"""Common helpers for supplemental ingestion scripts."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Iterable, Any


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


def write_jsonl(entries: Iterable[Any], outdir: str, prefix: str) -> str:
    """Write entries as JSONL and return output path."""
    outdir = os.path.expanduser(outdir)
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outpath = os.path.join(outdir, f"{prefix}-{ts}.jsonl")
    with open(outpath, "w", encoding="utf-8") as out:
        for entry in entries:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return outpath
