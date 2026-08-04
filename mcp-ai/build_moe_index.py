#!/usr/bin/env python3
"""Utility to (re)build the MoE profile index and cache it to disk.

Writes index artifacts into ~/.mcp-ai/embeds for faster startup.
"""
from __future__ import annotations

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rebuild', action='store_true', help='Force rebuild index (ignore cached)')
    parser.add_argument('--add-profile', nargs=2, metavar=('NAME', 'KW'), help='Add/update a profile name with space-separated keywords')
    parser.add_argument('--remove-profile', metavar='NAME', help='Remove a named profile')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    # import the moe_router module from package
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        import mcp_ai.moe_router as moe
    except Exception:
        # fallback to direct import path
        import importlib.util
        spec = importlib.util.spec_from_file_location('moe_router', os.path.join(os.path.dirname(__file__), 'moe_router.py'))
        moe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(moe)

    if args.rebuild:
        # remove cached files if present
        try:
            d = moe._embeds_dir()
            paths = moe._index_disk_paths()
            for p in paths.values():
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
        except Exception:
            pass

    if args.add_profile:
        name, kw = args.add_profile
        try:
            # add profile to module and rebuild index
            if hasattr(moe, '_add_profile'):
                ok = moe._add_profile(name, kw)
                if ok:
                    print(f'Profile {name} added/updated.')
                    sys.exit(0)
            print('Add profile not supported in this build of moe_router.')
            sys.exit(2)
        except Exception as e:
            print('Failed to add profile:', e)
            sys.exit(2)

    if args.remove_profile:
        name = args.remove_profile
        try:
            if hasattr(moe, '_remove_profile'):
                ok = moe._remove_profile(name)
                if ok:
                    print(f'Profile {name} removed.')
                    sys.exit(0)
            print('Remove profile not supported in this build of moe_router.')
            sys.exit(2)
        except Exception as e:
            print('Failed to remove profile:', e)
            sys.exit(2)

    ok = moe._build_profile_index()
    if not ok:
        print('Failed to build/load profile index')
        sys.exit(2)
    print('Profile index ready: type=%s entries=%d' % (moe._INDEX_TYPE, len(moe._INDEX_NAMES) if moe._INDEX_NAMES else 0))


if __name__ == '__main__':
    main()
