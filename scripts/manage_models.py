#!/usr/bin/env python3
"""manage_models.py - simple model index manager for /var/lib/mcp-llms

Provides: add, remove, list, verify, cleanup operations on a JSON index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone


def load_index(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_index(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_add(args):
    idx = load_index(args.index)
    p = args.path
    if not os.path.exists(p):
        print(f"error: path not found: {p}", file=sys.stderr)
        return 2
    name = args.name or os.path.basename(p)
    size = os.path.getsize(p)
    checksum = args.checksum
    if not checksum:
        checksum = f"sha256:{sha256_of(p)}"
    entry = {
        "path": os.path.abspath(p),
        "checksum": checksum,
        "size": size,
        "added_at": iso_now(),
        "pinned": bool(args.pinned),
    }
    idx[name] = entry
    save_index(args.index, idx)
    print(json.dumps({"name": name, "entry": entry}, indent=2))
    return 0


def cmd_list(args):
    idx = load_index(args.index)
    print(json.dumps(idx, indent=2))
    return 0


def cmd_verify(args):
    idx = load_index(args.index)
    target = args.name or args.path
    # allow name or path
    found = None
    for k, v in idx.items():
        if k == target or v.get("path") == target:
            found = (k, v)
            break
    if not found:
        print("not found in index", file=sys.stderr)
        return 2
    k, v = found
    path = v.get("path")
    if not os.path.exists(path):
        print("file missing", file=sys.stderr)
        return 3
    actual = sha256_of(path)
    expected = v.get("checksum", "").split(":", 1)[-1]
    if actual == expected:
        print("OK")
        return 0
    else:
        print(f"MISMATCH expected {expected} got {actual}", file=sys.stderr)
        return 4


def cmd_remove(args):
    idx = load_index(args.index)
    name = args.name
    if name not in idx:
        print("not found", file=sys.stderr)
        return 2
    path = idx[name].get("path")
    try:
        if args.delete_file and path and os.path.exists(path):
            os.remove(path)
            print(f"removed file: {path}")
    except Exception as e:
        print(f"failed to remove file: {e}", file=sys.stderr)
    idx.pop(name, None)
    save_index(args.index, idx)
    print(f"removed entry: {name}")
    return 0


def cmd_cleanup(args):
    idx = load_index(args.index)
    now = time.time()
    removed = []
    days = int(args.days)
    cutoff = now - (days * 86400)
    for name, v in list(idx.items()):
        p = v.get("path")
        pinned = v.get("pinned", False)
        if pinned:
            continue
        if not p or not os.path.exists(p):
            print(f"index entry missing file, removing index entry: {name}")
            idx.pop(name, None)
            removed.append(name)
            continue
        mtime = os.path.getmtime(p)
        if mtime < cutoff:
            if args.dry_run:
                print(f"DRY: would remove {name} ({p})")
            else:
                try:
                    os.remove(p)
                    print(f"removed file: {p}")
                except Exception as e:
                    print(f"failed to remove {p}: {e}", file=sys.stderr)
                idx.pop(name, None)
                removed.append(name)
    save_index(args.index, idx)
    print(f"cleanup removed: {removed}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="Manage /var/lib/mcp-llms models index")
    p.add_argument("--index", default="/var/lib/mcp-llms/models.json", help="index JSON path")
    sp = p.add_subparsers(dest="cmd")

    pa = sp.add_parser("add")
    pa.add_argument("--path", required=True)
    pa.add_argument("--name")
    pa.add_argument("--checksum")
    pa.add_argument("--pinned", action="store_true")
    pa.set_defaults(func=cmd_add)

    pl = sp.add_parser("list")
    pl.set_defaults(func=cmd_list)

    pv = sp.add_parser("verify")
    pv.add_argument("--name")
    pv.add_argument("--path")
    pv.set_defaults(func=cmd_verify)

    pr = sp.add_parser("remove")
    pr.add_argument("--name", required=True)
    pr.add_argument("--delete-file", action="store_true")
    pr.set_defaults(func=cmd_remove)

    pc = sp.add_parser("cleanup")
    pc.add_argument("--days", default=30)
    pc.add_argument("--dry-run", action="store_true")
    pc.set_defaults(func=cmd_cleanup)

    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        p.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
