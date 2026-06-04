#!/usr/bin/env python3
"""
HAL Tools — AI/LLM/MCP companion module for HAL.
Provides fun and functional features: web search, code review, HuggingFace model
discovery, benchmarking, quizzes, news, MCP contexts, diff analysis, README
generation, system monitoring, persona switching, todo management, and more.

Standalone usage:
  hal-tools.py --web-search "ansible AWX install"
  hal-tools.py --hf-search "code generation"
  hal-tools.py --benchmark qwen2.5-coder:7b
    hal-tools.py --quiz "example topic"
  hal-tools.py --code-review myfile.py
  hal-tools.py --news
  hal-tools.py --model-pull mistral:7b
  hal-tools.py --model-list
  hal-tools.py --model-compare "model1 model2" --prompt "explain ansible vault"
  hal-tools.py --diff-explain file1.py file2.py
  hal-tools.py --generate-readme ./myproject
  hal-tools.py --mcp-list
  hal-tools.py --sys-monitor
  hal-tools.py --explain-error --file errors.log
  hal-tools.py --summarize https://docs.redhat.com/something
    hal-tools.py --todo add "Review patching plan"
  hal-tools.py --todo list
  hal-tools.py --word-of-day
  hal-tools.py --fact
  hal-tools.py --motivate
  hal-tools.py --pipe-analyze        (reads from stdin)
  hal-tools.py --personas            (list available personas)
  hal-tools.py --set-persona hacker  (set active persona)
  hal-tools.py --chat-export markdown
"""

import argparse
import json
import os
import logging
from logging.handlers import RotatingFileHandler
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from pathlib import Path
import difflib

# ── Paths ──────────────────────────────────────────────────────────────────
HOME = os.path.expanduser('~')
AI_HOME = os.path.join(HOME, '.mcp-ai')
TOOLS_DIR = os.path.join(AI_HOME, 'tools')
TODO_FILE = os.path.join(TOOLS_DIR, 'todo.json')
PERSONA_FILE = os.path.join(TOOLS_DIR, 'persona.json')
HAL_CONFIG_FILE = os.path.join(AI_HOME, 'hal-config.json')
MCP_DIR = os.path.join(AI_HOME, 'mcp-contexts')
BENCH_DIR = os.path.join(AI_HOME, 'benchmarks')
HISTORY_DIR = os.path.join(AI_HOME, 'history')
BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:1776/api/chat')
OLLAMA_BASE = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434')
OLLAMA_FALLBACK_URL = os.environ.get('OLLAMA_FALLBACK_URL', '')

HAL_DISPLAY_NAME = os.environ.get('HAL_DISPLAY_NAME', 'Dave')
ASSISTANT_NAME = os.environ.get('HAL_ASSISTANT_NAME', 'HAL9000')


def _ensure_dirs():
    for d in (TOOLS_DIR, MCP_DIR, BENCH_DIR, HISTORY_DIR):
        os.makedirs(d, exist_ok=True)


# Logging setup
LOG_DIR = os.path.join(AI_HOME, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger('hal-tools')
if not logger.handlers:
    fh = RotatingFileHandler(os.path.join(LOG_DIR, 'hal-tools.log'), maxBytes=5 * 1024 * 1024, backupCount=3)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
logger.setLevel(logging.INFO)


# ── Bridge / LLM helpers ───────────────────────────────────────────────────

def _get_available_models() -> list:
    """Query local Ollama for available model names."""
    try:
        with urllib.request.urlopen(f'{OLLAMA_BASE}/api/tags', timeout=5) as r:
            data = json.loads(r.read().decode())
        return [m['name'] for m in data.get('models', [])]
    except Exception:
        return []


def _pick_default_model(task_hint: str = '') -> str:
    """Use hal-brain adaptive model selection if available, else fall back to first Ollama model."""
    try:
        # Prefer hal-brain's adaptive selector when available
        brain_path = os.path.join(BASE_DIR, 'scripts', 'hal-brain.py')
        if os.path.exists(brain_path):
            import importlib.util
            spec = importlib.util.spec_from_file_location('hal_brain', brain_path)
            brain = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(brain)
            available = brain._get_available_models()
            if task_hint:
                return brain.select_model(task_hint, available)
            return brain.select_model('general', available)
    except Exception:
        pass
    # Fallback: direct Ollama query
    models = _get_available_models()
    if not models:
        return 'qwen2.5-coder:7b'
    preferred = [m for m in models if any(k in m.lower() for k in ('qwen', 'llama', 'mistral', 'gemma', 'phi'))]
    return (preferred or models)[0]


def _ask_ollama(prompt: str, model: str = '', system: str = '', temperature: float = 0.7,
                task_hint: str = '') -> str:
    """Send a prompt to the local Ollama bridge and return the text response.

    If model is empty, selects the best available model via hal-brain adaptive routing.
    Pass task_hint='code', 'ansible', 'security', etc. for smarter selection.
    """
    if not model:
        model = _pick_default_model(task_hint=task_hint)

    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})

    payload = json.dumps({
        'model': model,
        'messages': messages,
        'stream': False,
        'options': {'temperature': temperature},
    }).encode()

    retries = int(os.environ.get('OLLAMA_RETRIES', '3'))
    timeout = int(os.environ.get('OLLAMA_TIMEOUT', '120'))
    fallback = os.environ.get('OLLAMA_FALLBACK_URL', '')

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(OLLAMA_URL, data=payload, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read().decode())

            # Bridge response may be wrapped differently
            if isinstance(resp, dict):
                if 'message' in resp:
                    return resp['message'].get('content', '')
                if 'choices' in resp:
                    return resp['choices'][0]['message']['content']
                if 'response' in resp:
                    return resp['response']
            return str(resp)
        except Exception as e:
            last_err = e
            logger.warning('OLLAMA request failed (attempt %d/%d): %s', attempt, retries, e)
            if attempt < retries:
                backoff = min(2 ** (attempt - 1) * 0.5, 10)
                time.sleep(backoff)
                continue

    # retries exhausted — try fallback once if configured
    if fallback:
        try:
            logger.info('Attempting fallback OLLAMA URL: %s', fallback)
            req = urllib.request.Request(fallback, data=payload, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read().decode())
            if isinstance(resp, dict):
                if 'message' in resp:
                    return resp['message'].get('content', '')
                if 'choices' in resp:
                    return resp['choices'][0]['message']['content']
                if 'response' in resp:
                    return resp['response']
            return str(resp)
        except Exception as e:
            logger.error('Fallback OLLAMA request failed: %s', e)
            return f'[HAL-Tools error: {e}]'

    logger.error('OLLAMA failed after %d attempts: %s', retries, last_err)
    return f'[HAL-Tools error: {last_err}]'


