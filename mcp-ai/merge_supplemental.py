#!/usr/bin/env python3
"""Merge supplemental JSONL files into a single combined JSONL with deduplication.

Looks for files matching `supplemental-*.jsonl` under ~/.mcp-ai/training by default.
Writes `supplemental-combined-<TS>.jsonl` and prints the output path.
"""
from __future__ import annotations
import os
import sys
import glob
import json
import hashlib
import argparse
from datetime import datetime, timezone


def find_files(indir: str, pattern: str) -> list[str]:
    indir = os.path.expanduser(indir)
    pat = os.path.join(indir, pattern)
    files = sorted(glob.glob(pat))
    return files


def dedup_and_merge(files: list[str], outdir: str, prefix: str) -> str:
    seen = set()
    outdir = os.path.expanduser(outdir)
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outname = f"{prefix}-{ts}.jsonl"
    outpath = os.path.join(outdir, outname)
    written = 0
    with open(outpath, "w", encoding="utf-8") as out:
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            # skip malformed lines
                            continue
                        # preferred dedup key: `sha256` field if present
                        if isinstance(obj, dict) and obj.get("sha256"):
                            key = "sha256:" + str(obj.get("sha256"))
                        else:
                            key = hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                        if key in seen:
                            continue
                        seen.add(key)
                        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
                        written += 1
            except Exception:
                # skip unreadable files
                continue
    return outpath, written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--indir", default="~/.mcp-ai/training", help="Input directory to scan for supplemental files")
    parser.add_argument("--pattern", default="supplemental-*.jsonl", help="Glob pattern to match supplemental files")
    parser.add_argument("--outdir", default="~/.mcp-ai/training", help="Output directory for combined file")
    parser.add_argument("--prefix", default="supplemental-combined", help="Output filename prefix")
    args = parser.parse_args(argv)

    files = find_files(args.indir, args.pattern)
    if not files:
        print(f"No files found in {args.indir} matching {args.pattern}", file=sys.stderr)
        return 2
    print(f"Found {len(files)} files to merge")
    outpath, written = dedup_and_merge(files, args.outdir, args.prefix)
    print(f"Wrote {written} unique entries to: {outpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
