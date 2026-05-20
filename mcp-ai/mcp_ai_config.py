#!/usr/bin/env python3
"""Centralized configuration helpers for mcp-ai modules.

Provide small utilities to locate the repository, venv python, and
load a simple per-host JSON config written by the installer. This keeps
defaults in one place and reduces duplication across scripts.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]


def get_repo_root() -> str:
    return str(REPO_ROOT)


def get_venv_python() -> str:
    """Return the best candidate python executable from the repo venv.

    Checks for `.venv/bin/python`, `venv/bin/python`, then falls back to
    the current interpreter.
    """
    candidates = [
        REPO_ROOT / '.venv' / 'bin' / 'python',
        REPO_ROOT / 'venv' / 'bin' / 'python',
        Path(sys.executable),
    ]
    for p in candidates:
        try:
            if p and p.exists():
                return str(p)
        except Exception:
            continue
    return sys.executable


def _load_user_config() -> Dict[str, Any]:
    paths = []
    if os.environ.get('MCP_CONFIG'):
        paths.append(Path(os.environ.get('MCP_CONFIG')))
    paths.append(Path('/var/lib/mcp/.mcp-ai/config.json'))
    paths.append(REPO_ROOT / 'mcp-config.json')
    for p in paths:
        try:
            if p and p.exists():
                return json.loads(p.read_text())
        except Exception:
            continue
    return {}


# cached
_USER_CONFIG: Dict[str, Any] = _load_user_config()


def get_config(key: str, default: Optional[Any] = None) -> Any:
    # env vars take precedence
    ev = os.environ.get(key.upper())
    if ev is not None:
        return ev
    return _USER_CONFIG.get(key, default)


def get_ollama_url(default: str = 'http://localhost:11434') -> str:
    """Return base Ollama URL (no trailing slash)."""
    v = get_config('ollama_url', default)
    return str(v).rstrip('/')


def get_bridge_port(default: int = 1776) -> int:
    v = get_config('bridge_port', default)
    try:
        return int(v)
    except Exception:
        return int(default)


def get_ai_user(default: str = 'mcp-ai') -> str:
    return get_config('ai_user', default)
