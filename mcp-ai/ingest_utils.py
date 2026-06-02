#!/usr/bin/env python3
"""Shared ingestion utilities for auto-ingest and tests.

Functions:
 - get_file_hash(path)
 - load_import_tracker(path)
 - save_import_tracker(path, tracker)
 - should_import_file(filepath, tracker, base_dir)
 - ingest_business_intel(jsonl_path)
 - ingest_documents(doc_path)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def get_file_hash(filepath: Path) -> str:
    sha256 = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception:
        return ""


def load_import_tracker(tracker_path: Path) -> dict:
    if not tracker_path.exists():
        return {"imported": {}, "last_run": None}
    try:
        with open(tracker_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"imported": {}, "last_run": None}


def save_import_tracker(tracker_path: Path, tracker: dict) -> None:
    try:
        tracker_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tracker_path, "w", encoding="utf-8") as fh:
            json.dump(tracker, fh, indent=2)
    except Exception:
        pass


def should_import_file(filepath: Path, tracker: dict, base_dir: Path | None = None, verbose: bool = False) -> bool:
    try:
        stat = filepath.stat()
        current_hash = get_file_hash(filepath)
        current_mtime = stat.st_mtime

        if base_dir:
            try:
                rel_path = str(filepath.relative_to(base_dir))
            except Exception:
                rel_path = str(filepath)
        else:
            rel_path = str(filepath)

        if rel_path not in tracker.get("imported", {}):
            if verbose:
                print(f"  [NEW] {filepath.name}")
            return True

        prev = tracker["imported"].get(rel_path, {})
        if prev.get("hash") != current_hash or prev.get("mtime", 0) < current_mtime:
            if verbose:
                print(f"  [UPDATED] {filepath.name}")
            return True

        if verbose:
            print(f"  [SKIP] {filepath.name} (unchanged)")
        return False
    except Exception as e:
        if verbose:
            print(f"  [ERROR checking] {filepath.name}: {e}")
        return False


def ingest_business_intel(jsonl_path: Path, verbose: bool = False) -> Tuple[int, int, int]:
    script = Path(__file__).parent / "ingest_business_intel.py"
    if not script.exists():
        return 0, 0, 1
    cmd = [sys.executable, str(script), str(jsonl_path)]
    try:
        if verbose:
            print(f"  Running: {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            if verbose:
                print(proc.stderr[:200])
            return 0, 0, 1
        out = proc.stdout + proc.stderr
        ok = out.count("Imported 1 record")
        return ok, 0, 0
    except Exception:
        return 0, 0, 1


def ingest_documents(doc_path: Path, verbose: bool = False) -> Tuple[int, int, int]:
    script = Path(__file__).parent / "ingest_documents.py"
    if not script.exists():
        return 0, 0, 1
    cmd = [sys.executable, str(script), str(doc_path)]
    try:
        if verbose:
            print(f"  Running: {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            if verbose:
                print(proc.stderr[:200])
            return 0, 0, 1
        out = proc.stdout + proc.stderr
        import re
        m = re.search(r"(\d+) ok.*?(\d+) skip.*?(\d+) error", out)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
        return 1, 0, 0
    except Exception:
        return 0, 0, 1