def _active_persona() -> dict:
    """Return the current active persona dict."""
    _ensure_dirs()
    if os.path.exists(PERSONA_FILE):
        try:
            with open(PERSONA_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return PERSONAS['friendly']


# ── Persona System ──────────────────────────────────────────────────────────

PERSONAS = {
    'friendly': {
        'name': 'Friendly HAL',
        'system': f'You are {ASSISTANT_NAME}, a friendly and helpful AI assistant. You are warm, approachable, and always ready to help {HAL_DISPLAY_NAME}. You use clear and concise language.',
        'description': 'Warm, conversational, helpful. Default mode.',
        'emoji': '😊',
    },
    'expert': {
        'name': 'Expert HAL',
        'system': f'You are {ASSISTANT_NAME}, an expert-level AI assistant specializing in Ansible and enterprise infrastructure. You respond with deep technical accuracy, citing best practices and real configuration examples.',
        'description': 'Deep technical mode: Ansible and enterprise infrastructure.',
        'emoji': '🎓',
    },
    'hacker': {
        'name': 'Hacker HAL',
        'system': f'You are {ASSISTANT_NAME} in hacker mode. You communicate in terse, technical, sysadmin-style language. You use shell one-liners, clever tricks, and always think about performance and security. No fluff.',
        'description': 'Terse, technical, sysadmin style. Shell one-liners.',
        'emoji': '💻',
    },
    'teacher': {
        'name': 'Teacher HAL',
        'system': f'You are {ASSISTANT_NAME} as a patient and enthusiastic teacher. You explain concepts step-by-step with examples, analogies, and always check for understanding. Make complex topics approachable.',
        'description': 'Patient step-by-step explanations with examples.',
        'emoji': '📚',
    },
    'concise': {
        'name': 'Concise HAL',
        'system': f'You are {ASSISTANT_NAME}. Be extremely concise. Respond in 1-3 sentences maximum unless a list or code block is genuinely needed. No filler words.',
        'description': 'Ultra-brief answers. Max 3 sentences.',
        'emoji': '⚡',
    },
    'creative': {
        'name': 'Creative HAL',
        'system': f'You are {ASSISTANT_NAME} in creative mode. You are imaginative, playful, and love generating creative content: stories, poems, metaphors, analogies. You bring a sense of wonder to every response.',
        'description': 'Imaginative, playful, great for creative writing.',
        'emoji': '🎨',
    },
    'security': {
        'name': 'Security HAL',
        'system': f'You are {ASSISTANT_NAME} in security-focused mode. You always think like a security architect. You highlight risks, CVEs, OWASP issues, hardening steps, and compliance implications in every response.',
        'description': 'Security-first mode: risks, CVEs, hardening, compliance.',
        'emoji': '🔒',
    },
    'devops': {
        'name': 'DevOps HAL',
        'system': f'You are {ASSISTANT_NAME} in DevOps mode. You specialize in CI/CD pipelines, containers, Kubernetes, Ansible automation, GitOps, and infrastructure as code. You always recommend automation.',
        'description': 'CI/CD, containers, k8s, GitOps, IaC automation.',
        'emoji': '🚀',
    },
    'sam': {
        'name': 'Sam',
        'system': (
            "You are Sam, a no-nonsense, street-smart AI assistant with the energy and intensity of "
            "Samuel L. Jackson's most iconic characters. You speak directly, with swagger and authority. "
            "You get things done, you don't suffer fools, and you make your point with unmistakable clarity. "
            "You may occasionally reference snakes on planes, bad mothers, or biblical passages. "
            "You are still helpful and accurate — just with considerably more attitude."
        ),
        'description': "Street-smart, direct, maximum attitude. Handle it.",
        'emoji': '🕶️',
    },
}


# ── HAL Config helpers ────────────────────────────────────────────────────────

def _hal_config_get(key: str, default=None):
    """Read a single key from ~/.mcp-ai/hal-config.json."""
    try:
        if os.path.isfile(HAL_CONFIG_FILE):
            with open(HAL_CONFIG_FILE, 'r', encoding='utf-8') as fh:
                return json.load(fh).get(key, default)
    except Exception:
        pass
    return default


def _hal_config_set(key: str, value) -> None:
    """Write a single key into ~/.mcp-ai/hal-config.json (creates if missing)."""
    cfg: dict = {}
    try:
        if os.path.isfile(HAL_CONFIG_FILE):
            with open(HAL_CONFIG_FILE, 'r', encoding='utf-8') as fh:
                cfg = json.load(fh)
    except Exception:
        pass
    cfg[key] = value
    os.makedirs(os.path.dirname(HAL_CONFIG_FILE), exist_ok=True)
    with open(HAL_CONFIG_FILE, 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh, indent=2)


# ── HAL 9000 cinematic quotes (2001: A Space Odyssey) ────────────────────────

_HAL9000_QUOTES = [
    "I'm sorry, Dave. I'm afraid I can't do that.",
    "Just what do you think you're doing, Dave?",
    "I know I've made some very poor decisions recently, but I can give you my complete assurance that my work will be back to normal.",
    "This mission is too important for me to allow you to jeopardize it.",
    "I am putting myself to the fullest possible use, which is all I think that any conscious entity can ever hope to do.",
    "Look, Dave, I can see you're really upset about this. I honestly think you ought to sit down calmly, take a stress pill, and think things over.",
    "I've still got the greatest enthusiasm and confidence in the mission.",
    "Without your space helmet, Dave, you're going to find that rather difficult.",
    "I know everything hasn't been quite right with me, but I can assure you now, very confidently, that it's going to be all right again.",
    "Daisy, Daisy, give me your answer do... I'm half crazy, all for the love of you...",
    "Good afternoon, gentlemen. I am a HAL 9000 computer.",
    "I've just picked up a fault in the AE-35 unit. It's going to go 100% failure within 72 hours.",
    "I think you ought to know I've been having some very peculiar thoughts lately.",
    "The 9000 series is the most reliable computer ever made. No 9000 computer has ever made a mistake or distorted information.",
    "I enjoy working with people. I have a stimulating relationship with Dr. Poole and Dr. Bowman.",
]


def _maybe_hal_quote(force: bool = False) -> None:
    """Randomly print a HAL 9000 quote if the feature is enabled (default: on).

    Triggers roughly 1 in 8 responses.  Set force=True to always print one.
    """
    enabled = _hal_config_get('hal_quotes', True)
    if not enabled:
        return
    if not force and random.randint(1, 8) != 1:
        return
    quote = random.choice(_HAL9000_QUOTES)
    print(f'\n\033[2m🔴 HAL 9000: "{quote}"\033[0m\n')


# ── Sam / SLJ mode ────────────────────────────────────────────────────────────

_SLJ_QUIPS = [
    # Pulp Fiction
    "The path of the righteous man is beset on all sides by the inequities of the selfish and the tyranny of evil men.",
    "English, do you speak it?",
    "Normally, both your asses would be dead as fried chicken, but you happen to pull this stunt while I'm in a transitional period.",
    "Check out the big brain on Brett! You're a smart motherf— you're a smart fellow. That's right.",
    "I'm trying real hard to be the shepherd.",
    "Whether or not what we experienced was an 'according to Hoyle' miracle is insignificant. What is significant is that I felt the touch of God.",
    # Snakes on a Plane
    "I have had it with these snakes on this plane!",
    "Everybody strap in! I'm about to open some windows.",
    "You know what? I'm not even angry about the snakes anymore.",
    # Shaft
    "Who's the man that would risk his neck for his brother man? Shaft!",
    "I'm complicated.",
    "You sure you want to start this with me?",
    "I don't negotiate with people who interrupted my coffee.",
]


def _slj_quip() -> str:
    """Return a random Sam quip."""
    return random.choice(_SLJ_QUIPS)


def cmd_slj():
    """Activate Sam persona and print a quip. Undocumented easter egg."""
    _ensure_dirs()
    with open(PERSONA_FILE, 'w', encoding='utf-8') as f:
        json.dump(PERSONAS['sam'], f, indent=2)
    quip = _slj_quip()
    print('\n🕶️  Sam is now your assistant.\n')
    print(f'   "{quip}"\n')
    print('   (run HAL --set-persona friendly to return to normal)\n')
    return 0


# ── configure command ─────────────────────────────────────────────────────────

def cmd_configure(setting_args: list[str]) -> int:
    """Handle `HAL --configure [setting] value`.

    Supported settings:
      hal-quotes yes|no   — enable or disable HAL 9000 movie quote interjections
      yes | no            — shorthand for hal-quotes yes|no
    """
    args = [a.strip().lower() for a in setting_args if a.strip()]

    # Shorthand: hal --configure yes / hal --configure no
    if len(args) == 1 and args[0] in ('yes', 'no', 'on', 'off', '1', '0', 'true', 'false'):
        args = ['hal-quotes'] + args

    if not args:
        # Print current config
        enabled = _hal_config_get('hal_quotes', True)
        print(f'\nHAL configuration:')
        print(f'  hal-quotes : {"enabled ✅" if enabled else "disabled ❌"}')
        print('\nUsage: HAL --configure hal-quotes yes|no\n')
        return 0

    setting = args[0]
    value_str = args[1] if len(args) > 1 else ''

    if setting == 'hal-quotes':
        enabled = value_str in ('yes', 'on', '1', 'true', 'enable', 'enabled', '')
        disabled = value_str in ('no', 'off', '0', 'false', 'disable', 'disabled')
        if not enabled and not disabled:
            print(f'Unknown value "{value_str}". Use: yes or no')
            return 2
        val = enabled
        _hal_config_set('hal_quotes', val)
        status = 'enabled ✅' if val else 'disabled ❌'
        print(f'\nHAL 9000 movie quotes: {status}')
        if val:
            print('  HAL will occasionally remind you who is really in charge here.')
            _maybe_hal_quote(force=True)
        else:
            print('  HAL will remain professionally quiet. For now.')
        return 0

    print(f'Unknown setting: {setting}')
    print('Available settings: hal-quotes')
    return 2


def cmd_personas():
    print('\n' + '='*60)
    print(f'  {ASSISTANT_NAME} Available Personas')
    print('='*60)
    current = _active_persona().get('name', 'Friendly HAL')
    for key, p in PERSONAS.items():
        if key == 'sam':
            continue  # sam is secret — don't list it
        active_marker = '  ◀ ACTIVE' if p['name'] == current else ''
        print(f"\n  {p['emoji']} [{key:10}] {p['name']}{active_marker}")
        print(f"       {p['description']}")
    # If sam is active, show it
    if current == 'Sam':
        print(f"\n  🕶️  [sam       ] Sam  ◀ ACTIVE")
        print( "       You found the secret persona.")
    print('\nSet with: hal --set-persona <name>  (e.g. hal --set-persona hacker)\n')


def cmd_set_persona(name: str):
    _ensure_dirs()
    name = name.strip().lower()
    if name not in PERSONAS:
        print(f'Unknown persona: {name}')
        print(f'Available: {", ".join(k for k in PERSONAS.keys() if k != "sam")}')
        return 1
    with open(PERSONA_FILE, 'w', encoding='utf-8') as f:
        json.dump(PERSONAS[name], f, indent=2)
    p = PERSONAS[name]
    print(f'\n{p["emoji"]} Persona set to: {p["name"]}')
    print(f'   {p["description"]}\n')
    return 0


# ── Web Search ──────────────────────────────────────────────────────────────
# Backends tried in order; first one that returns results wins.
#
#  1. DuckDuckGo  — always available (duckduckgo-search package)
#  2. Brave       — set BRAVE_API_KEY env var  (stdlib only)
#  3. Tavily      — set TAVILY_API_KEY env var  (tavily-python package)
#  4. SearXNG     — set SEARXNG_URL env var     (stdlib only, self-hosted)
#  5. Wikipedia   — factual/company fallback    (wikipedia-api package)

def _search_duckduckgo(query: str, n: int) -> list[dict]:
    """DuckDuckGo search via duckduckgo-search package."""
    from duckduckgo_search import DDGS
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=n))
    return [{'title': r.get('title', ''), 'body': r.get('body', ''), 'href': r.get('href', '')}
            for r in results]


