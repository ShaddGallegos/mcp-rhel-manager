#!/usr/bin/env python3
"""Automated ingestion of intelligence data into HAL training.

Watches for new business intelligence JSONL files and documents, imports them
into training data, and tracks import history to avoid duplicates.

Usage:
    python3 mcp-ai/auto_ingest_training.py
    python3 mcp-ai/auto_ingest_training.py --verbose
    python3 mcp-ai/auto_ingest_training.py --sync-intel /path/to/Business_Tools

For cron scheduling:
    0 2 * * * cd <REPO_ROOT> && python3 mcp-ai/auto_ingest_training.py >> ~/.mcp-ai/auto_ingest.log 2>&1
"""
from __future__ import annotations

import argparse
import time
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import mcp_config as cfg
    DEFAULT_TRAIN_DIR = Path(cfg.TRAIN_DIR)
    IMPORT_TRACKER = DEFAULT_TRAIN_DIR / ".import_tracker.json"
    BUSINESS_TOOLS_PATH = Path(os.path.expanduser(os.getenv('BUSINESS_TOOLS_PATH', os.path.join(cfg.REPO_ROOT, 'Business_Tools', 'Training_Data'))))
    DOCUMENT_WATCH_PATHS = [
        Path(os.path.expanduser(os.getenv('DOCUMENT_WATCH_PATHS_1', '~/Downloads'))),
        Path(os.path.expanduser(os.getenv('DOCUMENT_WATCH_PATHS_2', '~/Documents'))),
    ]
except Exception:
    DEFAULT_TRAIN_DIR = Path(os.path.expanduser("~/.mcp-ai/training"))
    IMPORT_TRACKER = DEFAULT_TRAIN_DIR / ".import_tracker.json"
    BUSINESS_TOOLS_PATH = Path(os.path.expanduser("~/GIT/Business_Tools/Training_Data"))
    DOCUMENT_WATCH_PATHS = [
        Path(os.path.expanduser("~/Downloads")),
        Path(os.path.expanduser("~/Documents")),
    ]

# Prefer shared ingest utilities when available (keeps local fallback definitions)
try:
    from ingest_utils import (
        get_file_hash as _iu_get_file_hash,
        load_import_tracker as _iu_load_import_tracker,
        save_import_tracker as _iu_save_import_tracker,
        should_import_file as _iu_should_import_file,
        ingest_business_intel as _iu_ingest_business_intel,
        ingest_documents as _iu_ingest_documents,
        utc_ts as _iu_utc_ts,
    )

    def load_import_tracker():
        return _iu_load_import_tracker(IMPORT_TRACKER)

    def save_import_tracker(tracker: dict) -> None:
        return _iu_save_import_tracker(IMPORT_TRACKER, tracker)

    def get_file_hash(filepath: Path) -> str:
        return _iu_get_file_hash(filepath)

    def should_import_file(filepath: Path, tracker: dict, base_dir: Path | None = None, verbose: bool = False) -> bool:
        # Keep original signature but delegate; pass base_dir through for correct relpath calculation
        return _iu_should_import_file(filepath, tracker, base_dir, verbose)

    def ingest_business_intel(jsonl_path: Path, verbose: bool = False):
        return _iu_ingest_business_intel(jsonl_path, verbose)

    def ingest_documents(doc_path: Path, verbose: bool = False):
        return _iu_ingest_documents(doc_path, verbose)

    def utc_ts() -> str:
        return _iu_utc_ts()
except Exception:
    # if ingest_utils not available, continue using local implementations below
    pass


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def get_file_hash(filepath: Path) -> str:
    """Compute SHA256 hash of file content."""
    sha256 = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception:
        return ""


def load_import_tracker() -> dict:
    """Load history of imported files."""
    if not IMPORT_TRACKER.exists():
        return {"imported": {}, "last_run": None}
    try:
        with open(IMPORT_TRACKER, "r") as f:
            return json.load(f)
    except Exception:
        return {"imported": {}, "last_run": None}


def save_import_tracker(tracker: dict) -> None:
    """Save import history."""
    try:
        DEFAULT_TRAIN_DIR.mkdir(parents=True, exist_ok=True)
        with open(IMPORT_TRACKER, "w") as f:
            json.dump(tracker, f, indent=2)
    except Exception as e:
        print(f"ERROR: Failed to save import tracker: {e}", file=sys.stderr)


