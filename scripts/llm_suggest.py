#!/usr/bin/env python3
"""Produce an LLM suggestion for a file or free-text prompt using configured experts.

This script is intentionally minimal: it composes a prompt with the file
contents and some context, then invokes the configured expert command via
`mcp-ai/llm_bridge.py`.

Example:
  ./scripts/llm_suggest.py --expert code_fixer --file path/to/file.py
  ./scripts/llm_suggest.py --expert code_fixer --text "Fix this bash snippet..."
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from importlib import util

# Load mcp-ai/llm_bridge.py as a module regardless of the package name (hyphen)
repo_root = Path(__file__).resolve().parents[1]
bridge_path = repo_root / "mcp-ai" / "llm_bridge.py"
if not bridge_path.exists():
    print(f"llm_bridge.py not found at {bridge_path}", file=sys.stderr)
    raise SystemExit(2)
spec = util.spec_from_file_location("mcp_ai.llm_bridge", str(bridge_path))
llm_mod = util.module_from_spec(spec)
spec.loader.exec_module(llm_mod)
_bridge = llm_mod


def build_prompt_from_file(path: str) -> str:
    try:
        text = open(path, "r", encoding="utf-8", errors="ignore").read()
    except Exception as exc:
        text = f"<ERROR reading file: {exc}>"
    prompt = f"You are a helpful expert that suggests minimal reproducible fixes.\nFile: {path}\n---BEGIN FILE---\n{text}\n---END FILE---\nPlease provide a compact patch in unified diff format and a short explanation. If no changes are needed, reply with 'NOCHANGE'."
    return prompt


def build_prompt_from_text(text: str) -> str:
    return f"You are an expert developer. Given the following request, produce a compact patch or a suggested change.\nRequest:\n{text}\nProvide a patch or 'NOCHANGE'."


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--expert", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--text")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--out", help="write suggestion JSON to this path")
    args = p.parse_args()

    bridge = _bridge.LLMBridge()
    if args.file:
        prompt = build_prompt_from_file(args.file)
    else:
        prompt = build_prompt_from_text(args.text)

    try:
        out = bridge.run_expert(args.expert, prompt, timeout=args.timeout)
    except Exception as exc:
        print(f"LLM run failed: {exc}", file=sys.stderr)
        return 2

    result = {"expert": args.expert, "output": out}
    s = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(s)
        print(args.out)
    else:
        print(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
