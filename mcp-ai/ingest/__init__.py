"""Ingest package: thin wrappers to invoke existing ingest scripts.

This package provides a small programmatic entrypoint to run the
existing top-level ingest scripts (e.g., `ingest_documents`, `ingest_urls`)
without duplicating their logic. It calls the module `main()` functions by
temporarily setting `sys.argv`.
"""
from __future__ import annotations

import importlib
import sys
from typing import List


def _call_module_main(mod_name: str, argv: List[str]) -> int:
    """Import module by name and call its `main()` function with simulated argv.

    Returns the module's `main()` return value when available, or 0 on success.
    """
    old_argv = sys.argv[:]
    try:
        sys.argv = [mod_name] + argv
        mod = importlib.import_module(mod_name)
        # Try to reload to ensure consistent behavior when invoked multiple times
        importlib.reload(mod)
        if hasattr(mod, 'main'):
            rv = mod.main()
            return int(rv) if isinstance(rv, int) else 0
        return 0
    finally:
        sys.argv = old_argv


def ingest_documents(argv: List[str]) -> int:
    return _call_module_main('ingest_documents', argv)


def ingest_urls(argv: List[str]) -> int:
    return _call_module_main('ingest_urls', argv)


def ingest_business_intel(argv: List[str]) -> int:
    return _call_module_main('ingest_business_intel', argv)


def ingest_artifacts(argv: List[str]) -> int:
    return _call_module_main('ingest_artifacts', argv)


def ingest_history(argv: List[str]) -> int:
    return _call_module_main('ingest_history', argv)


def ingest_redhat_docs(argv: List[str]) -> int:
    return _call_module_main('ingest_redhat_docs', argv)
