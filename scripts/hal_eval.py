#!/usr/bin/env python3
"""Simple CLI to run HAL evaluator over a prompts file.

Usage:
  scripts/hal_eval.py --prompts prompts.txt [--answers answers.txt]

Each line in prompts.txt is a prompt. If answers.txt is provided, each line
corresponds to the expected answer for the same prompt.
"""
import argparse
import json
from pathlib import Path

import importlib
import scripts.hal as hal
importlib.reload(hal)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--prompts', required=True)
    p.add_argument('--answers')
    p.add_argument('--max', type=int, default=10)
    p.add_argument('--keywords', help='Comma-separated keywords to look for')
    p.add_argument('--dry', action='store_true', help='Run evaluator in dry mode with fake bridge responses')
    args = p.parse_args()

    prompts = [l.rstrip('\n') for l in Path(args.prompts).read_text(encoding='utf-8').splitlines() if l.strip()]
    expected_answers = None
    if args.answers:
        expected_answers = [l.rstrip('\n') for l in Path(args.answers).read_text(encoding='utf-8').splitlines()]

    keywords = [k.strip() for k in (args.keywords or '').split(',') if k.strip()]

    if args.dry:
        # Inject a deterministic fake bridge to avoid external dependencies in CI
        def _fake_bridge(prompt, **kwargs):
            return f"DRY_ANSWER: contains KEYWORD — for prompt: {prompt}"
        hal.call_bridge = _fake_bridge

    summary = hal.evaluate_prompts(prompts, expected_keywords=keywords or None, expected_answers=expected_answers, max_examples=args.max)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
