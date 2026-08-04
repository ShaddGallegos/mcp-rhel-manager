#!/usr/bin/env python3
"""Prepare supplemental training data, scan, clean, and optionally encrypt/convert.

Usage examples:
  python mcp-ai/supplemental_training.py --name mycorp --url https://example.com/news
  python mcp-ai/supplemental_training.py --name mydocs --dirs /data/docs --encrypt

This script is conservative: it scans with `clamscan` if available, redacts
obvious secrets using `mcp-ai/redact_training.py`, and writes a JSONL dataset
to `~/.ansible/.supplementaltraining/<name>/dataset.jsonl`. If `--encrypt` is
specified (default), the output file will be encrypted using the ansible vault
password file at `~/.ansible/conf/.vaultpass.txt` when available or via the
local `mcp-ai/training_crypto.py` fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional
try:
    import ingest_common
    _HAS_INGEST_COMMON = True
except Exception:
    _HAS_INGEST_COMMON = False

try:
    import requests
except Exception:
    requests = None

# reuse redaction utilities (local sibling modules)
import redact_training
import training_crypto

LOG = logging.getLogger("supplemental_training")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_BASE = Path.home() / ".ansible" / ".supplementaltraining"
ALT_COMPAT = Path.home() / ".ansible" / ".suplamentaltraining"
VAULT_PASSFILE = Path.home() / ".ansible" / "conf" / ".vaultpass.txt"


def ensure_outdir(name: str) -> Path:
    out = DEFAULT_BASE / name
    out.mkdir(parents=True, exist_ok=True)
    # compatibility: create an alias with common misspelling
    try:
        if not ALT_COMPAT.exists():
            ALT_COMPAT.parent.mkdir(parents=True, exist_ok=True)
            if not ALT_COMPAT.exists():
                # create a symlink to the correct dir for user convenience
                try:
                    ALT_COMPAT.symlink_to(DEFAULT_BASE)
                except Exception:
                    pass
    except Exception:
        pass
    return out


def sanitize_text(s: str) -> str:
    # use redact_training to remove PEMs, tokens, emails, etc.
    return redact_training.redact_text(s)


def is_binary(path: Path) -> bool:
    try:
        b = path.read_bytes()
        return b.find(b"\0") != -1
    except Exception:
        return False


def fetch_url(url: str, outdir: Path) -> Optional[Path]:
    if requests is None:
        LOG.warning("requests not installed; skipping URL: %s", url)
        return None
    try:
        r = requests.get(url, timeout=30, stream=True)
        r.raise_for_status()
        fn = url.split("//")[-1].replace('/', '_')
        target = outdir / (fn + ".download")
        with open(target, 'wb') as fh:
            for chunk in r.iter_content(8192):
                if not chunk:
                    continue
                fh.write(chunk)
        return target
    except Exception as exc:
        LOG.warning("Failed to fetch %s: %s", url, exc)
        return None


def try_clamscan(path: Path) -> bool:
    # return True if clean, False if infected or scan inconclusive
    # Allow tests or operators to skip clamscan by setting SUPPLEMENTAL_SKIP_CLAMS=1
    if os.environ.get('SUPPLEMENTAL_SKIP_CLAMS', '').lower() in ('1', 'true', 'yes'):
        LOG.info('SUPPLEMENTAL_SKIP_CLAMS set; skipping malware scan for %s', path)
        return True

    clamscan = shutil.which('clamscan') or shutil.which('clamdscan')
    if not clamscan:
        LOG.info('clamscan not available; skipping malware scan for %s', path)
        return True
    try:
        cp = subprocess.run([clamscan, '-r', '--no-summary', str(path)], capture_output=True, text=True, timeout=300)
        out = cp.stdout + cp.stderr
        if 'Infected files: 0' in out or cp.returncode == 0:
            return True
        LOG.warning('Malware scan reported issues for %s: %s', path, out.splitlines()[-1] if out else cp.returncode)
        return False
    except Exception as exc:
        LOG.warning('Malware scan failed: %s', exc)
        return False


def extract_text_from_file(p: Path) -> Optional[str]:
    # Support plain text, HTML, PDF (if pypdf installed), and simple docx via python-docx
    if p.suffix.lower() in ('.txt', '.md', '.json', '.jsonl'):
        try:
            return p.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            return None
    if p.suffix.lower() in ('.html', '.htm'):
        try:
            from bs4 import BeautifulSoup

            html = p.read_text(encoding='utf-8', errors='ignore')
            soup = BeautifulSoup(html, 'html.parser')
            return soup.get_text('\n')
        except Exception:
            # fallback: strip tags
            try:
                t = p.read_text(encoding='utf-8', errors='ignore')
                import re

                return re.sub(r'<[^>]+>', ' ', t)
            except Exception:
                return None
    if p.suffix.lower() == '.pdf':
        try:
            import pypdf

            reader = pypdf.PdfReader(str(p))
            parts = []
            for pg in reader.pages:
                try:
                    parts.append(pg.extract_text() or '')
                except Exception:
                    continue
            return '\n'.join(parts)
        except Exception:
            LOG.info('pypdf not available or failed to parse PDF %s; skipping', p)
            return None
    # docx
    if p.suffix.lower() == '.docx':
        try:
            import docx

            doc = docx.Document(str(p))
            return '\n'.join([p.text for p in doc.paragraphs])
        except Exception:
            return None
    return None


def build_dataset(entries: List[dict], out_file: Path) -> None:
    # Prefer atomic, append-only writes when helpers are available.
    if _HAS_INGEST_COMMON:
        # Remove existing target to preserve original semantics (fresh file)
        try:
            if out_file.exists():
                out_file.unlink()
        except Exception:
            pass
        for e in entries:
            ingest_common.append_jsonl(str(out_file), e)
    else:
        with out_file.open('w', encoding='utf-8') as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + '\n')


def maybe_convert_to_gguf(outdir: Path, dataset: Path, gguf_tool: Optional[str]) -> Optional[Path]:
    # Conversion tools vary; try a few known commands if requested
    if not gguf_tool:
        gguf_tool = shutil.which('gguf-convert') or shutil.which('convert-gguf')
    if not gguf_tool:
        LOG.info('No gguf conversion tool available; skipping conversion')
        return None
    gguf_out = outdir / (dataset.stem + '.gguf')
    try:
        cp = subprocess.run([gguf_tool, str(dataset), str(gguf_out)], capture_output=True, text=True, timeout=3600)
        if cp.returncode == 0 and gguf_out.exists():
            LOG.info('GGUF conversion succeeded: %s', gguf_out)
            return gguf_out
        LOG.warning('GGUF conversion failed: %s', cp.stderr or cp.stdout)
        return None
    except Exception as exc:
        LOG.warning('GGUF conversion exception: %s', exc)
        return None


def encrypt_with_ansible_vault(target: Path, vault_passfile: Path) -> bool:
    av = shutil.which('ansible-vault')
    if not av:
        return False
    if not vault_passfile.exists():
        LOG.warning('Vault password file not found: %s', vault_passfile)
        return False
    try:
        cp = subprocess.run([av, 'encrypt', '--vault-password-file', str(vault_passfile), str(target)], capture_output=True, text=True, timeout=600)
        if cp.returncode == 0:
            LOG.info('Encrypted %s with ansible-vault', target)
            return True
        LOG.warning('ansible-vault failed: %s', cp.stderr or cp.stdout)
        return False
    except Exception as exc:
        LOG.warning('ansible-vault exception: %s', exc)
        return False


def fallback_encrypt(target: Path, vault_passfile: Path) -> bool:
    # Read password from vault_passfile if available, else ask user
    if vault_passfile.exists():
        pwd = vault_passfile.read_text(encoding='utf-8').strip()
    else:
        LOG.warning('Vault password file not found; cannot fallback encrypt without prompting')
        return False
    try:
        training_crypto.encrypt_file(target, password=pwd, iterations=390000)
        LOG.info('Encrypted %s with local training_crypto', target)
        return True
    except Exception as exc:
        LOG.warning('fallback encryption failed: %s', exc)
        return False


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog='supplemental_training')
    p.add_argument('--name', required=True, help='Name for this supplemental model')
    p.add_argument('--url', action='append', help='URL to fetch and include (can repeat)')
    p.add_argument('--url-file', help='File with one URL per line')
    p.add_argument('--dirs', nargs='*', help='Local directories to scan for documents')
    p.add_argument('--paths', nargs='*', help='Local files to include explicitly')
    p.add_argument('--outdir', help='Optional output base dir (defaults to ~/.ansible/.supplementaltraining)')
    p.add_argument('--no-encrypt', action='store_true', help='Do not encrypt final artifacts')
    p.add_argument('--convert-gguf', action='store_true', help='Attempt to convert dataset to GGUF (requires external tool)')
    p.add_argument('--gguf-tool', help='Path to gguf converter tool (optional)')
    args = p.parse_args(argv or sys.argv[1:])

    out_base = Path(args.outdir) if args.outdir else DEFAULT_BASE
    outdir = ensure_outdir(args.name) if args.outdir is None else (Path(args.outdir) / args.name)
    outdir.mkdir(parents=True, exist_ok=True)

    rawdir = outdir / 'raw'
    rawdir.mkdir(parents=True, exist_ok=True)
    textdir = outdir / 'text'
    textdir.mkdir(parents=True, exist_ok=True)

    urls = args.url or []
    if args.url_file:
        try:
            for line in Path(args.url_file).read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line:
                    urls.append(line)
        except Exception:
            LOG.warning('Failed to read url-file: %s', args.url_file)

    # fetch URLs
    for u in urls:
        LOG.info('Fetching URL: %s', u)
        f = fetch_url(u, rawdir)
        if f:
            LOG.info('Fetched to %s', f)

    # copy explicit paths
    paths = args.paths or []
    for p in paths:
        src = Path(p).expanduser()
        if not src.exists():
            LOG.warning('Path not found: %s', src); continue
        if src.is_dir():
            for sub in src.rglob('*'):
                if sub.is_file():
                    dst = rawdir / sub.name
                    try: shutil.copy2(sub, dst)
                    except Exception: pass
        else:
            dst = rawdir / src.name
            try: shutil.copy2(src, dst)
            except Exception: pass

    # scan directories
    if args.dirs:
        for d in args.dirs:
            dd = Path(d).expanduser()
            if not dd.exists():
                LOG.warning('Dir not found: %s', dd); continue
            for f in dd.rglob('*'):
                if f.is_file():
                    try:
                        shutil.copy2(f, rawdir / f.name)
                    except Exception:
                        continue

    # Malware scan raw dir
    all_raw = list(rawdir.glob('*'))
    clean_files = []
    for f in all_raw:
        ok = try_clamscan(f)
        if ok:
            clean_files.append(f)
        else:
            qd = outdir / 'quarantine'
            qd.mkdir(exist_ok=True)
            try:
                shutil.move(str(f), qd / f.name)
            except Exception:
                LOG.warning('Failed to quarantine %s', f)

    # extract text and redact
    entries = []
    seen_hashes = set()
    for f in clean_files:
        text = extract_text_from_file(f)
        if not text:
            continue
        text = sanitize_text(text)
        # simple dedupe
        h = hashlib.sha256(text.encode('utf-8')).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        eid = str(uuid.uuid4())
        entries.append({'id': eid, 'source': str(f.name), 'text': text})
        # also write individual text file
        try:
            with (textdir / (eid + '.txt')).open('w', encoding='utf-8') as fh:
                fh.write(text)
        except Exception:
            pass

    if not entries:
        LOG.warning('No text entries extracted; nothing to build')
        return 2

    dataset = outdir / 'dataset.jsonl'
    build_dataset(entries, dataset)
    LOG.info('Wrote dataset: %s (%d entries)', dataset, len(entries))

    gguf_path = None
    if args.convert_gguf:
        gguf_path = maybe_convert_to_gguf(outdir, dataset, args.gguf_tool)

    # encrypt artifacts
    if not args.no_encrypt:
        target = gguf_path or dataset
        ok = False
        if shutil.which('ansible-vault'):
            ok = encrypt_with_ansible_vault(target, VAULT_PASSFILE)
        if not ok:
            ok = fallback_encrypt(target, VAULT_PASSFILE)
        if not ok:
            LOG.warning('Encryption not performed for %s', target)
        else:
            LOG.info('Encrypted: %s', target)

    print('Supplemental training dataset available at:', outdir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