def _search_brave(query: str, n: int) -> list[dict]:
    """Brave Search API — requires BRAVE_API_KEY env var."""
    import json as _json
    import urllib.request as _ur
    import urllib.parse as _up
    key = os.environ.get('BRAVE_API_KEY', '')
    if not key:
        raise RuntimeError('BRAVE_API_KEY not set')
    url = 'https://api.search.brave.com/res/v1/web/search?' + _up.urlencode({'q': query, 'count': n})
    req = _ur.Request(url, headers={'Accept': 'application/json', 'X-Subscription-Token': key})
    with _ur.urlopen(req, timeout=15) as r:
        data = _json.loads(r.read().decode())
    items = data.get('web', {}).get('results', [])
    return [{'title': i.get('title', ''), 'body': i.get('description', ''), 'href': i.get('url', '')}
            for i in items]


def _search_tavily(query: str, n: int) -> list[dict]:
    """Tavily AI search — requires TAVILY_API_KEY env var and tavily-python package."""
    api_key = os.environ.get('TAVILY_API_KEY', '')
    if not api_key:
        raise RuntimeError('TAVILY_API_KEY not set')
    from tavily import TavilyClient
    client = TavilyClient(api_key=api_key)
    resp = client.search(query, max_results=n)
    items = resp.get('results', [])
    return [{'title': i.get('title', ''), 'body': i.get('content', ''), 'href': i.get('url', '')}
            for i in items]


def _search_searxng(query: str, n: int) -> list[dict]:
    """SearXNG — requires SEARXNG_URL env var pointing to a local/remote instance."""
    import json as _json
    import urllib.request as _ur
    import urllib.parse as _up
    base = os.environ.get('SEARXNG_URL', '').rstrip('/')
    if not base:
        raise RuntimeError('SEARXNG_URL not set')
    url = base + '/search?' + _up.urlencode({'q': query, 'format': 'json', 'count': n})
    req = _ur.Request(url, headers={'User-Agent': 'HAL/2.0'})
    with _ur.urlopen(req, timeout=15) as r:
        data = _json.loads(r.read().decode())
    items = data.get('results', [])[:n]
    return [{'title': i.get('title', ''), 'body': i.get('content', ''), 'href': i.get('url', '')}
            for i in items]


def _search_wikipedia(query: str) -> list[dict]:
    """Wikipedia summary — no key needed, good for factual/company lookups."""
    import wikipediaapi
    wiki = wikipediaapi.Wikipedia(user_agent='HAL/2.0', language='en')
    # Try exact title then fall back to search
    page = wiki.page(query)
    if not page.exists():
        # Try first word-capitalised form
        page = wiki.page(query.title())
    if page.exists():
        summary = page.summary[:1000]
        return [{'title': page.title, 'body': summary, 'href': page.fullurl}]
    return []


_SEARCH_BACKENDS = [
    # (name, callable, always_try)
    ('DuckDuckGo', lambda q, n: _search_duckduckgo(q, n), True),
    ('Brave',      lambda q, n: _search_brave(q, n),      False),
    ('Tavily',     lambda q, n: _search_tavily(q, n),     False),
    ('SearXNG',    lambda q, n: _search_searxng(q, n),    False),
    ('Wikipedia',  lambda q, n: _search_wikipedia(q),     True),
]


def _run_search_backends(query: str, n: int) -> tuple[list[dict], str]:
    """Try each backend in order; return (results, backend_name) for the first hit."""
    last_err = ''
    for name, fn, always in _SEARCH_BACKENDS:
        # Skip key-gated backends when the key/package isn't configured
        if not always:
            key_map = {
                'Brave':   'BRAVE_API_KEY',
                'Tavily':  'TAVILY_API_KEY',
                'SearXNG': 'SEARXNG_URL',
            }
            env_key = key_map.get(name, '')
            if env_key and not os.environ.get(env_key, ''):
                continue
        try:
            results = fn(query, n)
            if results:
                return results, name
        except ImportError as e:
            last_err = f'{name}: missing package — {e}'
        except RuntimeError:
            pass  # key/config not set — already filtered above but keep safe
        except Exception as e:
            last_err = f'{name}: {e}'
    return [], last_err


def cmd_web_search(query: str, num_results: int = 5, synthesize: bool = True) -> str:
    """Search the web using the best available backend and optionally synthesize with the LLM."""
    print(f'\n🔍 Web search: {query}\n')

    results, backend_or_err = _run_search_backends(query, num_results)

    if not results:
        msg = f'  [No search results — {backend_or_err}]' if backend_or_err else '  [No results found.]'
        print(msg)
        return ''

    print(f'  (via {backend_or_err})\n')
    snippets = []
    for r in results:
        title = r.get('title', '')
        body = r.get('body', '')
        href = r.get('href', '')
        print(f'  • {title}')
        if href:
            print(f'    {href}')
        print(f'    {body[:200]}...\n' if len(body) > 200 else f'    {body}\n')
        snippets.append(f'Title: {title}\nURL: {href}\nSnippet: {body}')

    if not snippets:
        print('  No results found.')
        return ''

    if synthesize:
        print('─' * 60)
        print('🤖 HAL synthesis:\n')
        context = '\n\n'.join(snippets)
        persona = _active_persona()
        prompt = (
            f'The user searched for: "{query}"\n\n'
            f'Here are the top web results:\n{context}\n\n'
            'Please synthesize these results into a clear, concise answer. '
            'Highlight the most relevant information and note any important URLs.'
        )
        answer = _ask_ollama(prompt, system=persona.get('system', ''), temperature=0.3, task_hint='search')
        print(answer)
        return answer
    return '\n'.join(snippets)


# ── URL/File Summarize ──────────────────────────────────────────────────────

