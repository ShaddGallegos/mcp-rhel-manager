#!/usr/bin/env python3
"""Simple pure-Python search over reindex_embeddings JSONL outputs.

Provides `search_index(index_path, query, top_k=5)` returning list of dicts:
  {'id','text_preview','score','meta'}

This uses the same hashing-based embedding as `reindex_embeddings._hash_vector`.
"""
import json
import math
import os
from pathlib import Path

try:
    from scripts.reindex_embeddings import _hash_vector
except Exception:
    # fallback: simple local reimplementation
    import hashlib
    def _hash_vector(text, dim=64):
        h = hashlib.sha256(text.encode('utf-8')).digest()
        vec = []
        i = 0
        while len(vec) < dim:
            chunk = hashlib.sha256(h + i.to_bytes(2, 'little')).digest()
            for b in chunk:
                vec.append((b / 255.0))
                if len(vec) >= dim:
                    break
            i += 1
        return [float(x) for x in vec[:dim]]


_INDEX_CACHE = {}


def _load_index(index_path):
    p = Path(index_path)
    key = str(p.resolve())
    mtime = p.stat().st_mtime if p.exists() else 0
    cached = _INDEX_CACHE.get(key)
    if cached and cached.get('mtime') == mtime:
        return cached['records']
    records = []
    if not p.exists():
        return records
    with open(p, 'r', encoding='utf-8') as fh:
        for ln in fh:
            try:
                j = json.loads(ln)
                vec = j.get('vector')
                if not vec:
                    # derive from text preview
                    vec = _hash_vector(j.get('text_preview','') or '')
                records.append({'id': j.get('id'), 'meta': j.get('meta', {}), 'text_preview': j.get('text_preview',''), 'vector': vec})
            except Exception:
                continue
    _INDEX_CACHE[key] = {'mtime': mtime, 'records': records}
    return records


def _dot(a, b):
    return sum(x*y for x,y in zip(a,b))


def _norm(a):
    return math.sqrt(sum(x*x for x in a))


def _cosine(a,b):
    na = _norm(a)
    nb = _norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return _dot(a,b)/(na*nb)


def search_index(index_path, query, top_k=5, dim=64):
    records = _load_index(index_path)
    if not records:
        return []
    qv = _hash_vector(query, dim=dim)
    scored = []
    for r in records:
        vec = r.get('vector')
        if not vec:
            continue
        try:
            score = _cosine(qv, vec)
        except Exception:
            score = 0.0
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, r in scored[:top_k]:
        rec = {'id': r.get('id'), 'meta': r.get('meta'), 'text_preview': r.get('text_preview'), 'score': float(score)}
        out.append(rec)
    return out
 