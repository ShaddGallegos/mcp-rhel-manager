#!/usr/bin/env python3
"""Unified CLI for common mcp-ai utilities.

Provides subcommands that dispatch to existing mcp-ai scripts/modules.
This keeps the UX consistent and reduces the number of standalone script
entrypoints to maintain.
"""
from __future__ import annotations

import argparse
import sys


def _run_module(module_name: str, args: list[str]):
    # import locally to keep startup fast when not used
    import importlib
    importlib.invalidate_caches()
    mod = importlib.import_module(module_name)
    # call main() where available
    if hasattr(mod, 'main'):
        # simulate argv inside module
        old_argv = sys.argv[:]
        try:
            sys.argv = [module_name] + args
            rv = mod.main()
            return 0 if rv is None else int(rv)
        finally:
            sys.argv = old_argv
    # fallback: no-op
    return 0


def main():
    p = argparse.ArgumentParser(prog='mcp-ai')
    sp = p.add_subparsers(dest='cmd')

    sp.add_parser('install-assistant', help='Run interactive install assistant')
    sp.add_parser('precompute-embeds', help='Run precompute_embeds script')
    sp.add_parser('dynamic-menu', help='Run dynamic menu demo')
    sp.add_parser('llm-health', help='Run llm health server')
    sp.add_parser('moe', help='Run moe router ad-hoc (prints aggregation)')
    sp.add_parser('ingest-docs', help='Ingest local documents')
    sp.add_parser('ingest-urls', help='Ingest URLs')
    sp.add_parser('git', help='Git helper menu')

    args, rest = p.parse_known_args()
    cmd = args.cmd
    if cmd == 'install-assistant':
        return _run_module('install_assistant', rest)
    if cmd == 'precompute-embeds':
        return _run_module('precompute_embeds', rest)
    if cmd == 'dynamic-menu':
        return _run_module('dynamic_menu', rest)
    if cmd == 'llm-health':
        return _run_module('llm_health', rest)
    if cmd == 'moe':
        # delegate to moe_router CLI: pass through args
        return _run_module('moe_router', rest)
    if cmd == 'ingest-docs':
        # forward args to ingest_documents
        return _run_module('ingest_documents', rest)
    if cmd == 'ingest-urls':
        return _run_module('ingest_urls', rest)
    if cmd == 'git':
        return _run_module('git_manager', rest)

    p.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
