#!/usr/bin/env python3
"""Simple embeddings reindexer for HAL training data.

This is a lightweight, dependency-free reindexer. It reads text files or JSONL
records from the training directory and produces a compact JSONL index with
per-record id, metadata, and a pseudovector.

Usage:
  scripts/reindex_embeddings.py --input-dir PATH --out index.jsonl

The embedding implementation prefers `numpy` + `hashlib` token hashing if
available, otherwise falls back to a stable sha256-derived vector.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
try:
    import ingest_common
    _HAS_INGEST_COMMON = True
except Exception:
    _HAS_INGEST_COMMON = False


def _hash_vector(text, dim=64):
    # Deterministic pseudo-embedding using sha256 chunks
    h = hashlib.sha256(text.encode('utf-8')).digest()
    # Expand or fold into dim floats in [0,1)
    vec = []
    i = 0
    while len(vec) < dim:
        chunk = hashlib.sha256(h + i.to_bytes(2, 'little')).digest()
        for b in chunk:
            vec.append((b / 255.0))
            if len(vec) >= dim:
                break
        i += 1
    return [round(float(x), 6) for x in vec[:dim]]


def iter_training_texts(input_dir: str):
    p = Path(input_dir)
    if not p.exists():
        return
    for f in p.rglob('*.jsonl'):
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        j = json.loads(ln)
                        text = j.get('text') or j.get('content') or j.get('response') or ''
                        yield str(f) + ':' + (j.get('id') or j.get('path') or str(hash(text))) , text, {'src': str(f)}
                    except Exception:
                        continue
        except Exception:
            continue
    for f in p.rglob('*.txt'):
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                text = fh.read().strip()
                if text:
                    yield str(f), text, {'src': str(f)}
        except Exception:
            continue


def build_index(input_dir: str, out_file: str, dim=64):
    # Prefer atomic append helper when available; otherwise write atomically via tmp file.
    if _HAS_INGEST_COMMON:
        try:
            if os.path.exists(out_file):
                try:
                    os.remove(out_file)
                except Exception:
                    pass
            for rid, text, meta in iter_training_texts(input_dir):
                vec = _hash_vector(text, dim=dim)
                rec = {'id': rid, 'meta': meta, 'text_preview': text[:500], 'vector': vec}
                ingest_common.append_jsonl(out_file, rec)
        except Exception:
            # fallback to atomic tmp write
            tmp = out_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as out:
                for rid, text, meta in iter_training_texts(input_dir):
                    vec = _hash_vector(text, dim=dim)
                    rec = {'id': rid, 'meta': meta, 'text_preview': text[:500], 'vector': vec}
                    out.write(json.dumps(rec, ensure_ascii=False) + '\n')
            os.replace(tmp, out_file)
    else:
        tmp = out_file + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as out:
            for rid, text, meta in iter_training_texts(input_dir):
                vec = _hash_vector(text, dim=dim)
                rec = {'id': rid, 'meta': meta, 'text_preview': text[:500], 'vector': vec}
                out.write(json.dumps(rec, ensure_ascii=False) + '\n')
        os.replace(tmp, out_file)
    print(f'Wrote index: {out_file}')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input-dir', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--dim', type=int, default=64)
    args = p.parse_args()
    build_index(args.input_dir, args.out, dim=args.dim)


if __name__ == '__main__':
    main()
