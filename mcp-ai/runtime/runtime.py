#!/usr/bin/env python3
"""
Minimal MCP LLM runtime helpers.

Provides simple CLI helpers to list models, download model archives into
`/var/lib/mcp-llms/<model-name>` and install LoRA adapter archives under
`/var/lib/mcp-llms/<model-name>/adapters/<adapter-name>`.

Designed to be small and dependency-free so it can run inside the host
environment or inside a slim management container.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile

DEFAULT_MODEL_DIR = "/var/lib/mcp-llms"


def ensure_model_dir(path: str = DEFAULT_MODEL_DIR) -> str:
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except PermissionError:
        # Fall back to a per-user directory when the host store is not writable
        fallback = os.environ.get("MCP_LLMS", os.path.expanduser("~/.local/share/mcp-llms"))
        os.makedirs(fallback, exist_ok=True)
        print(f"Warning: cannot create {path}, falling back to {fallback}", file=sys.stderr)
        return fallback


def list_models(path: str = DEFAULT_MODEL_DIR) -> list[str]:
    real = ensure_model_dir(path)
    return sorted([d for d in os.listdir(real) if os.path.isdir(os.path.join(real, d))])


def model_info(name: str, path: str = DEFAULT_MODEL_DIR) -> dict:
    base = ensure_model_dir(path)
    p = os.path.join(base, name)
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    files = 0
    dirs = 0
    size = 0
    for root, ds, fs in os.walk(p):
        dirs += len(ds)
        files += len(fs)
        for f in fs:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return {"path": p, "dirs": dirs, "files": files, "size": size}


def _sha256(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def download_and_extract(url: str, name: str, path: str = DEFAULT_MODEL_DIR, checksum: str | None = None) -> str:
    """
    Download `url` and extract into `path/name`.
    Supports tar (gz/xz/bz2) and zip; if the URL is a raw file it will be
    moved into the model directory.

    Returns the destination directory.
    """
    ensure_model_dir(path)
    dest = os.path.join(path, name)
    if os.path.exists(dest):
        raise FileExistsError(f"Destination already exists: {dest}")

    tmp = tempfile.mkdtemp(prefix="mcp_dl_")
    try:
        parsed = urllib.parse.urlparse(url)
        fname = os.path.basename(parsed.path) or "download"
        tmpfile = os.path.join(tmp, fname)
        print(f"Downloading {url} -> {tmpfile}")
        urllib.request.urlretrieve(url, tmpfile)

        if checksum:
            got = _sha256(tmpfile)
            if got.lower() != checksum.lower():
                raise ValueError(f"Checksum mismatch: expected {checksum}, got {got}")

        os.makedirs(dest, exist_ok=False)

        if tarfile.is_tarfile(tmpfile):
            print("Extracting tar archive...")
            with tarfile.open(tmpfile, "r:*") as tf:
                tf.extractall(dest)
        elif zipfile.is_zipfile(tmpfile):
            print("Extracting zip archive...")
            with zipfile.ZipFile(tmpfile) as zf:
                zf.extractall(dest)
        else:
            # Not an archive; move the raw file into the model dir
            print("Moving raw file into model directory")
            shutil.move(tmpfile, os.path.join(dest, fname))

        print(f"Model installed at: {dest}")
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def apply_lora(adapter_archive: str, model_name: str, path: str = DEFAULT_MODEL_DIR) -> str:
    """
    Extract a LoRA archive into the model's `adapters/` subdirectory.
    The adapter archive must be a tar or zip.

    Returns the adapter destination directory.
    """
    model_dir = os.path.join(path, model_name)
    if not os.path.isdir(model_dir):
        raise FileNotFoundError(f"Model not found: {model_dir}")
    adapters_dir = os.path.join(model_dir, "adapters")
    os.makedirs(adapters_dir, exist_ok=True)

    if not os.path.exists(adapter_archive):
        raise FileNotFoundError(adapter_archive)

    base = os.path.basename(adapter_archive)
    adapter_name = os.path.splitext(base)[0]
    dest = os.path.join(adapters_dir, adapter_name)
    if os.path.exists(dest):
        raise FileExistsError(dest)

    tmp = tempfile.mkdtemp(prefix="mcp_adapter_")
    try:
        if tarfile.is_tarfile(adapter_archive):
            with tarfile.open(adapter_archive, "r:*") as tf:
                tf.extractall(tmp)
        elif zipfile.is_zipfile(adapter_archive):
            with zipfile.ZipFile(adapter_archive) as zf:
                zf.extractall(tmp)
        else:
            raise ValueError("Adapter archive must be tar or zip")

        shutil.move(tmp, dest)
        print(f"Adapter installed: {dest}")
        return dest
    finally:
        if os.path.exists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def _cli() -> None:
    p = argparse.ArgumentParser(prog="mcp-runtime", description="MCP LLM runtime helpers")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="List installed models")

    info = sub.add_parser("info", help="Show info for a model")
    info.add_argument("model", nargs=1)

    dl = sub.add_parser("download", help="Download and extract a model archive")
    dl.add_argument("url")
    dl.add_argument("name")
    dl.add_argument("--checksum", default=None)

    app = sub.add_parser("apply-lora", help="Install a LoRA adapter archive for a model")
    app.add_argument("adapter_archive")
    app.add_argument("model")

    args = p.parse_args()
    if args.cmd == "list":
        for m in list_models():
            print(m)
    elif args.cmd == "info":
        m = args.model[0]
        try:
            info = model_info(m)
            print(f"Model: {m}")
            print(f"  Path: {info['path']}")
            print(f"  Dirs: {info['dirs']}  Files: {info['files']}  Size: {info['size']} bytes")
        except FileNotFoundError:
            print(f"Model not found: {m}", file=sys.stderr)
            sys.exit(2)
    elif args.cmd == "download":
        download_and_extract(args.url, args.name, checksum=args.checksum)
    elif args.cmd == "apply-lora":
        apply_lora(args.adapter_archive, args.model)
    else:
        p.print_help()


if __name__ == "__main__":
    _cli()
