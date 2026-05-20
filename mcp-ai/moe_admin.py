#!/usr/bin/env python3
"""Admin CLI for MoE runtime tasks (model_map reload/show)."""
from __future__ import annotations

import argparse
import json
import sys

HERE = __import__('os').path.dirname(__file__)
sys.path.insert(0, HERE)

import moe_router


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='MoE admin CLI')
    p.add_argument('cmd', choices=['show', 'reload'], help='show or reload model_map')
    args = p.parse_args(argv or sys.argv[1:])

    if args.cmd == 'show':
        mm = moe_router.load_model_map()
        print(json.dumps(mm, indent=2))
        return 0

    if args.cmd == 'reload':
        try:
            moe_router.MODEL_MAP = None
            mm = moe_router.load_model_map()
            print('Reloaded model_map entries:', len(mm))
            return 0
        except Exception as e:
            print('Reload failed:', e, file=sys.stderr)
            return 2


if __name__ == '__main__':
    raise SystemExit(main())
