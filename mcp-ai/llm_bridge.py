"""Lightweight bridge for invoking local LLM runtimes per-expert.

Config is a JSON mapping of expert names to command templates. The bridge
doesn't hardcode runtimes; instead it executes the configured shell command
template with a {prompt_file} placeholder. This keeps it flexible for
ollama/llama.cpp/HTTP wrappers.

Search order for config:
 - environment variable `LLM_EXPERTS_CONFIG`
 - user config `~/.mcp-ai/llm_experts.json`
 - system config `/etc/mcp-ai/llm_experts.json`
 - packaged example `packaging/llm/llm_experts.example.json`

Example mapping entry:
  "code_fixer": {
      "cmd": "ollama run coder-model --prompt-file {prompt_file}"
  }

The command template must contain `{prompt_file}`. The bridge writes the
prompt to a temporary file and substitutes into the command template.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Any


def _default_paths() -> list[str]:
    paths = []
    env = os.environ.get("LLM_EXPERTS_CONFIG")
    if env:
        paths.append(env)
    paths.append(os.path.expanduser("~/.mcp-ai/llm_experts.json"))
    paths.append("/etc/mcp-ai/llm_experts.json")
    # packaged example (fallback)
    repo_root = Path(__file__).resolve().parents[1]
    paths.append(str(repo_root / "packaging" / "llm" / "llm_experts.example.json"))
    return paths


def load_experts() -> Dict[str, Any]:
    for p in _default_paths():
        try:
            if p and os.path.exists(p):
                with open(p, "r") as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        return data
        except Exception:
            continue
    return {}


class LLMBridge:
    def __init__(self, experts: Dict[str, Any] | None = None):
        self.experts = experts or load_experts()

    def list_experts(self) -> list[str]:
        return sorted(self.experts.keys())

    def run_expert(self, expert: str, prompt: str, timeout: int = 60) -> str:
        if expert not in self.experts:
            raise KeyError(f"Unknown expert: {expert}")
        entry = self.experts[expert]
        cmd_template = entry.get("cmd") or entry.get("command")
        if not cmd_template:
            raise ValueError(f"Expert {expert} has no 'cmd' template configured")

        # write prompt to temp file
        tmp = tempfile.NamedTemporaryFile("w", delete=False)
        try:
            tmp.write(prompt)
            tmp.flush()
            tmp.close()
            cmd = cmd_template.format(prompt_file=tmp.name, model=entry.get("model", ""))
            # run via shell to support complex CLI forms; user-provided template must be safe
            proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
            if proc.returncode != 0:
                raise RuntimeError(f"Command failed (rc={proc.returncode}): {proc.stderr.strip()}")
            return proc.stdout
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("expert")
    p.add_argument("--prompt-file", help="path to file containing prompt")
    p.add_argument("--timeout", type=int, default=60)
    args = p.parse_args()
    if not args.prompt_file or not os.path.exists(args.prompt_file):
        print("prompt file required", file=sys.stderr)
        sys.exit(2)
    with open(args.prompt_file) as fh:
        prompt = fh.read()
    b = LLMBridge()
    out = b.run_expert(args.expert, prompt, timeout=args.timeout)
    print(out)