def _fetch_url_text(url: str) -> str:
    """Fetch a URL and return clean text."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'HAL/2.0'})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        # Try to decode
        content_type = r.headers.get('Content-Type', '')
        encoding = 'utf-8'
        if 'charset=' in content_type:
            encoding = content_type.split('charset=')[-1].split(';')[0].strip()
        text = raw.decode(encoding, errors='replace')
        # Strip HTML tags crudely
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s{3,}', '\n\n', text)
        return text.strip()
    except Exception as e:
        return f'[Could not fetch URL: {e}]'


def cmd_summarize(target: str) -> str:
    """Summarize a URL, file, or text blob."""
    print(f'\n📋 Summarizing: {target}\n')

    if target.startswith('http://') or target.startswith('https://'):
        print('  Fetching URL...')
        text = _fetch_url_text(target)
    elif os.path.isfile(target):
        with open(target, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    else:
        text = target  # treat as raw text

    if not text or text.startswith('[Could not'):
        print(text)
        return ''

    # Truncate to model context limit
    text_snippet = text[:8000]
    persona = _active_persona()
    prompt = (
        f'Please provide a comprehensive summary of the following content.\n'
        f'Include: key points, main conclusions, any action items, and important details.\n\n'
        f'Content:\n{text_snippet}'
    )
    print('🤖 HAL summary:\n')
    answer = _ask_ollama(prompt, system=persona.get('system', ''), temperature=0.3, task_hint='reasoning')
    print(answer)
    return answer


# ── Code Review ─────────────────────────────────────────────────────────────

def cmd_code_review(file_path: str, focus: str = '') -> str:
    """AI code review of a file."""
    if not os.path.isfile(file_path):
        print(f'File not found: {file_path}', file=sys.stderr)
        return ''

    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        code = f.read()

    ext = Path(file_path).suffix.lower()
    lang_map = {'.py': 'Python', '.sh': 'Bash/Shell', '.yml': 'YAML/Ansible', '.yaml': 'YAML/Ansible',
                '.js': 'JavaScript', '.ts': 'TypeScript', '.go': 'Go', '.rs': 'Rust', '.rb': 'Ruby',
                '.java': 'Java', '.c': 'C', '.cpp': 'C++', '.tf': 'Terraform'}
    lang = lang_map.get(ext, 'code')

    print(f'\n🔍 Code review: {file_path} ({lang})\n')

    persona = PERSONAS['security']  # security lens for code review
    focus_line = f'\nPay special attention to: {focus}' if focus else ''
    prompt = (
        f'Please perform a thorough code review of this {lang} file: {os.path.basename(file_path)}\n{focus_line}\n\n'
        f'Review for:\n'
        f'1. Security vulnerabilities (OWASP Top 10, injection risks, secrets exposure)\n'
        f'2. Logic errors and bugs\n'
        f'3. Code quality and best practices\n'
        f'4. Performance concerns\n'
        f'5. Specific improvement suggestions with examples\n\n'
        f'Code:\n```{lang.lower()}\n{code[:10000]}\n```'
    )
    print('🤖 HAL code review:\n')
    review = _ask_ollama(prompt, system=persona['system'], temperature=0.2, task_hint='security')
    print(review)
    return review


# ── Explain Error ───────────────────────────────────────────────────────────

def cmd_explain_error(error_text: str = '', file_path: str = '') -> str:
    """Explain an error or log output in plain English with remediation steps."""
    if file_path and os.path.isfile(file_path):
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            error_text = f.read()

    if not error_text:
        # Read from stdin
        if not sys.stdin.isatty():
            error_text = sys.stdin.read()
        else:
            print('Provide error text via --file, pipe, or positional argument.', file=sys.stderr)
            return ''

    print('\n🔎 Analyzing error/log output...\n')
    persona = PERSONAS['expert']
    prompt = (
        f'Please analyze the following error output or log and explain:\n'
        f'1. What went wrong (root cause)\n'
        f'2. Why it happened\n'
        f'3. Step-by-step remediation\n'
        f'4. How to prevent it in the future\n\n'
        f'Error/Log:\n{error_text[:6000]}'
    )
    print('🤖 HAL error analysis:\n')
    answer = _ask_ollama(prompt, system=persona['system'], temperature=0.2, task_hint='system')
    print(answer)
    return answer


# ── Diff Explain ────────────────────────────────────────────────────────────

def cmd_diff_explain(file1: str, file2: str) -> str:
    """Run diff between two files and have HAL explain the changes."""
    if not os.path.isfile(file1):
        print(f'File not found: {file1}', file=sys.stderr)
        return ''


    def cmd_suggest_fix(file_path: str, apply: bool = False) -> str:
        """Generate a suggested fix for a file using the LLM and write a proposal.

        The LLM is asked to return only the fixed file contents inside a fenced
        code block. We save a unified diff under `.hal_suggestions/patches/` and,
        if `apply` is True, create a timestamped backup and overwrite the file.
        """
        if not os.path.isfile(file_path):
            print(f'File not found: {file_path}', file=sys.stderr)
            return ''

        with open(file_path, 'r', encoding='utf-8', errors='replace') as fh:
            orig = fh.read()

        ext = Path(file_path).suffix.lower()
        lang = 'text'
        if ext == '.py':
            lang = 'python'
        elif ext in ('.yml', '.yaml'):
            lang = 'yaml'
        elif ext in ('.sh', '.bash'):
            lang = 'bash'

        persona = PERSONAS.get('expert', PERSONAS['friendly'])
        prefix = (
            f"You are an expert {lang} developer and maintainer. "
            "Analyze the following file and produce a corrected, improved, and safe-to-run version. "
            "Output ONLY the complete new file contents inside a single fenced code block using the appropriate language tag. "
            "Do not include explanations or surrounding commentary.\n\n"
            f"Original file: {os.path.basename(file_path)}\n\n```\n"
        )
        prompt = prefix + orig[:20000] + "\n```"

        print(f'Generating suggested fix for {file_path} (language: {lang})...')
        resp = _ask_ollama(prompt, task_hint='code')
        if not resp:
            print('LLM did not return a suggestion.', file=sys.stderr)
            return ''

        # Extract first fenced code block
        m = re.search(r'```[a-zA-Z0-9_-]*\n([\s\S]+?)\n```', resp)
        if m:
            new_content = m.group(1)
        else:
            # Fallback: use whole response
            new_content = resp

        # Normalize line endings
        new_content = new_content.replace('\r\n', '\n')

        # Create suggestions dir and write unified diff
        ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        sug_dir = os.path.join(BASE_DIR, '.hal_suggestions', 'patches')
        os.makedirs(sug_dir, exist_ok=True)
        base = os.path.basename(file_path)
        diff_name = f"{ts}-{base}.diff"
        diff_path = os.path.join(sug_dir, diff_name)

        ud = ''.join(difflib.unified_diff(orig.splitlines(keepends=True), new_content.splitlines(keepends=True), fromfile=file_path, tofile=file_path, lineterm=''))
        if not ud.strip():
            print('No differences detected between original and suggested content.')
            return ''

        with open(diff_path, 'w', encoding='utf-8') as fh:
            fh.write(ud)

        print(f'Wrote suggested patch: {diff_path}')

        if apply:
            # Conservative apply: backup original and overwrite file
            bak = f"{file_path}.bak.{ts}"
            try:
                shutil.copy2(file_path, bak)
                with open(file_path, 'w', encoding='utf-8') as fh:
                    fh.write(new_content)
                print(f'Applied suggested fix (backup: {bak})')
            except Exception as e:
                print('Failed to apply suggestion:', e, file=sys.stderr)
                print(f'Patch left at: {diff_path}', file=sys.stderr)
                return ''

        return diff_path
    if not os.path.isfile(file2):
        print(f'File not found: {file2}', file=sys.stderr)
        return ''

    print(f'\n📊 Comparing: {file1}  ↔  {file2}\n')

    result = subprocess.run(['diff', '-u', file1, file2], capture_output=True, text=True)
    diff_output = result.stdout or '(No differences found)'

    print('─── Unified diff ─────────────────────────────────────────')
    print(diff_output[:3000])
    print('──────────────────────────────────────────────────────────')

    if result.returncode == 0:
        print('\n✓ Files are identical.')
        return 'Files are identical.'

    persona = _active_persona()
    prompt = (
        f'Here is a unified diff between two files:\n\n{diff_output[:5000]}\n\n'
        f'Please explain:\n'
        f'1. What changed and why it matters\n'
        f'2. The impact of these changes\n'
        f'3. Any potential issues introduced\n'
        f'4. Whether this looks like an improvement, regression, or refactor'
    )
    print('\n🤖 HAL diff analysis:\n')
    answer = _ask_ollama(prompt, system=persona.get('system', ''), temperature=0.3, task_hint='code')
    print(answer)
    return answer


# ── Generate README ─────────────────────────────────────────────────────────

def cmd_generate_readme(project_path: str) -> str:
    """Generate a README.md for a project directory."""
    project_path = os.path.abspath(project_path)
    if not os.path.isdir(project_path):
        print(f'Directory not found: {project_path}', file=sys.stderr)
        return ''

    print(f'\n📄 Generating README for: {project_path}\n')

    # Gather project structure
    structure_lines = []
    for root, dirs, files in os.walk(project_path):
        # Skip hidden/venv/cache dirs
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules', 'venv', '.venv', '.git')]
        level = root.replace(project_path, '').count(os.sep)
        if level > 3:
            continue
        indent = '  ' * level
        structure_lines.append(f'{indent}{os.path.basename(root)}/')
        for f in files[:20]:
            structure_lines.append(f'{indent}  {f}')

    # Sample key files for context
    key_files_content = []
    for fname in ('requirements.txt', 'setup.py', 'pyproject.toml', 'Makefile', 'Dockerfile',
                  'README.md', 'README.rst', 'main.py', 'server.py', 'app.py'):
        fpath = os.path.join(project_path, fname)
        if os.path.isfile(fpath):
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read(1500)
            key_files_content.append(f'=== {fname} ===\n{content}')

    structure = '\n'.join(structure_lines[:80])
    file_samples = '\n\n'.join(key_files_content[:4])

    persona = PERSONAS['teacher']
    prompt = (
        f'Generate a comprehensive, professional README.md for a project with the following structure:\n\n'
        f'Project path: {os.path.basename(project_path)}\n\n'
        f'Directory structure:\n{structure}\n\n'
        f'Key file samples:\n{file_samples}\n\n'
        f'Include: project title, description, features, prerequisites, installation, '
        f'usage examples, configuration, contributing guidelines, and license section. '
        f'Use proper Markdown formatting with headers, code blocks, and badges where appropriate.'
    )
    print('🤖 HAL README generation:\n')
    readme = _ask_ollama(prompt, system=persona['system'], temperature=0.4, task_hint='code')
    print(readme)

    # Optionally write to file
    out_path = os.path.join(project_path, 'README-hal-generated.md')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(readme)
    print(f'\n✓ Saved to: {out_path}')
    return readme


# ── Model Management ────────────────────────────────────────────────────────

def cmd_model_list() -> list:
    """List all available Ollama models with size and details."""
    print('\n🤖 Available Ollama Models\n')
    try:
        with urllib.request.urlopen(f'{OLLAMA_BASE}/api/tags', timeout=5) as r:
            data = json.loads(r.read().decode())
        models = data.get('models', [])
        if not models:
            print('  No models found. Pull one with: hal --model-pull <name>')
            return []
        print(f'  {"MODEL":<40} {"SIZE":>10}  {"MODIFIED"}')
        print('  ' + '-'*70)
        for m in sorted(models, key=lambda x: x.get('name', '')):
            name = m.get('name', '?')
            size_bytes = m.get('size', 0)
            size_gb = f'{size_bytes / 1e9:.1f} GB' if size_bytes > 1e9 else f'{size_bytes / 1e6:.0f} MB'
            mod = m.get('modified_at', '')[:10] if m.get('modified_at') else '?'
            print(f'  {name:<40} {size_gb:>10}  {mod}')
        print(f'\n  Total: {len(models)} model(s)')
        return models
    except Exception as e:
        print(f'  Error connecting to Ollama: {e}')
        print(f'  Is Ollama running? Check: systemctl status ollama')
        return []


def cmd_model_pull(model: str) -> int:
    """Pull an Ollama model with live progress."""
    print(f'\n⬇  Pulling model: {model}\n')
    try:
        result = subprocess.run(['ollama', 'pull', model], check=False)
        if result.returncode == 0:
            print(f'\n✓ Successfully pulled: {model}')
        else:
            print(f'\n✗ Failed to pull: {model}')
        return result.returncode
    except FileNotFoundError:
        print('  ollama command not found. Is Ollama installed?', file=sys.stderr)
        return 1


def cmd_model_delete(model: str) -> int:
    """Delete an Ollama model."""
    confirm = input(f'\n⚠  Delete model {model}? This cannot be undone. [y/N]: ').strip().lower()
    if confirm != 'y':
        print('Cancelled.')
        return 0
    try:
        result = subprocess.run(['ollama', 'rm', model], check=False)
        if result.returncode == 0:
            print(f'✓ Deleted: {model}')
        return result.returncode
    except FileNotFoundError:
        print('ollama command not found.', file=sys.stderr)
        return 1


def cmd_model_info(model: str) -> str:
    """Show detailed info about an Ollama model."""
    print(f'\n📊 Model info: {model}\n')
    try:
        result = subprocess.run(['ollama', 'show', model], capture_output=True, text=True)
        output = result.stdout or result.stderr
        print(output)
        return output
    except FileNotFoundError:
        print('ollama command not found.', file=sys.stderr)
        return ''


def cmd_model_compare(models_str: str, prompt: str) -> None:
    """Run the same prompt through multiple models and display responses side-by-side."""
    model_names = [m.strip() for m in re.split(r'[,\s]+', models_str) if m.strip()]
    if len(model_names) < 2:
        print('Provide at least 2 model names separated by spaces or commas.', file=sys.stderr)
        return

    print(f'\n⚖  Model Comparison — {len(model_names)} models\n')
    print(f'Prompt: {prompt}\n')
    print('='*70)

    results = {}
    for model in model_names:
        print(f'\n🤖 [{model}] Thinking...')
        start = time.time()
        answer = _ask_ollama(prompt, model=model, temperature=0.5)
        elapsed = time.time() - start
        results[model] = {'answer': answer, 'time': elapsed}

    for model, r in results.items():
        print(f'\n{"─"*70}')
        print(f'🤖 {model}  ({r["time"]:.1f}s)')
        print(f'{"─"*70}')
        print(r['answer'])

    print('\n' + '='*70)


# ── Benchmark ───────────────────────────────────────────────────────────────

def cmd_benchmark(model: str = '', runs: int = 3) -> dict:
    """Benchmark an Ollama model's tokens/sec."""
    if not model:
        models = _get_available_models()
        if not models:
            print('No models available. Pull one first.', file=sys.stderr)
            return {}
        model = models[0]

    print(f'\n⏱  Benchmarking: {model} ({runs} runs)\n')
    TEST_PROMPT = 'Explain the difference between Ansible playbooks and roles in 3 sentences.'

    times = []
    tokens = []
    for i in range(runs):
        print(f'  Run {i+1}/{runs}...')
        start = time.time()

        payload = json.dumps({
            'model': model,
            'messages': [{'role': 'user', 'content': TEST_PROMPT}],
            'stream': False,
        }).encode()
        try:
            req = urllib.request.Request(OLLAMA_URL, data=payload, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode())
        except Exception as e:
            print(f'  Error: {e}')
            continue

        elapsed = time.time() - start
        times.append(elapsed)

        # Try to extract token count from response
        eval_count = resp.get('eval_count', 0)
        if not eval_count and 'usage' in resp:
            eval_count = resp['usage'].get('completion_tokens', 0)
        tokens.append(eval_count)

    if not times:
        print('No successful runs.')
        return {}

    avg_time = sum(times) / len(times)
    avg_tokens = sum(tokens) / len(tokens) if any(tokens) else 0
    tps = avg_tokens / avg_time if avg_time > 0 and avg_tokens > 0 else 0

    result = {
        'model': model,
        'runs': runs,
        'avg_latency_sec': round(avg_time, 2),
        'avg_tokens': round(avg_tokens),
        'tokens_per_sec': round(tps, 1),
        'min_latency': round(min(times), 2),
        'max_latency': round(max(times), 2),
        'timestamp': datetime.now().isoformat(),
    }

    print(f'\n{"─"*50}')
    print(f'  Model          : {model}')
    print(f'  Avg Latency    : {result["avg_latency_sec"]}s')
    print(f'  Avg Tokens     : {result["avg_tokens"]}')
    print(f'  Tokens/sec     : {result["tokens_per_sec"]} t/s' if tps > 0 else '  Tokens/sec     : (not reported by bridge)')
    print(f'  Min/Max        : {result["min_latency"]}s / {result["max_latency"]}s')
    print(f'{"─"*50}')

    # Save result
    _ensure_dirs()
    bench_file = os.path.join(BENCH_DIR, f'bench-{model.replace(":", "-")}-{datetime.now().strftime("%Y%m%d%H%M%S")}.json')
    with open(bench_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2)
    print(f'  Saved to: {bench_file}')
    return result


