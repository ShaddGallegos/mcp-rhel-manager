#!/usr/bin/env python3
"""Invoke a local llama.cpp model via the `llama-cpp-python` binding.

This helper reads a prompt file and returns the model output to stdout.
It requires `llama-cpp-python` to be installed in the Python environment used
to run the script (pip install llama-cpp-python). If the binding is not
available the script exits with a helpful message.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True, help="Path to ggml model file")
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--max-tokens", type=int, default=256)
    args = p.parse_args()

    prompt_path = Path(args.prompt_file)
    if not prompt_path.exists():
        print(f"prompt file not found: {prompt_path}", file=sys.stderr)
        return 2

    prompt = prompt_path.read_text()

    try:
        from llama_cpp import Llama
    except Exception as exc:
        print("llama_cpp Python binding not available.", file=sys.stderr)
        print("Install with: pip install llama-cpp-python", file=sys.stderr)
        print("Or ensure a suitable runtime is available and update your expert mapping.", file=sys.stderr)
        return 3

    try:
        llm = Llama(model_path=args.model)
        # Call the model; the llama-cpp-python API returns a mapping with 'choices'
        out = llm(prompt, max_tokens=args.max_tokens)
        # extract text
        text = None
        if isinstance(out, dict) and 'choices' in out and len(out['choices']) > 0:
            text = out['choices'][0].get('text')
        else:
            try:
                text = out.choices[0].text
            except Exception:
                text = str(out)
        if text is None:
            text = ''
        print(text)
        return 0
    except Exception as exc:
        print(f"llama invocation failed: {exc}", file=sys.stderr)
        return 4


if __name__ == '__main__':
    raise SystemExit(main())
