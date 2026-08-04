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

try:
    import mcp_config as topcfg
except Exception:
    topcfg = None


def get_repo_root() -> str:
    # Prefer top-level repo root when available to keep behavior consistent
    if topcfg and getattr(topcfg, 'REPO_ROOT', None):
        return topcfg.REPO_ROOT
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
    paths.append(REPO_ROOT / 'config' / 'mcp-config.json')
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
    # Environment overrides everything. Normalize so callers can append
    # `/api/chat` safely (i.e. return the base URL without `/api/chat`).
    def _normalize_base(u: str) -> str:
        s = str(u or '').rstrip('/')
        if s.endswith('/api/chat'):
            s = s[: -len('/api/chat')]
        return s

    ev = os.environ.get('OLLAMA_URL')
    if ev is not None:
        return _normalize_base(ev)

    # Prefer central config when present.
    if topcfg and getattr(topcfg, 'OLLAMA_URL', None):
        return _normalize_base(topcfg.OLLAMA_URL)

    v = get_config('ollama_url', default)
    return _normalize_base(v)


def get_bridge_port(default: int = 1776) -> int:
    # Environment override
    ev = os.environ.get('BRIDGE_PORT')
    if ev is not None:
        try:
            return int(ev)
        except Exception:
            pass

    # Prefer central config when available
    if topcfg and getattr(topcfg, 'BRIDGE_PORT', None) is not None:
        try:
            return int(topcfg.BRIDGE_PORT)
        except Exception:
            pass

    v = get_config('bridge_port', default)
    try:
        return int(v)
    except Exception:
        return int(default)


def get_ai_user(default: str = 'mcp-ai') -> str:
    return get_config('ai_user', default)


def get_model_routing_map() -> Dict[str, str]:
    """Return task-to-model routing map.

    Precedence:
    1. `MCP_MODEL_ROUTING_JSON` env var (JSON object)
    2. `model_routing` object from config.json / mcp-config.json
    3. Built-in safe defaults
    """
    defaults: Dict[str, str] = {
        'default': os.environ.get('MCP_MODEL_DEFAULT', 'qwen2.5-coder:7b'),
        'fast': os.environ.get('MCP_MODEL_FAST', 'qwen2.5-coder:7b'),
        'diagnostics': os.environ.get('MCP_MODEL_DIAGNOSTICS', 'llama3.1:8b'),
        'analysis': os.environ.get('MCP_MODEL_ANALYSIS', 'llama3.1:8b'),
        'code': os.environ.get('MCP_MODEL_CODE', 'qwen2.5-coder:7b'),
        'reasoning': os.environ.get('MCP_MODEL_REASONING', 'llama3.1:8b'),
    }

    env_json = os.environ.get('MCP_MODEL_ROUTING_JSON', '').strip()
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, dict):
                merged = dict(defaults)
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        merged[k.strip().lower()] = v.strip()
                return merged
        except Exception:
            pass

    cfg_map = get_config('model_routing', None)
    if isinstance(cfg_map, dict):
        merged = dict(defaults)
        for k, v in cfg_map.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                merged[k.strip().lower()] = v.strip()
        return merged

    return defaults


def pick_model_for_task(task: str = 'default') -> str:
    """Pick model by task name, with fallback to `default`."""
    routing = get_model_routing_map()
    key = (task or 'default').strip().lower()
    if key in routing and routing[key]:
        return routing[key]
    return routing.get('default', os.environ.get('MCP_MODEL_DEFAULT', 'qwen2.5-coder:7b'))
