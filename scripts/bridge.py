#!/usr/bin/env python3
"""Compatibility shim to locate and run the canonical MCP bridge.

This file is placed under `scripts/` for discovery convenience. It will
walk parent directories to find `mcp-ai/bridge.py` so it continues to work
whether invoked from the repo root or `scripts/` directory.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def find_bridge() -> Path:
    p = Path(__file__).resolve().parent
    # Walk up until we find mcp-ai/bridge.py
    while True:
        candidate = p / "mcp-ai" / "bridge.py"
        if candidate.exists():
            return candidate
        if p.parent == p:
            break
        p = p.parent
    return Path("")


def main() -> None:
    bridge_path = find_bridge()
    if not bridge_path or not bridge_path.exists():
        print(f"Could not find canonical bridge at {bridge_path}", file=sys.stderr)
        sys.exit(2)
    # Execute canonical bridge script as __main__ so CLI args behave as expected.
    runpy.run_path(str(bridge_path), run_name="__main__")


if __name__ == "__main__":
    main()