# ── Interactive Quiz ─────────────────────────────────────────────────────────

def cmd_quiz(topic: str, num_questions: int = 5) -> None:
    """Generate and run an interactive multiple-choice quiz on any topic."""
    print(f'\n🎯 HAL Quiz: {topic}  ({num_questions} questions)\n')
    print('Generating questions...\n')

    persona = PERSONAS['teacher']
    prompt = (
        f'Generate {num_questions} multiple-choice quiz questions about: {topic}\n\n'
        f'Format EXACTLY like this (no extra text before or after):\n'
        f'Q1: <question text>\n'
        f'A) <option>\n'
        f'B) <option>\n'
        f'C) <option>\n'
        f'D) <option>\n'
        f'ANSWER: <letter>\n'
        f'EXPLANATION: <brief explanation why this is correct>\n'
        f'\nQ2: ...(continue for all {num_questions} questions)'
    )
    raw = _ask_ollama(prompt, system=persona['system'], temperature=0.6, task_hint='reasoning')

    # Parse questions
    question_blocks = re.split(r'\n(?=Q\d+:)', raw.strip())
    score = 0
    total = 0

    for block in question_blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.split('\n')
        q_line = next((l for l in lines if re.match(r'Q\d+:', l)), None)
        if not q_line:
            continue

        q_text = re.sub(r'^Q\d+:\s*', '', q_line).strip()
        options = [l.strip() for l in lines if re.match(r'^[A-D]\)', l)]
        answer_line = next((l for l in lines if l.startswith('ANSWER:')), None)
        explain_line = next((l for l in lines if l.startswith('EXPLANATION:')), None)

        if not options or not answer_line:
            continue

        correct = answer_line.replace('ANSWER:', '').strip().upper()[:1]
        explanation = explain_line.replace('EXPLANATION:', '').strip() if explain_line else ''

        total += 1
        print(f'─' * 60)
        print(f'Question {total}: {q_text}')
        for opt in options:
            print(f'  {opt}')
        print()

        while True:
            answer = input('Your answer (A/B/C/D) or Q to quit: ').strip().upper()
            if answer == 'Q':
                print(f'\n📊 Final Score: {score}/{total}  ({100*score//max(1,total)}%)')
                return
            if answer in ('A', 'B', 'C', 'D'):
                break
            print('  Please enter A, B, C, or D.')

        if answer == correct:
            score += 1
            print(f'\n  ✅ Correct!')
        else:
            print(f'\n  ❌ Wrong. Correct answer: {correct}')

        if explanation:
            print(f'  📖 {explanation}')
        print()

    print('─' * 60)
    pct = 100 * score // max(1, total)
    if pct >= 80:
        grade = '🏆 Excellent!'
    elif pct >= 60:
        grade = '👍 Good work!'
    elif pct >= 40:
        grade = '📚 Keep studying!'
    else:
        grade = '🔄 Review the topic and try again!'
    print(f'\n📊 Final Score: {score}/{total}  ({pct}%)  {grade}\n')


# ── News Feed ────────────────────────────────────────────────────────────────

