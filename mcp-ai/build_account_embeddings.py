#!/usr/bin/env python3
"""Build account-level embeddings from local training data.

Usage:
  python3 mcp-ai/build_account_embeddings.py --rebuild --output-dir /tmp/emb

This script prefers `sentence-transformers` when available and falls back to
`sklearn` TF-IDF + TruncatedSVD if not. It writes `embeddings.npy` and
`ids.json` into the output directory; if `faiss` is available, a FAISS index
is written as `faiss.index`.
"""
from __future__ import annotations
import os
import sys
import json
import argparse
import importlib.util
from pathlib import Path
import numpy as np


def load_hal_for_train_dir() -> str:
    # Import `scripts/hal.py` to reuse its TRAIN_DIR config when present
    repo_root = Path(__file__).resolve().parents[1]
    hal_path = repo_root.joinpath('scripts', 'hal.py')
    if not hal_path.exists():
        # fallback
        return os.path.join(os.path.expanduser('~'), '.mcp-ai', 'training')
    spec = importlib.util.spec_from_file_location('hal_for_emb', str(hal_path))
    hal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hal)
    return getattr(hal, 'TRAIN_DIR', os.path.join(os.path.expanduser('~'), '.mcp-ai', 'training'))


def gather_account_docs(train_dir: str) -> tuple[list[str], list[str]]:
    p = Path(train_dir)
    docs = []
    ids = []
    if not p.exists():
        return docs, ids
    for fp in sorted(p.glob('*.json')):
        try:
            rec = json.loads(fp.read_text(encoding='utf-8'))
        except Exception:
            continue
        if rec.get('type') != 'business_intel_account':
            continue
        account = (rec.get('account_name') or fp.stem).strip()
        txt = ''
        parts = []
        for k in ('short_summary', 'text', 'primary_objective'):
            v = rec.get(k) or ''
            if isinstance(v, list):
                parts.extend([str(x) for x in v])
            else:
                parts.append(str(v))
        # headlines
        for h in rec.get('notable_news_headlines', []) or []:
            if isinstance(h, dict):
                parts.append(h.get('title') or h.get('link') or '')
            else:
                parts.append(str(h))
        txt = '\n'.join([p for p in parts if p])
        if not txt.strip():
            continue
        docs.append(txt)
        ids.append(account)
    return docs, ids


def build_embeddings_sentence_transformer(docs: list[str], model_name: str) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(model_name)
    embs = m.encode(docs, show_progress_bar=True, convert_to_numpy=True)
    return np.array(embs, dtype=np.float32)


def build_embeddings_sklearn(docs: list[str], dim: int = 384) -> np.ndarray:
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import TruncatedSVD
    except Exception as exc:
        raise RuntimeError('sklearn not available; please install sentence-transformers or scikit-learn') from exc
    vec = TfidfVectorizer(max_features=4096, stop_words='english')
    X = vec.fit_transform(docs)
    if X.shape[1] <= dim:
        # convert to dense
        return X.toarray().astype(np.float32)
    svd = TruncatedSVD(n_components=dim, random_state=42)
    Xr = svd.fit_transform(X)
    return Xr.astype(np.float32)


def try_build_faiss_index(embs: np.ndarray, out_dir: Path) -> bool:
    try:
        import faiss
    except Exception:
        return False
    d = embs.shape[1]
    index = faiss.IndexFlatL2(d)
    index.add(embs)
    faiss.write_index(index, str(out_dir.joinpath('faiss.index')))
    return True


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--output-dir', default=os.path.join(os.path.expanduser('~'), '.mcp-ai', 'models', 'account_embeddings'))
    ap.add_argument('--model', default='all-MiniLM-L6-v2', help='SentenceTransformer model name (when available)')
    args = ap.parse_args(argv)

    train_dir = load_hal_for_train_dir()
    docs, ids = gather_account_docs(train_dir)
    if not docs:
        print('No business_intel_account documents found in', train_dir)
        return 2

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    embs = None
    used = 'none'
    # Prefer sentence-transformers
    try:
        embs = build_embeddings_sentence_transformer(docs, args.model)
        used = f'sentence-transformers:{args.model}'
    except Exception:
        try:
            embs = build_embeddings_sklearn(docs)
            used = 'sklearn-tfidf-svd'
        except Exception as exc:
            print('Failed to build embeddings:', exc)
            return 3

    # Save numpy embeddings and id mapping
    np.save(str(out_dir.joinpath('embeddings.npy')), embs)
    with open(out_dir.joinpath('ids.json'), 'w', encoding='utf-8') as fh:
        json.dump(ids, fh, indent=2)
    print(f'Wrote embeddings ({embs.shape}) to {out_dir}')

    # Try FAISS
    ok = try_build_faiss_index(embs, out_dir)
    if ok:
        print('FAISS index written to', out_dir.joinpath('faiss.index'))
    else:
        print('FAISS not available; saved numpy embeddings only.')

    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
