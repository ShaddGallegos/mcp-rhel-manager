#!/usr/bin/env python3
"""Precompute expert profile embeddings and persist index files.

This helper computes embeddings for EXPERT_PROFILES defined in `moe_router` and
saves a numpy matrix and an optional FAISS index under the embeds directory
used by `moe_router` (typically `~/.mcp-ai/embeds` or `$AI_HOME/embeds`).

Usage: precompute_embeds.py [--model MODEL] [--no-faiss] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import logging

logger = logging.getLogger('precompute_embeds')


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--model', help='sentence-transformers model name (overrides MOE_SENT_TRANS_MODEL)')
    p.add_argument('--no-faiss', action='store_true', help='Do not attempt to build a FAISS index')
    p.add_argument('--verbose', action='store_true')
    args = p.parse_args(argv or sys.argv[1:])

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    try:
        import moe_router
    except Exception as e:
        print('Failed to import moe_router:', e, file=sys.stderr)
        return 2

    # Allow overriding model via CLI/env
    if args.model:
        os.environ['MOE_SENT_TRANS_MODEL'] = args.model

    print('Initializing embedding model and computing profile embeddings...')
    ok = moe_router._ensure_embedding_model()
    if not ok:
        print('Could not initialize embedding model. Ensure sentence-transformers is installed and available in the venv.', file=sys.stderr)
        return 3

    # Build index (FAISS + npz fallback). This function persists files used by moe_router.
    idx_ok = moe_router._build_profile_index()
    if not idx_ok:
        print('Index build failed (see logs).', file=sys.stderr)
        return 4

    paths = moe_router._index_disk_paths()
    print('Embeddings and index persisted to:', paths)
    return 0


if __name__ == '__main__':
    sys.exit(main())