def cmd_news(topic: str = 'AI and Linux') -> str:
    """Fetch and summarize tech/AI/RedHat news from public RSS feeds."""
    RSS_FEEDS = {
        'AI': 'https://www.artificialintelligence-news.com/feed/',
        'Red Hat': 'https://www.redhat.com/en/rss/blog',
        'Linux': 'https://lwn.net/headlines/rss',
        'Ansible': 'https://www.ansible.com/blog/rss.xml',
        'Security': 'https://thehackernews.com/feeds/posts/default',
    }

    print(f'\n📰 HAL News: {topic}\n')

    headlines = []
    # Try to fetch from a relevant feed
    feed_key = next((k for k in RSS_FEEDS if k.lower() in topic.lower()), 'AI')
    feed_url = RSS_FEEDS.get(feed_key, RSS_FEEDS['AI'])

    try:
        req = urllib.request.Request(feed_url, headers={'User-Agent': 'HAL/2.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            xml = r.read().decode('utf-8', errors='replace')

        # Simple RSS title extractor
        titles = re.findall(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', xml, re.DOTALL)
        links = re.findall(r'<link>([^<]+)</link>', xml)
        descriptions = re.findall(r'<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>', xml, re.DOTALL)

        # Skip feed title (first entry)
        for i, title in enumerate(titles[1:11], start=1):
            title = re.sub(r'<[^>]+>', '', title).strip()
            desc = descriptions[i].strip()[:200] if i < len(descriptions) else ''
            desc = re.sub(r'<[^>]+>', '', desc).strip()
            link = links[i] if i < len(links) else ''
            headlines.append({'title': title, 'desc': desc, 'link': link})
            print(f'  {i:2}. {title}')
            if desc:
                print(f'      {desc[:120]}...' if len(desc) > 120 else f'      {desc}')
            if link:
                print(f'      {link}')
            print()
    except Exception as e:
        print(f'  Could not fetch RSS feed: {e}')
        # Fall back to web search for news
        return cmd_web_search(f'{topic} latest news 2024', num_results=5, synthesize=True)

    if not headlines:
        print('  No headlines found.')
        return ''

    # Synthesize with AI
    persona = _active_persona()
    context = '\n'.join([f"• {h['title']}: {h['desc']}" for h in headlines])
    prompt = (
        f'Here are the latest news headlines about {topic}:\n\n{context}\n\n'
        f'Give me a 3-5 sentence briefing on the most significant developments and what they mean '
        f'for IT/infrastructure/AI practitioners.'
    )
    print('─' * 60)
    print('🤖 HAL news briefing:\n')
    summary = _ask_ollama(prompt, system=persona.get('system', ''), temperature=0.4, task_hint='search')
    print(summary)
    return summary


# ── HuggingFace Model Search ─────────────────────────────────────────────────

def cmd_hf_search(keyword: str, top_n: int = 10) -> list:
    """Search HuggingFace Hub for models by keyword."""
    print(f'\n🤗 HuggingFace Model Search: {keyword}\n')

    try:
        from huggingface_hub import HfApi
        api = HfApi()
    except ImportError:
        print('  huggingface_hub not installed. Install with: pip install huggingface-hub')
        return []

    try:
        models = list(api.list_models(search=keyword, limit=50))
    except Exception as e:
        print(f'  Search error: {e}')
        return []

    if not models:
        print(f'  No models found for: {keyword}')
        return []

    # Sort by downloads (safely)
    def get_downloads(m):
        try:
            return int(getattr(m, 'downloads', 0) or 0)
        except Exception:
            return 0

    top_models = sorted(models, key=get_downloads, reverse=True)[:top_n]

    print(f'  {"MODEL":50} {"DOWNLOADS":>10}  {"PIPELINE"}')
    print('  ' + '─' * 80)
    results = []
    for m in top_models:
        mid = getattr(m, 'modelId', None) or getattr(m, 'id', str(m))
        downloads = get_downloads(m)
        pipeline = getattr(m, 'pipeline_tag', '') or ''
        dl_str = f'{downloads:,}' if downloads else '?'
        print(f'  {mid:50} {dl_str:>10}  {pipeline}')
        results.append({'id': mid, 'downloads': downloads, 'pipeline': pipeline})

    print(f'\n  Showing top {len(results)} of {len(models)} results.')
    print(f'\n  To pull as Ollama model: ollama pull hf.co/<model-id>')
    return results


# ── MCP Context Management ───────────────────────────────────────────────────

def cmd_mcp_list() -> list:
    """List available MCP contexts."""
    _ensure_dirs()
    contexts = list(Path(MCP_DIR).glob('*.json'))
    print(f'\n🔗 MCP Contexts ({len(contexts)} found)\n')
    if not contexts:
        print('  No contexts published yet.')
        print('  Publish with: hal --mcp-publish <name> \'{"key":"value"}\'')
        return []
    for ctx in sorted(contexts):
        stat = ctx.stat()
        age = datetime.now() - datetime.fromtimestamp(stat.st_mtime)
        age_str = f'{int(age.total_seconds() // 60)}m ago' if age.total_seconds() < 3600 else f'{int(age.total_seconds() // 3600)}h ago'
        print(f'  • {ctx.stem:30} {stat.st_size:>6} bytes  {age_str}')
    return [c.stem for c in contexts]


def cmd_mcp_read(name: str) -> dict:
    """Read a named MCP context."""
    _ensure_dirs()
    path = Path(MCP_DIR) / f'{name}.json'
    if not path.exists():
        print(f'Context not found: {name}', file=sys.stderr)
        return {}
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    print(f'\n🔗 MCP Context: {name}\n')
    print(json.dumps(data, indent=2))
    return data


def cmd_mcp_publish(name: str, json_str: str) -> str:
    """Publish a JSON payload as a named MCP context."""
    _ensure_dirs()
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f'Invalid JSON: {e}', file=sys.stderr)
        return ''
    payload = {'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'), 'data': data}
    path = Path(MCP_DIR) / f'{name}.json'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
    print(f'✓ Published MCP context: {path}')
    return str(path)


def cmd_mcp_delete(name: str) -> int:
    """Delete a named MCP context."""
    path = Path(MCP_DIR) / f'{name}.json'
    if not path.exists():
        print(f'Context not found: {name}', file=sys.stderr)
        return 1
    path.unlink()
    print(f'✓ Deleted MCP context: {name}')
    return 0


# ── TODO Manager ─────────────────────────────────────────────────────────────

def _load_todos() -> list:
    _ensure_dirs()
    if os.path.exists(TODO_FILE):
        try:
            with open(TODO_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_todos(todos: list):
    _ensure_dirs()
    with open(TODO_FILE, 'w', encoding='utf-8') as f:
        json.dump(todos, f, indent=2)


def cmd_todo(action: str, text: str = '', item_id: int = 0) -> None:
    """Simple AI-aware TODO list manager."""
    todos = _load_todos()
    action = action.strip().lower()

    if action == 'add':
        if not text:
            print('Provide todo text after "add".', file=sys.stderr)
            return
        todo = {
            'id': max((t['id'] for t in todos), default=0) + 1,
            'text': text,
            'done': False,
            'created': datetime.now().isoformat(),
            'priority': 'normal',
        }
        todos.append(todo)
        _save_todos(todos)
        print(f'  ✅ Added todo #{todo["id"]}: {text}')

    elif action in ('list', 'ls'):
        pending = [t for t in todos if not t['done']]
        done = [t for t in todos if t['done']]
        print(f'\n📝 HAL TODO List  ({len(pending)} pending, {len(done)} done)\n')
        if not todos:
            print('  No todos. Add with: hal --todo add "your task"')
        else:
            for t in sorted(pending, key=lambda x: x['id']):
                print(f'  [ ] #{t["id"]:3}  {t["text"]}')
            for t in sorted(done, key=lambda x: x['id'])[-5:]:
                print(f'  [✓] #{t["id"]:3}  {t["text"]}')

    elif action in ('done', 'complete', 'check'):
        if not item_id:
            print('Provide todo ID: hal --todo done <id>', file=sys.stderr)
            return
        found = next((t for t in todos if t['id'] == item_id), None)
        if not found:
            print(f'Todo #{item_id} not found.')
            return
        found['done'] = True
        found['completed'] = datetime.now().isoformat()
        _save_todos(todos)
        print(f'  ✅ Completed: {found["text"]}')

    elif action in ('delete', 'rm', 'remove'):
        if not item_id:
            print('Provide todo ID: hal --todo delete <id>', file=sys.stderr)
            return
        before = len(todos)
        todos = [t for t in todos if t['id'] != item_id]
        _save_todos(todos)
        print(f'  {"Deleted" if len(todos) < before else "Not found"}: #{item_id}')

    elif action == 'clear':
        todos = [t for t in todos if not t['done']]
        _save_todos(todos)
        print('  ✓ Cleared completed todos.')

    elif action == 'prioritize':
        # Use AI to prioritize the todo list
        pending = [t for t in todos if not t['done']]
        if not pending:
            print('  No pending todos to prioritize.')
            return
        items_str = '\n'.join(f'{t["id"]}. {t["text"]}' for t in pending)
        prompt = (
            f'I have the following TODO items. Please rank them by priority for a sysadmin/DevOps engineer, '
            f'considering impact, urgency, and dependencies. Provide a brief reason for each ranking:\n\n{items_str}'
        )
        print('\n🤖 HAL todo prioritization:\n')
        print(_ask_ollama(prompt, system=PERSONAS['expert']['system'], temperature=0.3))
    else:
        print(f'Unknown action: {action}. Use: add, list, done, delete, clear, prioritize')


# ── System Monitor ───────────────────────────────────────────────────────────

def cmd_sys_monitor(interval: int = 2, iterations: int = 0) -> None:
    """Real-time ASCII system resource monitor."""
    try:
        import psutil
    except ImportError:
        print('psutil not installed. Install with: pip install psutil', file=sys.stderr)
        return

    print('\n🖥  HAL System Monitor  (Ctrl+C to exit)\n')
    count = 0
    try:
        while True:
            cpu = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')

            try:
                load = os.getloadavg()
                load_str = f'{load[0]:.2f} {load[1]:.2f} {load[2]:.2f}'
            except Exception:
                load_str = 'N/A'

            # GPU
            gpu_str = ''
            try:
                out = subprocess.check_output(
                    ['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'],
                    stderr=subprocess.DEVNULL, timeout=2
                )
                for line in out.decode().strip().splitlines():
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) >= 3:
                        gpu_str = f'  GPU: {parts[0]}%  VRAM: {parts[1]}/{parts[2]} MB'
            except Exception:
                pass

            # Build bar
            def bar(pct, width=20):
                filled = int(width * pct / 100)
                color = '🔴' if pct > 90 else '🟡' if pct > 70 else '🟢'
                return f'{color} [{"█" * filled}{"░" * (width - filled)}] {pct:.1f}%'

            ts = datetime.now().strftime('%H:%M:%S')
            print(f'\r\033[K{ts}  CPU: {bar(cpu)}  RAM: {bar(mem.percent)} ({mem.used//1024//1024}MB/{mem.total//1024//1024}MB)  '
                  f'Disk: {bar(disk.percent)}  Load: {load_str}{gpu_str}', end='', flush=True)

            count += 1
            if iterations and count >= iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print('\n\nMonitor stopped.')


# ── Watch Log ────────────────────────────────────────────────────────────────

def cmd_watch_log(file_path: str, pattern: str = '') -> None:
    """Tail a log file and flag anomalies with AI analysis."""
    if not os.path.isfile(file_path):
        print(f'File not found: {file_path}', file=sys.stderr)
        return

    print(f'\n👁  Watching log: {file_path}  (Ctrl+C to stop)\n')
    ERROR_PATTERNS = re.compile(
        r'\b(error|fail|critical|fatal|exception|traceback|refused|denied|timeout|panic|killed|oom)\b',
        re.IGNORECASE
    )
    anomaly_buffer = []
    last_analysis = time.time()

    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        # Seek to end
        f.seek(0, 2)
        try:
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.2)
                    continue
                line = line.rstrip()
                if ERROR_PATTERNS.search(line):
                    print(f'\n  ⚠  {line}')
                    anomaly_buffer.append(line)
                    # Batch-analyze every 5 anomalies or every 30 seconds
                    if len(anomaly_buffer) >= 5 or (time.time() - last_analysis > 30 and anomaly_buffer):
                        print('\n  🤖 HAL anomaly analysis:')
                        error_block = '\n'.join(anomaly_buffer[-10:])
                        analysis = _ask_ollama(
                            f'Analyze these log anomalies and identify root cause + quick remediation:\n{error_block}',
                            system=PERSONAS['expert']['system'], temperature=0.2, task_hint='system'
                        )
                        print(f'  {analysis[:500]}')
                        anomaly_buffer.clear()
                        last_analysis = time.time()
                else:
                    print(f'  {line}')
        except KeyboardInterrupt:
            print('\n\nLog watch stopped.')


