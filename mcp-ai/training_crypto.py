#!/usr/bin/env python3
"""Encrypt/decrypt HAL training files with a password-derived key.

This script keeps data local by operating on local files only.
Encrypted files use the extension .enc and include metadata needed for decryption.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


DEFAULT_TRAIN_DIR = Path(os.path.expanduser("~/.mcp-ai/training"))
ENC_SUFFIX = ".enc"


def derive_key(password: str, salt: bytes, iterations: int = 390000) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    key = kdf.derive(password.encode("utf-8"))
    return base64.urlsafe_b64encode(key)


def build_cipher(password: str, salt: bytes, iterations: int) -> Fernet:
    key = derive_key(password=password, salt=salt, iterations=iterations)
    return Fernet(key)


def should_encrypt(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name.startswith("."):
        return False
    if path.suffix == ENC_SUFFIX:
        return False
    return True


def should_decrypt(path: Path) -> bool:
    return path.is_file() and path.suffix == ENC_SUFFIX


def encrypt_file(path: Path, password: str, iterations: int) -> Path:
    raw = path.read_bytes()
    salt = secrets.token_bytes(16)
    cipher = build_cipher(password=password, salt=salt, iterations=iterations)
    token = cipher.encrypt(raw)
    envelope = {
        "v": 1,
        "alg": "fernet-pbkdf2-sha256",
        "kdf": {
            "name": "PBKDF2HMAC",
            "iterations": iterations,
            "salt_b64": base64.b64encode(salt).decode("ascii"),
        },
        "source_name": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "ciphertext_b64": token.decode("utf-8"),
    }
    out_path = path.with_name(path.name + ENC_SUFFIX)
    out_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    path.unlink()
    return out_path


def decrypt_file(path: Path, password: str) -> Path:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    kdf_cfg = envelope.get("kdf") or {}
    iterations = int(kdf_cfg.get("iterations", 390000))
    salt_b64 = kdf_cfg.get("salt_b64")
    if not salt_b64:
        raise ValueError("missing salt in encrypted file")
    salt = base64.b64decode(salt_b64)
    cipher = build_cipher(password=password, salt=salt, iterations=iterations)
    token = (envelope.get("ciphertext_b64") or "").encode("utf-8")
    plain = cipher.decrypt(token)

    source_name = envelope.get("source_name") or path.stem
    out_path = path.with_name(source_name)
    out_path.write_bytes(plain)

    expected_sha = envelope.get("sha256")
    actual_sha = hashlib.sha256(plain).hexdigest()
    if expected_sha and expected_sha != actual_sha:
        out_path.unlink(missing_ok=True)
        raise ValueError(f"integrity mismatch for {path}")

    path.unlink()
    return out_path


def list_targets(indir: Path, mode: str, recursive: bool) -> list[Path]:
    iterator = indir.rglob("*") if recursive else indir.glob("*")
    files = []
    for p in iterator:
        if mode == "encrypt" and should_encrypt(p):
            files.append(p)
        elif mode == "decrypt" and should_decrypt(p):
            files.append(p)
    return sorted(files)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Encrypt/decrypt HAL local training data")
    p.add_argument("mode", choices=["encrypt", "decrypt"], help="Operation mode")
    p.add_argument("--indir", default=str(DEFAULT_TRAIN_DIR), help="Directory containing training files")
    p.add_argument("--recursive", action="store_true", help="Recurse into subdirectories")
    p.add_argument("--iterations", type=int, default=390000, help="PBKDF2 iterations (encrypt mode)")
    p.add_argument("--password-env", default="HAL_TRAINING_KEY", help="Env var that holds password")
    return p.parse_args()


def resolve_password(env_var: str) -> str:
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env
    p1 = getpass.getpass("Training password: ")
    if not p1:
        raise ValueError("empty password is not allowed")
    return p1


def main() -> int:
    args = parse_args()
    indir = Path(os.path.expanduser(args.indir)).resolve()
    if not indir.exists() or not indir.is_dir():
        print(f"Input directory does not exist: {indir}", file=sys.stderr)
        return 2

    try:
        password = resolve_password(args.password_env)
    except Exception as exc:
        print(f"Password error: {exc}", file=sys.stderr)
        return 2

    files = list_targets(indir=indir, mode=args.mode, recursive=args.recursive)
    if not files:
        print(f"No files to {args.mode} in {indir}")
        return 0

    print(f"Found {len(files)} file(s) to {args.mode}")
    ok = 0
    errors = 0
    for f in files:
        try:
            if args.mode == "encrypt":
                out = encrypt_file(f, password=password, iterations=args.iterations)
            else:
                out = decrypt_file(f, password=password)
            ok += 1
            print(f"OK: {f} -> {out}")
        except Exception as exc:
            errors += 1
            print(f"ERR: {f} ({exc})", file=sys.stderr)

    print(f"Done. success={ok} errors={errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
