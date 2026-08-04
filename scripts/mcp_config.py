"""Central runtime configuration for the repository.

Provide a single place to read environment variables and define runtime
paths used by multiple entry scripts. This keeps code agnostic to local
user paths and centralizes defaults.

Usage: import mcp_config as cfg; cfg.OLLAMA_URL
"""
from __future__ import annotations

import os
from pathlib import Path

# Repo and user locations
REPO_ROOT = os.path.abspath(os.path.expanduser(os.getenv('REPO_ROOT', os.path.dirname(os.path.realpath(__file__)))))
MCP_HOME = os.path.expanduser(os.getenv('MCP_HOME', os.path.expanduser('~')))
HOME = MCP_HOME
AI_HOME = os.path.expanduser(os.getenv('AI_HOME', os.path.join(HOME, '.mcp-ai')))

# HAL data dirs
TRAIN_DIR = os.path.expanduser(os.getenv('HAL_TRAIN_DIR', os.path.join(AI_HOME, 'training')))
FIXES_DIR = os.path.expanduser(os.getenv('HAL_FIXES_DIR', os.path.join(AI_HOME, 'fixes')))
REPORTS_DIR = os.path.expanduser(os.getenv('HAL_REPORTS_DIR', os.path.join(AI_HOME, 'reports')))
CACHE_DIR = os.path.expanduser(os.getenv('HAL_CACHE_DIR', os.path.join(AI_HOME, 'cache', 'intel')))
ALIAS_FILE = os.path.expanduser(os.getenv('HAL_ALIAS_FILE', os.path.join(AI_HOME, 'account_aliases.json')))
APPROVED_PODMAN_IMAGES_FILE = os.path.expanduser(os.getenv('APPROVED_PODMAN_IMAGES_FILE', os.path.join(AI_HOME, 'approved-podman-images.json')))

# Bridge / model defaults
OLLAMA_URL = os.getenv('OLLAMA_URL', 'http://localhost:1776/api/chat')
MOE_DEFAULT_MODEL = os.getenv('MOE_DEFAULT_MODEL', 'qwen2.5-coder:7b')

# Bridge defaults
try:
    BRIDGE_PORT = int(os.getenv('BRIDGE_PORT', '1776'))
except Exception:
    BRIDGE_PORT = 1776

# Metrics / pushgateway
MOE_PUSHGATEWAY_URL = os.getenv('MOE_PUSHGATEWAY_URL', os.getenv('MOE_PUSHGATEWAY', ''))

# Privileged flow
PRIVILEGED_ALLOWLIST_PATH = os.path.expanduser(os.getenv('PRIVILEGED_ALLOWLIST_PATH', os.path.join(AI_HOME, 'privileged_allowlist.json')))

# Ansible env and vault defaults
# NOTE: This repository should NOT commit real env.yml files containing secrets.
ANSIBLE_ENV_PATH = os.path.expanduser(os.getenv('ANSIBLE_ENV_PATH', os.path.join(HOME, '.ansible', 'conf', 'env.yml')))
ANSIBLE_VAULT_PASSWORD_FILE = os.path.expanduser(os.getenv('ANSIBLE_VAULT_PASSWORD_FILE', os.path.join(HOME, '.ansible', 'conf', '.vaultpass.txt')))


def get(key: str, default=None):
    """Return a configuration value from module globals or environment."""
    return globals().get(key, os.getenv(key, default))


def ensure_dirs():
    """Create important directories used by HAL if they don't exist yet."""
    for p in (AI_HOME, TRAIN_DIR, FIXES_DIR, REPORTS_DIR, CACHE_DIR):
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