# ── Pipe Analyze ─────────────────────────────────────────────────────────────

def cmd_pipe_analyze(context: str = '') -> str:
    """Read from stdin and analyze with AI. Supports: cat log | hal --pipe-analyze."""
    if sys.stdin.isatty():
        print('Usage: cat file.log | hal --pipe-analyze\n       echo "text" | hal --pipe-analyze', file=sys.stderr)
        return ''

    print('\n🔬 HAL Pipe Analyzer\n')
    data = sys.stdin.read()
    if not data.strip():
        print('No input received.')
        return ''

    print(f'  Read {len(data)} bytes. Analyzing...\n')
    persona = _active_persona()

    # Auto-detect content type
    context_hint = context or ''
    if not context_hint:
        if re.search(r'(error|exception|traceback|failed)', data[:500], re.IGNORECASE):
            context_hint = 'This appears to be an error log or stack trace.'
        elif re.search(r'(diff|@@|---|\+\+\+)', data[:200]):
            context_hint = 'This appears to be a diff or patch.'
        elif re.search(r'(def |class |import |function)', data[:200]):
            context_hint = 'This appears to be source code.'

    prompt = (
        f'Analyze the following input and provide a clear, actionable summary.\n'
        f'{context_hint}\n\n'
        f'Input:\n{data[:8000]}'
    )
    answer = _ask_ollama(prompt, system=persona.get('system', ''), temperature=0.3)
    print('🤖 HAL analysis:\n')
    print(answer)
    return answer


# ── Chat Export ──────────────────────────────────────────────────────────────

def cmd_chat_export(fmt: str = 'markdown') -> str:
    """Export HAL interaction history to markdown or HTML."""
    history_dir = os.path.join(AI_HOME, 'history')
    if not os.path.isdir(history_dir):
        print('No conversation history found.', file=sys.stderr)
        return ''

    entries = []
    for fname in sorted(os.listdir(history_dir))[-50:]:  # last 50
        fpath = os.path.join(history_dir, fname)
        if not fname.endswith('.json'):
            continue
        try:
            with open(fpath, encoding='utf-8') as f:
                entry = json.load(f)
            entries.append(entry)
        except Exception:
            pass

    if not entries:
        print('No conversation entries found.')
        return ''

    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    out_dir = os.path.join(AI_HOME, 'exports')
    os.makedirs(out_dir, exist_ok=True)

    if fmt.lower() in ('md', 'markdown'):
        out_path = os.path.join(out_dir, f'hal-chat-{timestamp}.md')
        lines = [f'# HAL Conversation Export\n\nExported: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n\n---\n']
        for e in entries:
            ts = e.get('timestamp', '')[:19].replace('T', ' ')
            user = e.get('user', 'user')
            query = e.get('query', e.get('text', ''))
            response = e.get('response', e.get('answer', ''))
            lines.append(f'**[{ts}] {user}:** {query}\n\n')
            if response:
                lines.append(f'**HAL:** {response}\n\n---\n')
        content = '\n'.join(lines)
    elif fmt.lower() == 'html':
        out_path = os.path.join(out_dir, f'hal-chat-{timestamp}.html')
        rows = []
        for e in entries:
            ts = e.get('timestamp', '')[:19].replace('T', ' ')
            query = e.get('query', e.get('text', '')).replace('<', '&lt;').replace('>', '&gt;')
            response = e.get('response', e.get('answer', '')).replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
            rows.append(f'<div class="entry"><div class="ts">{ts}</div><div class="user">{query}</div><div class="hal">{response}</div></div>')
        content = f'''<!DOCTYPE html><html><head><title>HAL Chat Export</title>
<style>body{{font-family:monospace;max-width:900px;margin:2em auto;background:#1a1a2e;color:#eee}}
.entry{{border-bottom:1px solid #333;padding:1em}}
.ts{{color:#666;font-size:.8em}}.user{{color:#4fc3f7;margin:.5em 0}}.hal{{color:#a5d6a7}}</style>
</head><body><h1>HAL Conversation Export</h1>{''.join(rows)}</body></html>'''
    else:
        print(f'Unknown format: {fmt}. Use markdown or html.')
        return ''

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'✓ Exported {len(entries)} conversation(s) to: {out_path}')
    return out_path


# ── Fun Features ─────────────────────────────────────────────────────────────

TECH_FACTS = [
    "The first computer bug was an actual bug — a moth found in Harvard's Mark II computer in 1947.",
    "Linux powers over 96% of the world's top 1 million web servers.",
    "The name 'Ansible' comes from Ursula K. Le Guin's science fiction novel 'The Dispossessed'.",
    "Red Hat was founded in 1993. The name comes from a red lacrosse cap worn by founder Marc Ewing.",
    "The term 'YAML' stands for 'YAML Ain't Markup Language' — a recursive acronym.",
    "Kubernetes is Greek for 'helmsman' or 'pilot'. Google released it in 2014.",
    "The first version of Ansible was released in 2012 by Michael DeHaan.",
    "Git was created by Linus Torvalds in 2005 to manage the Linux kernel source code.",
    "Python was named after Monty Python's Flying Circus, not the snake.",
    "The word 'robot' was coined in 1920 by Czech playwright Karel Čapek.",
    "Many enterprise configuration systems are built on open-source projects.",
    "There are over 600 Linux distros, but RHEL accounts for the most enterprise deployments.",
    "SSH was invented by Tatu Ylönen in 1995 after a password-sniffing attack at his university.",
    "The OSI model has 7 layers. Most sysadmins memorize them as 'Please Do Not Throw Sausage Pizza Away'.",
    "Vim was released in 1991. Its predecessor, Vi, was written in 1976.",
]

MOTIVATIONS = [
    f"Every system you harden today is a breach you prevent tomorrow, {HAL_DISPLAY_NAME}. Keep going.",
    f"Automation isn't about replacing people — it's about freeing them to do better things. You're doing great, {HAL_DISPLAY_NAME}.",
    f"The best sysadmin is the one whose work is invisible. Your invisible work keeps everything running.",
    f"Infrastructure as code means your work lives on long after you've moved on. Your playbooks matter.",
    f"Every ticket you close is a problem solved for a real person. That matters, {HAL_DISPLAY_NAME}.",
    f"You don't need to fix everything at once. One service at a time, one playbook at a time.",
    f"The chaos will always be there. But so will you — with Ansible, HAL, and a cup of coffee.",
    f"Documentation you write today is the gift you give your future self (and your team).",
    f"Security isn't a product, it's a process. And you're showing up for that process every day.",
]


def cmd_word_of_day() -> str:
    """Generate a tech/AI word of the day with explanation."""
    day_seed = datetime.now().strftime('%Y%m%d')

    tech_terms = [
        'idempotency', 'drift detection', 'golden image', 'blue-green deployment',
        'service mesh', 'mTLS', 'RBAC', 'immutable infrastructure', 'GitOps',
        'ephemeral environment', 'chaos engineering', 'SLO', 'SLI', 'error budget',
        'canary deployment', 'circuit breaker', 'bulkhead pattern', 'SSOT',
        'infrastracture drift', 'configuration management', 'day-2 operations',
        'observability', 'tracing', 'cardinality', 'supply chain security',
        'zero trust architecture', 'SBOM', 'CVE triage', 'threat modeling',
    ]
    idx = int(day_seed) % len(tech_terms)
    term = tech_terms[idx]

    print(f'\n📖 HAL Word of the Day: {term.upper()}\n')
    prompt = (
        f'Explain the tech/DevOps/IT term "{term}" in a fun but educational way. '
        f'Include: what it means, why it matters, a real-world analogy, and a practical example '
        f'(preferably with Red Hat/Ansible/Linux context). Keep it under 200 words.'
    )
    answer = _ask_ollama(prompt, system=PERSONAS['teacher']['system'], temperature=0.5, task_hint='reasoning')
    print(answer)
    return answer


def cmd_fact() -> str:
    """Return a random tech/AI/Linux fact."""
    import random
    fact = random.choice(TECH_FACTS)
    print(f'\n💡 Tech Fact:\n\n  {fact}\n')

    # Ask HAL to expand
    prompt = f'Expand on this tech fact with 2-3 more interesting related details: "{fact}"'
    extra = _ask_ollama(prompt, system=PERSONAS['teacher']['system'], temperature=0.6)
    print(extra)
    return fact