def should_import_file(filepath: Path, tracker: dict, base_dir: Path | None = None, verbose: bool = False) -> bool:
    """Check if file should be imported (new or updated).

    `base_dir` if provided is used to compute a stable relative path for
    the import tracker keys. This mirrors the signature used by shared
    `ingest_utils.should_import_file`.
    """
    try:
        stat = filepath.stat()
        current_hash = get_file_hash(filepath)
        current_mtime = stat.st_mtime

        # Compute a sensible relative path: prefer base_dir when provided,
        # otherwise attempt a reasonable project-relative fallback.
        if base_dir is not None:
            try:
                rel_path = str(filepath.relative_to(base_dir))
            except Exception:
                rel_path = str(filepath)
        else:
            try:
                rel_path = str(filepath.relative_to(filepath.parent.parent.parent))
            except Exception:
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
        print(f"  [ERROR checking] {filepath.name}: {e}", file=sys.stderr)
        return False


def ingest_business_intel(jsonl_path: Path, verbose: bool = False) -> tuple[int, int, int]:
    """Run business intel ingestion script. Returns (ok, skip, error) counts."""
    try:
        script = Path(__file__).parent / "ingest_business_intel.py"
        if not script.exists():
            print(f"ERROR: {script} not found", file=sys.stderr)
            return 0, 0, 1

        cmd = [sys.executable, str(script), str(jsonl_path)]
        if verbose:
            print(f"  Running: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            # Parse output for counts
            output = result.stdout + result.stderr
            ok = output.count("Imported 1 record")
            return ok, 0, 0
        else:
            if verbose:
                print(f"  stderr: {result.stderr[:200]}")
            return 0, 0, 1
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return 0, 0, 1


def ingest_documents(doc_path: Path, verbose: bool = False) -> tuple[int, int, int]:
    """Run document ingestion script. Returns (ok, skip, error) counts."""
    try:
        script = Path(__file__).parent / "ingest_documents.py"
        if not script.exists():
            print(f"ERROR: {script} not found", file=sys.stderr)
            return 0, 0, 1

        cmd = [sys.executable, str(script), str(doc_path)]
        if verbose:
            print(f"  Running: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if result.returncode == 0:
            output = result.stdout + result.stderr
            # Parse ingest_documents.py output format
            import re
            match = re.search(r"(\d+) ok.*?(\d+) skip.*?(\d+) error", output)
            if match:
                return int(match.group(1)), int(match.group(2)), int(match.group(3))
            return 1, 0, 0
        else:
            if verbose:
                print(f"  stderr: {result.stderr[:200]}")
            return 0, 0, 1
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return 0, 0, 1


def auto_ingest(verbose: bool = False) -> int:
    """Scan for new intelligence data and import. Returns exit code."""
    print(f"\n{'='*80}")
    print(f"HAL Auto-Ingest started at {utc_ts()}")
    print(f"{'='*80}")

    tracker = load_import_tracker()
    total_ok, total_skip, total_error = 0, 0, 0

    # 1. Scan Business_Tools JSONL files
    if BUSINESS_TOOLS_PATH.exists():
        print(f"\nScanning {BUSINESS_TOOLS_PATH}...")
        jsonl_files = sorted(BUSINESS_TOOLS_PATH.glob("*.jsonl"))
        intel_to_import = [f for f in jsonl_files if should_import_file(f, tracker, BUSINESS_TOOLS_PATH, verbose)]

        if intel_to_import:
            print(f"  Found {len(intel_to_import)} new/updated business intel file(s)")
            for jsonl_file in intel_to_import:
                if verbose:
                    print(f"\n  Importing {jsonl_file.name}...")
                ok, skip, err = ingest_business_intel(jsonl_file, verbose)
                total_ok += ok
                total_skip += skip
                total_error += err

                # Update tracker
                try:
                    rel_path = str(jsonl_file.relative_to(BUSINESS_TOOLS_PATH))
                except Exception:
                    rel_path = str(jsonl_file)
                tracker["imported"][rel_path] = {
                    "hash": get_file_hash(jsonl_file),
                    "mtime": jsonl_file.stat().st_mtime,
                    "imported_at": utc_ts()
                }
        else:
            print("  No new/updated business intel files")
    else:
        if verbose:
            print(f"Business_Tools path not found: {BUSINESS_TOOLS_PATH}")

    # 2. Scan document folders (limited to recent files)
    doc_count = 0
    for doc_path in DOCUMENT_WATCH_PATHS:
        if not doc_path.exists():
            continue

        print(f"\nScanning {doc_path}...")
        supported_exts = {
            ".xlsx", ".xls", ".csv", ".tsv", ".json", ".jsonl",
            ".txt", ".md", ".pdf", ".docx", ".doc",
            ".log", ".yml", ".yaml", ".xml", ".html", ".htm"
        }

        doc_files = [
            f for f in doc_path.glob("*")
            if f.is_file() and f.suffix.lower() in supported_exts
            and should_import_file(f, tracker, doc_path, verbose)
        ]

        # Limit to files modified in last 7 days to avoid re-importing old docs
        now = datetime.now(timezone.utc).timestamp()
        recent_docs = [
            f for f in doc_files
            if (now - f.stat().st_mtime) < (7 * 24 * 3600)
        ]

        if recent_docs:
            print(f"  Found {len(recent_docs)} new/updated document(s)")
            for doc_file in recent_docs[:5]:  # Limit to 5 per run
                if verbose:
                    print(f"\n  Importing {doc_file.name}...")
                ok, skip, err = ingest_documents(doc_file, verbose)
                total_ok += ok
                total_skip += skip
                total_error += err
                doc_count += 1

                # Update tracker
                try:
                    rel_path = str(doc_file.relative_to(doc_path))
                except Exception:
                    rel_path = str(doc_file)
                tracker["imported"][rel_path] = {
                    "hash": get_file_hash(doc_file),
                    "mtime": doc_file.stat().st_mtime,
                    "imported_at": utc_ts()
                }
        else:
            print("  No new/updated documents")

    # 3. Save updated tracker
    tracker["last_run"] = utc_ts()
    save_import_tracker(tracker)

    # Summary
    print(f"\n{'─'*80}")
    print(f"Summary:")
    print(f"  Imported:  {total_ok}")
    print(f"  Skipped:   {total_skip}")
    print(f"  Errors:    {total_error}")
    print(f"  Total files tracked: {len(tracker['imported'])}")
    print(f"  Last run: {tracker['last_run']}")
    print(f"{'='*80}\n")

    return 0 if total_error == 0 else 1


def main():
    ap = argparse.ArgumentParser(
        description="Automatically ingest new intelligence data into HAL training"
    )
    ap.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )
    ap.add_argument(
        "--sync-intel",
        metavar="PATH",
        help="Explicitly sync a Business_Tools directory (one-time)",
    )
    ap.add_argument(
        "--track-reset",
        action="store_true",
        help="Reset import tracker (re-import everything)",
    )
    ap.add_argument(
        "--show-tracker",
        action="store_true",
        help="Display current import tracker",
    )
    ap.add_argument(
        "--watch",
        action="store_true",
        help="Watch for filesystem changes and auto-ingest (uses watchdog if available)",
    )
    ap.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Fallback poll interval in seconds when watchdog is not available",
    )

    args = ap.parse_args()

    if args.track_reset:
        if IMPORT_TRACKER.exists():
            IMPORT_TRACKER.unlink()
            print(f"Import tracker reset. Next run will re-import all files.")
        sys.exit(0)

    if args.show_tracker:
        tracker = load_import_tracker()
        print(json.dumps(tracker, indent=2))
        sys.exit(0)

    if args.sync_intel:
        path = Path(args.sync_intel).expanduser()
        if not path.exists():
            print(f"ERROR: Path not found: {path}", file=sys.stderr)
            sys.exit(1)
        print(f"\nManual sync of {path}")
        jsonl_files = sorted(path.glob("*.jsonl"))
        total_ok, total_error = 0, 0
        for jsonl_file in jsonl_files:
            print(f"  Importing {jsonl_file.name}...")
            ok, _, err = ingest_business_intel(jsonl_file, args.verbose)
            total_ok += ok
            total_error += err
        print(f"Done: {total_ok} imported, {total_error} errors\n")
        sys.exit(0)

    if args.watch:
        # Prefer watchdog if installed, else fallback to polling
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class ChangeHandler(FileSystemEventHandler):
                def __init__(self, cb, verbose=False):
                    self.cb = cb
                    self.verbose = verbose

                def on_any_event(self, event):
                    if self.verbose:
                        print('Filesystem change detected:', event)
                    try:
                        self.cb(self.verbose)
                    except Exception:
                        pass

            def run_watch(verbose=False):
                handler = ChangeHandler(auto_ingest, verbose=verbose)
                obs = Observer()
                paths = [BUSINESS_TOOLS_PATH] + DOCUMENT_WATCH_PATHS
                for p in paths:
                    if p.exists():
                        obs.schedule(handler, str(p), recursive=False)
                obs.start()
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    obs.stop()
                obs.join()

            print('Starting watchdog-based auto-ingest watcher')
            run_watch(verbose=args.verbose)
        except Exception:
            print('watchdog not available; falling back to polling mode')
            try:
                while True:
                    auto_ingest(verbose=args.verbose)
                    time.sleep(args.poll_interval)
            except KeyboardInterrupt:
                print('Watcher stopped')
        sys.exit(0)

    sys.exit(auto_ingest(verbose=args.verbose))


if __name__ == "__main__":
    main()