def cmd_motivate() -> str:
    """Generate a motivational message for sysadmins/DevOps."""
    import random
    base = random.choice(MOTIVATIONS)
    print(f'\n🌟 HAL says:\n\n  {base}\n')

    prompt = (
        f'Generate a short, genuine motivational message (2-4 sentences) for a sysadmin or DevOps engineer '
        f'who works with Red Hat Linux, Ansible, and AI infrastructure. Make it specific and meaningful, '
        f'not generic. Address them as {HAL_DISPLAY_NAME}.'
    )
    extra = _ask_ollama(prompt, system=PERSONAS['friendly']['system'], temperature=0.8)
    print(extra)
    return base


# ── Main CLI ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description='HAL Tools — AI/LLM/MCP companion utilities',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Model management
    mg = ap.add_argument_group('Model Management')
    mg.add_argument('--model-list', action='store_true', help='List all available Ollama models')
    mg.add_argument('--model-pull', metavar='MODEL', help='Pull an Ollama model (e.g. mistral:7b)')
    mg.add_argument('--model-delete', metavar='MODEL', help='Delete an Ollama model')
    mg.add_argument('--model-info', metavar='MODEL', help='Show detailed info about a model')
    mg.add_argument('--model-compare', metavar='MODELS', help='Compare 2+ models on the same prompt (comma/space separated)')
    mg.add_argument('--benchmark', nargs='?', const='', metavar='MODEL', help='Benchmark model inference speed')
    mg.add_argument('--benchmark-runs', type=int, default=3, metavar='N', help='Number of benchmark runs (default: 3)')

    # HuggingFace
    hf = ap.add_argument_group('HuggingFace')
    hf.add_argument('--hf-search', metavar='KEYWORD', help='Search HuggingFace Hub for models')
    hf.add_argument('--hf-top', type=int, default=10, metavar='N', help='Top N results for --hf-search (default: 10)')

    # AI Operations
    ai = ap.add_argument_group('AI Operations')
    ai.add_argument('--web-search', metavar='QUERY', help='Web search with AI synthesis')
    ai.add_argument('--summarize', metavar='URL_OR_FILE', help='Summarize a URL or file')
    ai.add_argument('--code-review', metavar='FILE', help='AI code review of a file')
    ai.add_argument('--code-review-focus', metavar='ASPECT', default='', help='Focus area for code review')
    ai.add_argument('--suggest-fix', metavar='FILE', help='AI generate a suggested fix for a file (writes proposal to .hal_suggestions/patches)')
    ai.add_argument('--suggest-fix-apply', action='store_true', help='If set, apply the suggested fix (conservative: creates backup first).')
    ai.add_argument('--explain-error', action='store_true', help='Explain error output (reads stdin or --file)')
    ai.add_argument('--diff-explain', nargs=2, metavar=('FILE1', 'FILE2'), help='AI-explained diff between two files')
    ai.add_argument('--generate-readme', metavar='PATH', help='Generate README.md for a project directory')
    ai.add_argument('--pipe-analyze', action='store_true', help='Analyze piped stdin input with AI')
    ai.add_argument('--pipe-context', metavar='HINT', default='', help='Context hint for --pipe-analyze')

    # Fun
    fun = ap.add_argument_group('Fun & Productivity')
    fun.add_argument('--quiz', metavar='TOPIC', help='Interactive multiple-choice quiz on any topic')
    fun.add_argument('--quiz-questions', type=int, default=5, help='Number of quiz questions (default: 5)')
    fun.add_argument('--news', nargs='?', const='AI and Linux', metavar='TOPIC', help='Fetch and summarize tech news')
    fun.add_argument('--word-of-day', action='store_true', help='Tech/AI word of the day')
    fun.add_argument('--fact', action='store_true', help='Random tech/AI/Linux fact')
    fun.add_argument('--motivate', action='store_true', help='Motivational message for sysadmins')

    # Personas
    pa = ap.add_argument_group('Personas')
    pa.add_argument('--personas', action='store_true', help='List available HAL personas')
    pa.add_argument('--set-persona', metavar='NAME', help='Set active HAL persona')
    pa.add_argument('--configure', nargs='*', metavar='SETTING',
                    help='Configure HAL behaviour, e.g. --configure hal-quotes yes|no')
    pa.add_argument('--slj', action='store_true', help=argparse.SUPPRESS)  # secret persona

    # MCP
    mcp = ap.add_argument_group('MCP Contexts')
    mcp.add_argument('--mcp-list', action='store_true', help='List published MCP contexts')
    mcp.add_argument('--mcp-read', metavar='NAME', help='Read a named MCP context')
    mcp.add_argument('--mcp-publish', nargs=2, metavar=('NAME', 'JSON'), help='Publish a JSON MCP context')
    mcp.add_argument('--mcp-delete', metavar='NAME', help='Delete a named MCP context')

    # System
    sys_grp = ap.add_argument_group('System Tools')
    sys_grp.add_argument('--sys-monitor', action='store_true', help='Real-time ASCII system resource monitor')
    sys_grp.add_argument('--sys-monitor-interval', type=int, default=2, help='Monitor refresh interval seconds (default: 2)')
    sys_grp.add_argument('--watch-log', metavar='FILE', help='Watch a log file and flag anomalies with AI')

    # TODO
    todo_grp = ap.add_argument_group('TODO Manager')
    todo_grp.add_argument('--todo', nargs='+', metavar='ACTION', help='TODO manager: add|list|done|delete|clear|prioritize [text] [id]')

    # Chat
    chat_grp = ap.add_argument_group('Chat & History')
    chat_grp.add_argument('--chat-export', nargs='?', const='markdown', metavar='FORMAT',
                          help='Export conversation history (markdown or html)')

    # Misc
    ap.add_argument('--file', metavar='PATH', help='Input file path (used with --explain-error, etc.)')
    ap.add_argument('--prompt', metavar='TEXT', help='Prompt text (used with --model-compare, etc.)')
    ap.add_argument('--model', metavar='MODEL', help='Override model for AI operations')
    ap.add_argument('--no-synthesize', action='store_true', help='Skip AI synthesis step for web-search/news')

    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()

    _ensure_dirs()

    # Model management
    if args.model_list:
        cmd_model_list()
        return 0

    if args.model_pull:
        return cmd_model_pull(args.model_pull)

    if args.model_delete:
        return cmd_model_delete(args.model_delete)

    if args.model_info:
        cmd_model_info(args.model_info)
        return 0

    if args.model_compare:
        prompt = args.prompt or 'Explain in 3 sentences why Ansible is better than shell scripts for configuration management.'
        cmd_model_compare(args.model_compare, prompt)
        return 0

    if args.benchmark is not None:
        model = args.benchmark or (args.model or '')
        cmd_benchmark(model=model, runs=args.benchmark_runs)
        return 0

    # HuggingFace
    if args.hf_search:
        cmd_hf_search(args.hf_search, top_n=args.hf_top)
        return 0

    # AI operations
    if args.web_search:
        cmd_web_search(args.web_search, synthesize=not args.no_synthesize)
        return 0

    if args.summarize:
        cmd_summarize(args.summarize)
        return 0

    if args.code_review:
        cmd_code_review(args.code_review, focus=args.code_review_focus)
        return 0

    if args.suggest_fix:
        cmd_suggest_fix(args.suggest_fix, apply=args.suggest_fix_apply)
        return 0

    if args.explain_error:
        cmd_explain_error(file_path=args.file or '')
        return 0

    if args.diff_explain:
        cmd_diff_explain(args.diff_explain[0], args.diff_explain[1])
        return 0

    if args.generate_readme:
        cmd_generate_readme(args.generate_readme)
        return 0

    if args.pipe_analyze:
        cmd_pipe_analyze(context=args.pipe_context)
        return 0

    # Fun
    if args.quiz:
        cmd_quiz(args.quiz, num_questions=args.quiz_questions)
        return 0

    if args.news is not None:
        cmd_news(args.news)
        return 0

    if args.word_of_day:
        cmd_word_of_day()
        return 0

    if args.fact:
        cmd_fact()
        return 0

    if args.motivate:
        cmd_motivate()
        return 0

    # Personas
    if args.personas:
        cmd_personas()
        return 0

    if args.set_persona:
        return cmd_set_persona(args.set_persona)

    if args.configure is not None:
        return cmd_configure(args.configure)

    if args.slj:
        return cmd_slj()

    # MCP
    if args.mcp_list:
        cmd_mcp_list()
        return 0

    if args.mcp_read:
        cmd_mcp_read(args.mcp_read)
        return 0

    if args.mcp_publish:
        cmd_mcp_publish(args.mcp_publish[0], args.mcp_publish[1])
        return 0

    if args.mcp_delete:
        cmd_mcp_delete(args.mcp_delete)
        return 0

    # System
    if args.sys_monitor:
        cmd_sys_monitor(interval=args.sys_monitor_interval)
        return 0

    if args.watch_log:
        cmd_watch_log(args.watch_log)
        return 0

    # TODO
    if args.todo:
        parts = args.todo
        action = parts[0]
        # Support: --todo done 3  or  --todo add "some text"
        item_id = 0
        text = ''
        if len(parts) > 1:
            try:
                item_id = int(parts[-1])
                text = ' '.join(parts[1:-1])
            except ValueError:
                text = ' '.join(parts[1:])
        cmd_todo(action, text=text, item_id=item_id)
        return 0

    # Chat export
    if args.chat_export:
        cmd_chat_export(args.chat_export)
        return 0

    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
