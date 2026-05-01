#!/usr/bin/env python3
"""HAL CLI: send user requests to the local MCP/OLLAMA bridge and record interactions.

Usage:
  hal.py "What is the system status?"
  hal.py --remediate "Check disk errors"  # also invoke remediator on the created entry
  hal.py --exec --remediate "Try to fix service"  # allow execution (will set ALLOW_AUTO_FIX=1)
  hal.py --feedback <entry_path_or_prefix> "It worked"  # append user feedback to an existing entry
"""
import os
import sys
import json
import socket
import argparse
import subprocess
import re
import random
import csv
import io
import shutil
import zipfile
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except Exception:
    requests = None

HOME = os.path.expanduser('~')
AI_HOME = os.path.join(HOME, '.mcp-ai')
TRAIN_DIR = os.path.join(AI_HOME, 'training')
FIXES_DIR = os.path.join(AI_HOME, 'fixes')
REPORTS_DIR = os.path.join(AI_HOME, 'reports')
CACHE_DIR = os.path.join(AI_HOME, 'cache', 'intel')
OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:1776/api/chat')
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
HAL_DISPLAY_NAME = os.environ.get('HAL_DISPLAY_NAME', 'Dave')
ASSISTANT_NAME = os.environ.get('HAL_ASSISTANT_NAME', 'HAL9000')
WELL_PHRASE = os.environ.get('HAL_WELL_PHRASE', f'I am well today {HAL_DISPLAY_NAME}, thank you for asking')

# Optional one-command runtime overrides set by CLI flags in main().
RUNTIME_FORCE_MODEL = None
RUNTIME_FORCE_PROFILE = None

# Bridge circuit breaker state (process-local)
_BRIDGE_FAIL_COUNT = 0
_BRIDGE_OPEN_UNTIL = 0.0

# Lightweight in-memory training index cache
_TRAINING_INDEX_CACHE = {
    'signature': None,
    'built_at': 0.0,
    'entries': [],
}

# 🚀 PERFORMANCE: Response cache with TTL (default 1 hour)
_RESPONSE_CACHE = {}
_RESPONSE_CACHE_TTL = int(os.environ.get('HAL_CACHE_TTL_SEC', '3600'))

# 🧠 SMART: Query classification registry (replaces 30+ _is_*_query functions)
_QUERY_CLASSIFIER_REGISTRY = {}

# 📊 COOL: Session analytics and context tracking
_SESSION_CONTEXT = {
    'session_id': None,
    'started_at': 0.0,
    'queries': [],  # Query history
    'total_calls': 0,
    'cache_hits': 0,
    'models_used': {},  # model -> count
}

_ACCOUNT_NAME_CACHE = {
    'built_at': 0.0,
    'names': [],
}

# 💾 COOL: Query suggestion engine
_RECENT_QUERIES = []
_SPINNER_DEPTH = 0
VOICE_ENABLED = False
VOICE_RATE = 170
VOICE_NAME = None


def _run_with_spinner(label: str, func, *args, **kwargs):
    """Run a callable while showing a tiny terminal spinner so HAL doesn't look stuck."""
    global _SPINNER_DEPTH
    if _SPINNER_DEPTH > 0:
        return func(*args, **kwargs)

    use_spinner = os.environ.get('HAL_SPINNER', '1') != '0' and sys.stdout and sys.stdout.isatty()
    if not use_spinner:
        return func(*args, **kwargs)

    done = threading.Event()
    frames = ['|', '/', '-', '\\']

    def _spin():
        i = 0
        while not done.is_set():
            sys.stdout.write(f'\r{label} {frames[i % len(frames)]}')
            sys.stdout.flush()
            i += 1
            time.sleep(0.12)

    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    _SPINNER_DEPTH += 1
    try:
        return func(*args, **kwargs)
    finally:
        _SPINNER_DEPTH = max(0, _SPINNER_DEPTH - 1)
        done.set()
        t.join(timeout=0.3)
        sys.stdout.write(f'\r{label} done.\n')
        sys.stdout.flush()


def _run_subprocess_with_spinner(label: str, cmd: list[str], **kwargs):
    """Run subprocess.run with spinner for long-running external commands."""
    return _run_with_spinner(label, subprocess.run, cmd, **kwargs)


def _tts_binary() -> str | None:
    return shutil.which('espeak-ng') or shutil.which('espeak')


def _speech_ready() -> bool:
    return _tts_binary() is not None


def _speech_text(text: str, max_chars: int = 600) -> str:
    if not text:
        return ''
    # Keep spoken output concise and readable.
    cleaned = re.sub(r'[`*_#\[\]{}()<>]', ' ', text)
    cleaned = re.sub(r'https?://\S+', ' link ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:max_chars]


def speak_if_enabled(text: str) -> None:
    if not VOICE_ENABLED:
        return
    spoken = _speech_text(text)
    if not spoken:
        return
    tts = _tts_binary()
    if not tts:
        return
    cmd = [tts, '-s', str(max(90, min(320, int(VOICE_RATE))))]
    if VOICE_NAME:
        cmd.extend(['-v', str(VOICE_NAME)])
    cmd.append(spoken)
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _run_voice_chat(user: str) -> None:
    print('\nHAL voice chat mode')
    print('Type your prompts. HAL will speak responses. Type exit to quit.\n')
    speak_if_enabled('Voice chat is ready. Ask me anything.')
    while True:
        try:
            text = input('you> ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nExiting voice chat.')
            break
        if not text:
            continue
        if text.lower() in ('exit', 'quit', 'q'):
            print('Exiting voice chat.')
            break
        rag_context = search_training_data_for_rag(text)
        resp = call_bridge(text, rag_context=rag_context)
        assistant_text = extract_assistant_content(resp)
        out = assistant_text if assistant_text else str(resp)
        print('\nHAL response:\n')
        print(out)
        speak_if_enabled(out)
        try:
            write_interaction(user, f'VOICE_CHAT:{text}', out)
        except Exception:
            pass


def _hash_query(text: str) -> str:
    """Fingerprint query for deduplication and caching."""
    import hashlib
    return hashlib.md5(text.lower().strip().encode()).hexdigest()[:12]


def _get_cached_response(query: str) -> str | None:
    """🚀 Check cache for recent response (TTL-aware)."""
    h = _hash_query(query)
    if h in _RESPONSE_CACHE:
        entry = _RESPONSE_CACHE[h]
        age = time.time() - entry['cached_at']
        if age < _RESPONSE_CACHE_TTL:
            _SESSION_CONTEXT['cache_hits'] += 1
            return entry['response']
        else:
            del _RESPONSE_CACHE[h]
    return None


def _store_cached_response(query: str, response: str) -> None:
    """🚀 Cache response with timestamp."""
    h = _hash_query(query)
    _RESPONSE_CACHE[h] = {'response': response, 'cached_at': time.time(), 'query': query}
    # Limit cache size (keep last 100 responses)
    if len(_RESPONSE_CACHE) > 100:
        oldest = min(_RESPONSE_CACHE.items(), key=lambda x: x[1]['cached_at'])
        del _RESPONSE_CACHE[oldest[0]]


def _bridge_health_ok(timeout: float = 2.0) -> bool:
    """Return True when local bridge health endpoint reports ok."""
    url = os.environ.get('HAL_BRIDGE_HEALTH_URL', 'http://localhost:1776/health')
    try:
        if requests:
            r = requests.get(url, timeout=timeout)
            if r.status_code != 200:
                return False
            try:
                payload = r.json()
                return str(payload.get('status', '')).lower() == 'ok'
            except Exception:
                return True

        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            raw = resp.read().decode('utf-8', errors='replace')
            try:
                payload = json.loads(raw)
                return str(payload.get('status', '')).lower() == 'ok'
            except Exception:
                return bool(raw)
    except Exception:
        return False


def _start_bridge_background() -> bool:
    """Best-effort bridge startup when HAL is connected but bridge is down."""
    start_script = os.path.join(BASE_DIR, 'mcp-ai', 'start-bridge.sh')
    if not os.path.exists(start_script):
        return False

    # Avoid duplicate starts if bridge became healthy concurrently.
    if _bridge_health_ok(timeout=1.5):
        return True

    ensure_dirs()
    log_path = os.path.join(REPORTS_DIR, 'bridge-autostart.log')
    try:
        with open(log_path, 'a', encoding='utf-8') as log_fh:
            subprocess.Popen(
                ['bash', start_script],
                stdout=log_fh,
                stderr=log_fh,
                cwd=BASE_DIR,
                start_new_session=True,
            )
    except Exception:
        return False

    # Wait briefly for startup.
    for _ in range(10):
        if _bridge_health_ok(timeout=1.5):
            return True
        time.sleep(0.5)
    return False


def _intel_cache_file(account: str) -> str:
    return os.path.join(CACHE_DIR, f'{_slugify(account)}.json')


def _cleanup_intel_cache(max_age_days: int = 3) -> None:
    ensure_dirs()
    cutoff = time.time() - max(1, max_age_days) * 86400
    try:
        for fp in Path(CACHE_DIR).glob('*.json'):
            try:
                if fp.stat().st_mtime < cutoff:
                    fp.unlink(missing_ok=True)
            except Exception:
                continue
    except Exception:
        pass


def _load_intel_cache(account: str, max_age_days: int = 3, require_same_day: bool = True) -> str | None:
    path = _intel_cache_file(account)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        return None

    ts = str(data.get('timestamp', '') or '')
    report = str(data.get('report', '') or '')
    if not ts or not report:
        return None

    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None

    age_days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400.0
    if age_days > max(1, max_age_days):
        return None
    if require_same_day and dt.astimezone(timezone.utc).date() != datetime.now(timezone.utc).date():
        return None
    return report


def _save_intel_cache(account: str, report: str) -> None:
    ensure_dirs()
    path = _intel_cache_file(account)
    payload = {
        'account': account,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'report': report,
    }
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=2)
    except Exception:
        pass


def _register_query_classifier(pattern: str, classifier_name: str, func) -> None:
    """🧠 Register a query type classifier for later use."""
    _QUERY_CLASSIFIER_REGISTRY[classifier_name] = {'pattern': pattern, 'func': func}


def _classify_query_fast(text: str) -> str | None:
    """🧠 Fast query classification using registry (replaces 30+ if statements)."""
    for name, info in _QUERY_CLASSIFIER_REGISTRY.items():
        try:
            if info['func'](text):
                return name
        except Exception:
            pass
    return None


def _update_session_analytics(query: str, model: str) -> None:
    """📊 Track query analytics for current session and persist to disk."""
    _SESSION_CONTEXT['total_calls'] += 1
    entry = {
        'query': query[:100],
        'model': model,
        'timestamp': time.time()
    }
    _SESSION_CONTEXT['queries'].append(entry)
    _SESSION_CONTEXT['models_used'][model] = _SESSION_CONTEXT['models_used'].get(model, 0) + 1
    # Persist to rolling log so --analytics reads cross-session history
    log_path = os.path.join(AI_HOME, 'hal-query-log.jsonl')
    try:
        os.makedirs(AI_HOME, exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry) + '\n')
        # Keep log bounded to last 10 000 lines
        try:
            with open(log_path, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
            if len(lines) > 10000:
                with open(log_path, 'w', encoding='utf-8') as fh:
                    fh.writelines(lines[-10000:])
        except Exception:
            pass
    except Exception:
        pass


def _suggest_next_queries(current_query: str) -> list[str]:
    """💡 COOL: Suggest follow-up questions based on history."""
    suggestions = []
    if len(_RECENT_QUERIES) > 2:
        # Suggest related topics from recent queries
        last_3 = _RECENT_QUERIES[-3:]
        if 'satellite' in current_query.lower():
            suggestions.extend([
                'What are best practices for Satellite 6.18 performance tuning?',
                'How do I configure capsules with load balancing?',
            ])
        if 'ansible' in current_query.lower():
            suggestions.extend([
                'Show me an example AAP workflow for deployment.',
                'How do I use event-driven Ansible automation?',
            ])
    return suggestions[:3]  # Top 3 suggestions


def _bridge_cb_state_path() -> str:
    return os.path.join(AI_HOME, 'bridge_cb_state.json')


def _load_bridge_cb_state() -> tuple[int, float]:
    try:
        p = _bridge_cb_state_path()
        if not os.path.exists(p):
            return (0, 0.0)
        with open(p, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return (int(data.get('fail_count', 0) or 0), float(data.get('open_until', 0.0) or 0.0))
    except Exception:
        return (0, 0.0)


def _save_bridge_cb_state(fail_count: int, open_until: float) -> None:
    try:
        ensure_dirs()
        p = _bridge_cb_state_path()
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump({'fail_count': int(fail_count), 'open_until': float(open_until), 'updated_at': ts_now()}, fh)
    except Exception:
        pass


def load_config():
    cfg = {}
    try:
        cfg_path = os.path.join(AI_HOME, 'config.json')
        if os.path.exists(cfg_path):
            with open(cfg_path, 'r', encoding='utf-8') as fh:
                cfg = json.load(fh)
    except Exception:
        cfg = {}
    return cfg


CONFIG = load_config()
# conversational mode can be toggled via env HAL_CONVERSATIONAL or config 'conversational'
CONVERSATIONAL = os.environ.get('HAL_CONVERSATIONAL', str(CONFIG.get('conversational', 'true'))).lower() in ('1','true','yes','y')


def ensure_dirs():
    for d in (TRAIN_DIR, FIXES_DIR, REPORTS_DIR, CACHE_DIR):
        os.makedirs(d, exist_ok=True)


def ts_now():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _slugify(value: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9]+', '-', (value or '').strip().lower())
    return s.strip('-') or 'account'


def write_interaction(user, request_text, response_text):
    ensure_dirs()
    ts = ts_now()
    host = socket.gethostname()
    entry = {
        'type': 'hal_interaction',
        'timestamp': ts,
        'host': host,
        'user': user,
        'request': request_text,
        'ai_response_raw': response_text,
        'ai_summary': (response_text or '')[:2000]
    }
    fname = os.path.join(TRAIN_DIR, f'hal-{host}-{ts}.jsonl')
    with open(fname, 'w', encoding='utf-8') as fh:
        json.dump(entry, fh, indent=2)
    return fname


def get_available_models():
    """Fetch list of available models from Ollama."""
    try:
        if requests:
            r = requests.get('http://localhost:11434/api/tags', timeout=5)
            if r.status_code == 200:
                data = r.json()
                return [m['name'] for m in data.get('models', [])]
    except Exception:
        pass
    
    try:
        import urllib.request
        with urllib.request.urlopen('http://localhost:11434/api/tags', timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return [m['name'] for m in data.get('models', [])]
    except Exception:
        pass
    
    return []


DEFAULT_MODEL_PROFILES = {
    # Generic/default conversational + mixed tasks
    'general': [
        'qwen2.5-coder:7b',
        'llama4:scout',
        'scout-human:latest',
        'mistral',
        'neural-chat:latest',
        'neural-chat',
    ],
    # Code and templating tasks
    'codegen': [
        'qwen2.5-coder:7b',
        'llama4:scout',
        'scout-human:latest',
        'mistral',
    ],
    # Runbooks / deployment / patch strategy
    'strategy': [
        'llama4:scout',
        'qwen2.5-coder:7b',
        'scout-human:latest',
        'mistral',
    ],
    # Business/account/intel summaries
    'business': [
        'llama4:scout',
        'mistral',
        'qwen2.5-coder:7b',
        'scout-human:latest',
    ],
    # Diagnostics/troubleshooting summaries
    'diagnostics': [
        'qwen2.5-coder:7b',
        'llama4:scout',
        'mistral',
        'scout-human:latest',
    ],
}


def _load_model_profile_overrides() -> dict:
    """Load optional model profile overrides from env/config.

    Supports:
      HAL_MODEL_OVERRIDES='{"codegen": ["qwen2.5-coder:7b"], "general": ["llama4:scout"]}'
      config.json key: model_overrides with same structure
    """
    raw = os.environ.get('HAL_MODEL_OVERRIDES')
    if not raw:
        raw = CONFIG.get('model_overrides')

    if not raw:
        return {}

    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            return {}
        cleaned = {}
        for k, v in parsed.items():
            key = str(k).strip().lower()
            if key not in DEFAULT_MODEL_PROFILES:
                continue
            if isinstance(v, str):
                cleaned[key] = [v]
            elif isinstance(v, list):
                cleaned[key] = [str(x) for x in v if str(x).strip()]
        return cleaned
    except Exception:
        return {}


def _merged_model_profiles() -> dict:
    """Merge built-in model profiles with optional env/config overrides."""
    profiles = {k: list(v) for k, v in DEFAULT_MODEL_PROFILES.items()}
    overrides = _load_model_profile_overrides()
    for k, v in overrides.items():
        if v:
            profiles[k] = v
    return profiles


def _infer_task_profile(text: str) -> str:
    """Infer task profile from query text for dynamic model routing."""
    if not text:
        return 'general'

    q = text.lower()

    # Code generation / structured code tasks
    if re.search(r'\b(playbook|ansible\s+role|ansible\s+collection|jinja2?|template|python\s+script|code\s+generator|module\s+scaffold|```|write\s+code|generate\s+code)\b', q):
        return 'codegen'

    # Diagnostics / troubleshooting / remediation
    if re.search(r'\b(diagnostic|diagnostics|troubleshoot|troubleshooting|error|failed|failure|health\s*check|status\s*check|remediat|fix)\b', q):
        return 'diagnostics'

    # Strategy / runbooks / setup plans
    if re.search(r'\b(runbook|strategy|plan|roadmap|set\s*up|setup|install|deploy|migrate|patch|upgrade|update|configure)\b', q):
        return 'strategy'

    # Business/account insight workflows
    if re.search(r'\b(account|intel|stakeholder|executive|business|stock|csv|brief|customer)\b', q):
        return 'business'

    return 'general'


def choose_model_for_task(text: str | None = None, task_profile: str | None = None, available: list[str] | None = None) -> str:
    """Choose best available model for a task profile (or inferred profile)."""
    forced_model = RUNTIME_FORCE_MODEL or os.environ.get('HAL_FORCE_MODEL')
    if forced_model:
        # Return forced model even if unavailable so the error is explicit to the user.
        return forced_model

    profiles = _merged_model_profiles()
    forced_profile = RUNTIME_FORCE_PROFILE or os.environ.get('HAL_FORCE_PROFILE')
    profile = (task_profile or forced_profile or _infer_task_profile(text or '')).strip().lower()
    if profile not in profiles:
        profile = 'general'

    available_models = available if available is not None else get_available_models()
    preferred_list = profiles.get(profile, []) + profiles.get('general', [])

    # De-duplicate while preserving order
    seen = set()
    ordered = []
    for m in preferred_list:
        if m not in seen:
            ordered.append(m)
            seen.add(m)

    for model in ordered:
        if model in available_models:
            return model

    if available_models:
        return available_models[0]

    # No available list from Ollama; return top preferred for clearer error path
    return ordered[0] if ordered else 'qwen2.5-coder:7b'


def get_best_available_model(preferred_model='qwen2.5-coder:7b'):
    """Get best available model, with fallback to what's installed."""
    # Backward-compatible helper now routed through task-aware chooser.
    # If a preferred model is supplied, treat it as a temporary profile override.
    available = get_available_models()
    if preferred_model:
        if preferred_model in available:
            return preferred_model
    return choose_model_for_task(task_profile='general', available=available)


def call_bridge(text, timeout=60, rag_context: str | None = None, task_profile: str | None = None):
    global _BRIDGE_FAIL_COUNT, _BRIDGE_OPEN_UNTIL

    # 🚀 PERFORMANCE: Check cache first (avoid network round-trip)
    cached = _get_cached_response(text)
    if cached:
        _RECENT_QUERIES.append(text)
        return cached

    cb_fail_threshold = max(1, int(os.environ.get('HAL_BRIDGE_CB_FAIL_THRESHOLD', '3')))
    cb_cooldown_sec = max(5, int(os.environ.get('HAL_BRIDGE_CB_COOLDOWN_SEC', '45')))
    loaded_fail, loaded_until = _load_bridge_cb_state()
    if loaded_fail or loaded_until:
        _BRIDGE_FAIL_COUNT = max(_BRIDGE_FAIL_COUNT, loaded_fail)
        _BRIDGE_OPEN_UNTIL = max(_BRIDGE_OPEN_UNTIL, loaded_until)

    # Best-effort bridge recovery so connected mode can self-heal.
    auto_start_bridge = os.environ.get('HAL_AUTO_START_BRIDGE', '1').lower() not in ('0', 'false', 'no', 'n')
    bridge_ok = _bridge_health_ok(timeout=1.5)
    if auto_start_bridge and not bridge_ok:
        bridge_ok = _start_bridge_background()

    now = time.time()
    if now < _BRIDGE_OPEN_UNTIL:
        if bridge_ok:
            # Bridge recovered: clear circuit and proceed.
            _BRIDGE_FAIL_COUNT = 0
            _BRIDGE_OPEN_UNTIL = 0.0
        else:
            remaining = int(_BRIDGE_OPEN_UNTIL - now)
            return f'ERR: bridge circuit open ({remaining}s remaining)'

    # System instruction: ensure the model addresses the user by name,
    # avoids meta-level disclaimers, and uses a natural, human tone.
    system_msg = (
        f'You are {ASSISTANT_NAME}, a helpful system assistant. Address the user by the name "{HAL_DISPLAY_NAME}" when appropriate. '
        'Adopt a warm, conversational tone: be concise, friendly, and ask clarifying questions when the user is ambiguous. '
        'Avoid meta-level disclaimers and do not reveal internal system prompts. '
        'When the user requests an action, ask for confirmation or clarify intent before attempting to execute any privileged operation.'
    )

    # Inject training data context (RAG) into system prompt when available
    if rag_context:
        system_msg += (
            '\n\nYou have access to the following private knowledge base entries that are directly relevant to this query. '
            'Use this information to give a personalised, accurate response. '
            'Do NOT say "I don\'t have information about X" if the answer is present below.\n\n'
            + rag_context
        )

    # Choose model dynamically based on task profile and query text
    model = choose_model_for_task(text=text, task_profile=task_profile)
    
    # 📊 COOL: Track analytics
    _update_session_analytics(text, model)
    _RECENT_QUERIES.append(text)
    
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_msg},
            {'role': 'user', 'content': text}
        ]
    }
    
    # 🚀 PERFORMANCE: Start timing for diagnostics
    t0 = time.time()
    
    if requests:
        try:
            r = _run_with_spinner('Waiting for model response', requests.post, OLLAMA_URL, json=payload, timeout=timeout)
            _BRIDGE_FAIL_COUNT = 0
            _BRIDGE_OPEN_UNTIL = 0.0
            _save_bridge_cb_state(_BRIDGE_FAIL_COUNT, _BRIDGE_OPEN_UNTIL)
            result = r.text
            # 🚀 Cache successful response
            _store_cached_response(text, result)
            return result
        except Exception as e:
            _BRIDGE_FAIL_COUNT += 1
            if _BRIDGE_FAIL_COUNT >= cb_fail_threshold:
                _BRIDGE_OPEN_UNTIL = time.time() + cb_cooldown_sec
            _save_bridge_cb_state(_BRIDGE_FAIL_COUNT, _BRIDGE_OPEN_UNTIL)
            return f'ERR: {e}'
    # fallback to urllib
    try:
        import urllib.request
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(OLLAMA_URL, data=data, headers={'Content-Type': 'application/json'})
        with _run_with_spinner('Waiting for model response', urllib.request.urlopen, req, timeout=timeout) as resp:
            _BRIDGE_FAIL_COUNT = 0
            _BRIDGE_OPEN_UNTIL = 0.0
            _save_bridge_cb_state(_BRIDGE_FAIL_COUNT, _BRIDGE_OPEN_UNTIL)
            result = resp.read().decode('utf-8')
            # 🚀 Cache successful response
            _store_cached_response(text, result)
            return result
    except Exception as e:
        _BRIDGE_FAIL_COUNT += 1
        if _BRIDGE_FAIL_COUNT >= cb_fail_threshold:
            _BRIDGE_OPEN_UNTIL = time.time() + cb_cooldown_sec
        _save_bridge_cb_state(_BRIDGE_FAIL_COUNT, _BRIDGE_OPEN_UNTIL)
        return f'ERR: {e}'


def _parse_tool_call(resp_text):
    """Attempt to extract a tool call dict from the model response.
    Returns (tool_name, arguments) or (None, None).
    """
    try:
        j = json.loads(resp_text)
        # common bridge wrapper: message.content contains a JSON string
        content = None
        if isinstance(j, dict):
            if 'message' in j and isinstance(j['message'], dict) and 'content' in j['message']:
                content = j['message']['content']
            elif 'choices' in j and isinstance(j['choices'], list) and len(j['choices']) > 0:
                c = j['choices'][0]
                if isinstance(c, dict) and 'message' in c and isinstance(c['message'], dict) and 'content' in c['message']:
                    content = c['message']['content']
            elif 'content' in j:
                content = j['content']

            if content:
                try:
                    tool = json.loads(content)
                    if isinstance(tool, dict) and 'name' in tool:
                        return tool.get('name'), tool.get('arguments', {})
                except Exception:
                    # fallthrough to regex extraction
                    pass
        # top-level tool shape
        if isinstance(j, dict) and 'name' in j:
            return j.get('name'), j.get('arguments', {})
    except Exception:
        pass

    # fallback: find first JSON-like object in the text
    import re
    m = re.search(r"(\{[\s\S]*\})", resp_text)
    if m:
        try:
            tool = json.loads(m.group(1))
            if isinstance(tool, dict) and 'name' in tool:
                return tool.get('name'), tool.get('arguments', {})
        except Exception:
            pass

    return None, None


def _exec_local_tool(fullname, arguments):
    """Execute a local tool implemented in server.py by name.
    `fullname` may be namespaced (e.g., architect.predict_failure_and_evacuate).
    """
    func_name = fullname.split('.')[-1]
    try:
        import importlib
        import server as _server
        importlib.reload(_server)
        func = getattr(_server, func_name)
    except Exception as e:
        return f'ERR: failed to import/find tool {fullname}: {e}'

    # normalize arguments
    try:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        # call the function
        try:
            res = _run_with_spinner(
                f"Running local tool {func_name}",
                (lambda: func(**arguments) if arguments else func())
            )
        except TypeError:
            # maybe function expects no kwargs
            res = _run_with_spinner(f"Running local tool {func_name}", func)
        return str(res)
    except Exception as e:
        return f'ERR: tool execution failed: {e}'


def _is_greeting(text: str) -> bool:
    """Return True when the user input appears to be a simple greeting or a 'how are you' query.
    This is used to short-circuit the LLM for trivial greetings so HAL replies with a
    concise, human-friendly response (e.g. "Hello Dave, How can I help you today").
    """
    if not text:
        return False
    s = text.strip().lower()
    # Only treat very short, classic greetings as trivial (e.g., "hi", "hello", "hey").
    # Do NOT treat 'how are you' or 'how are you feeling' as trivial — those should be
    # forwarded to the LLM when available so HAL can produce a fuller response.
    if re.fullmatch(r"(hi|hello|hey|greetings|yo)([!.]*)", s):
        return True
    words = s.split()
    if len(words) <= 3 and re.search(r"\b(hi|hello|hey|greetings)\b", s):
        return True
    return False


def extract_assistant_content(resp_text: str) -> str | None:
    """Try to extract the assistant's textual reply from a bridge JSON wrapper.

    Returns the assistant content string or None if extraction fails.
    """
    if not resp_text:
        return None
    try:
        j = json.loads(resp_text)
    except Exception:
        j = None

    content = None
    if isinstance(j, dict):
        # Common wrapper shapes
        if 'message' in j and isinstance(j['message'], dict):
            msg = j['message']
            if 'content' in msg:
                content = msg['content']
            elif 'text' in msg:
                content = msg['text']
        # choices -> message -> content
        if content is None and 'choices' in j and isinstance(j['choices'], list) and len(j['choices']) > 0:
            c = j['choices'][0]
            if isinstance(c, dict):
                if 'message' in c and isinstance(c['message'], dict) and 'content' in c['message']:
                    content = c['message']['content']
                elif 'content' in c:
                    content = c['content']
                elif 'text' in c:
                    content = c['text']
        if content is None and 'content' in j:
            content = j['content']

    # If content itself is a JSON string, try to unwrap inner content
    if isinstance(content, str):
        try:
            inner = json.loads(content)
            if isinstance(inner, dict):
                if 'content' in inner:
                    return inner['content']
                if 'message' in inner and isinstance(inner['message'], dict) and 'content' in inner['message']:
                    return inner['message']['content']
        except Exception:
            pass
        return content

    # Last-ditch regex to pull a content value out of the JSON text
    m = re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', resp_text)
    if m:
        try:
            # unescape
            return bytes(m.group(1), 'utf-8').decode('unicode_escape')
        except Exception:
            return m.group(1)

    return None


def _diag_severity_rank(sev: str) -> int:
    s = (sev or 'info').strip().lower()
    order = {'critical': 4, 'high': 3, 'warning': 2, 'warn': 2, 'medium': 2, 'info': 1, 'low': 1}
    return order.get(s, 1)


def _collect_diag_findings(diag_json) -> list[dict]:
    findings = []
    if not isinstance(diag_json, dict):
        return findings

    for section in ('hardware', 'security'):
        recs = diag_json.get(section, [])
        if not isinstance(recs, list):
            continue
        for r in recs:
            if not isinstance(r, dict):
                continue
            severity = str(r.get('severity') or 'Info').strip()
            title = str(r.get('title') or 'Untitled finding').strip()
            remediations = r.get('remediations') if isinstance(r.get('remediations'), list) else []
            action = str(remediations[0]).strip() if remediations else 'Review this finding and remediate as appropriate.'
            findings.append({
                'section': section,
                'severity': severity,
                'title': title,
                'action': action,
            })

    findings.sort(key=lambda x: _diag_severity_rank(x.get('severity', 'info')), reverse=True)
    return findings


def _render_health_summary_menu(diag_json) -> str:
    findings = _collect_diag_findings(diag_json)
    if not findings:
        return 'No issues detected in local diagnostics.'

    crit = sum(1 for f in findings if _diag_severity_rank(f['severity']) >= 4)
    warn = sum(1 for f in findings if _diag_severity_rank(f['severity']) == 2)
    info = sum(1 for f in findings if _diag_severity_rank(f['severity']) <= 1)

    host = (diag_json.get('host') if isinstance(diag_json, dict) else None) or 'unknown-host'
    ts = (diag_json.get('timestamp') if isinstance(diag_json, dict) else None) or 'unknown-time'

    lines = []
    lines.append('HAL Health Check')
    lines.append('-' * 48)
    lines.append(f'Host: {host}')
    lines.append(f'Time: {ts}')
    lines.append(f'Findings: {len(findings)} total  |  Critical: {crit}  Warning: {warn}  Info: {info}')
    lines.append('')
    lines.append('Top issues:')

    for i, f in enumerate(findings[:3], start=1):
        lines.append(f"  {i}. [{f['severity']}] {f['title']}")
        lines.append(f"     Action: {f['action']}")

    lines.append('')
    lines.append('Menu:')
    lines.append('  1) Quick summary (default)')
    lines.append('  2) Recommended actions only')
    lines.append('  3) Full raw diagnostics (JSON)')
    lines.append('  4) AI short summary (plain text)')
    lines.append('  5) Fix all! (run full auto-remediation)')
    return '\n'.join(lines)


def _render_health_actions(diag_json) -> str:
    findings = _collect_diag_findings(diag_json)
    if not findings:
        return 'No actions required. System diagnostics returned no findings.'

    lines = ['Recommended actions:']
    for i, f in enumerate(findings[:8], start=1):
        lines.append(f"  {i}. [{f['severity']}] {f['action']}")
    return '\n'.join(lines)


def _choose_health_output_mode() -> str:
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return '1'
        choice = input('\nChoose output [1/2/3/4/5] (default 1): ').strip()
        return choice if choice in {'1', '2', '3', '4', '5'} else '1'
    except Exception:
        return '1'


def _choose_health_remediation_mode() -> str:
    """Choose what to do after health report: report only, guided fixes, or full auto-remediation."""
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return '1'
        print('\nRemediation options:')
        print('  1) Report only (no changes)')
        print('  2) Show guided fixes (LLM + MCP plan)')
        print('  3) Full auto-remediation (let HAL fix what it can)')
        choice = input('Choose remediation [1/2/3] (default 1): ').strip()
        return choice if choice in {'1', '2', '3'} else '1'
    except Exception:
        return '1'


def _repair_user_shortcuts() -> tuple[list[str], list[str]]:
    """Repair common desktop/file-manager shortcut targets under the user's home directory."""
    repaired = []
    errors = []

    home = os.path.expanduser('~')
    common_dirs = ['Desktop', 'Documents', 'Downloads', 'Music', 'Pictures', 'Videos']
    for name in common_dirs:
        path = os.path.join(home, name)
        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
                repaired.append(f'created {path}')
            except Exception as e:
                errors.append(f'failed to create {path}: {e}')

    bookmarks = os.path.join(home, '.config', 'gtk-3.0', 'bookmarks')
    if os.path.isfile(bookmarks):
        try:
            with open(bookmarks, 'r', encoding='utf-8', errors='replace') as fh:
                lines = fh.readlines()
            changed = False
            for raw in lines:
                token = raw.strip().split(' ')[0] if raw.strip() else ''
                if token.startswith('file://'):
                    p = token.replace('file://', '', 1)
                    if p.startswith(home) and not os.path.exists(p):
                        os.makedirs(p, exist_ok=True)
                        repaired.append(f'created missing bookmark target {p}')
                        changed = True
            if changed:
                with open(bookmarks, 'w', encoding='utf-8') as fh:
                    fh.writelines(lines)
        except Exception as e:
            errors.append(f'failed to process bookmarks: {e}')

    xdg = os.path.join(home, '.config', 'user-dirs.dirs')
    if os.path.isfile(xdg):
        try:
            with open(xdg, 'r', encoding='utf-8', errors='replace') as fh:
                text = fh.read()
            new_text = text
            new_text = re.sub(r'^XDG_MUSIC_DIR=.*$', 'XDG_MUSIC_DIR="$HOME/Music"', new_text, flags=re.MULTILINE)
            new_text = re.sub(r'^XDG_VIDEOS_DIR=.*$', 'XDG_VIDEOS_DIR="$HOME/Videos"', new_text, flags=re.MULTILINE)
            if new_text != text:
                with open(xdg, 'w', encoding='utf-8') as fh:
                    fh.write(new_text)
                repaired.append('updated XDG music/videos shortcuts')
        except Exception as e:
            errors.append(f'failed to update user-dirs.dirs: {e}')

    return repaired, errors


def _repair_ssh_keys() -> tuple[list[str], list[str]]:
    """Repair SSH key permissions and validate known_hosts file."""
    repaired = []
    errors = []

    home = os.path.expanduser('~')
    ssh_dir = os.path.join(home, '.ssh')
    
    if os.path.isdir(ssh_dir):
        # Fix SSH directory permissions (should be 700)
        try:
            stat = os.stat(ssh_dir)
            if stat.st_mode & 0o777 != 0o700:
                os.chmod(ssh_dir, 0o700)
                repaired.append('.ssh directory permissions fixed to 700')
        except Exception as e:
            errors.append(f'failed to fix .ssh directory perms: {e}')
        
        # Fix private key permissions (should be 600)
        for key_file in os.listdir(ssh_dir):
            if key_file.startswith('id_'):
                key_path = os.path.join(ssh_dir, key_file)
                try:
                    stat = os.stat(key_path)
                    if stat.st_mode & 0o777 != 0o600:
                        os.chmod(key_path, 0o600)
                        repaired.append(f'{key_file} permissions fixed to 600')
                except Exception as e:
                    errors.append(f'failed to fix {key_file} perms: {e}')
        
        # Validate/create known_hosts
        known_hosts = os.path.join(ssh_dir, 'known_hosts')
        if not os.path.isfile(known_hosts):
            try:
                open(known_hosts, 'a').close()
                os.chmod(known_hosts, 0o644)
                repaired.append('known_hosts file created with correct permissions')
            except Exception as e:
                errors.append(f'failed to create known_hosts: {e}')

    return repaired, errors


def _repair_mcp_ai_permissions() -> tuple[list[str], list[str]]:
    """Repair .mcp-ai directory and file permissions, clean stale cache."""
    repaired = []
    errors = []

    home = os.path.expanduser('~')
    ai_home = os.path.join(home, '.mcp-ai')

    if os.path.isdir(ai_home):
        # Fix main .mcp-ai directory permissions (should be 755)
        try:
            os.chmod(ai_home, 0o755)
            repaired.append('.mcp-ai directory permissions fixed')
        except Exception as e:
            errors.append(f'failed to fix .mcp-ai perms: {e}')

        # Clean stale training data (> 30 days old)
        train_dir = os.path.join(ai_home, 'training')
        if os.path.isdir(train_dir):
            try:
                now = time.time()
                cleaned = 0
                for fname in os.listdir(train_dir):
                    fpath = os.path.join(train_dir, fname)
                    if os.path.isfile(fpath):
                        mtime = os.path.getmtime(fpath)
                        if (now - mtime) > (30 * 86400):  # 30 days
                            try:
                                os.remove(fpath)
                                cleaned += 1
                            except Exception:
                                pass
                if cleaned > 0:
                    repaired.append(f'removed {cleaned} stale training files')
            except Exception as e:
                errors.append(f'failed to clean training dir: {e}')

        # Fix reports directory
        reports_dir = os.path.join(ai_home, 'reports')
        if os.path.isdir(reports_dir):
            try:
                for fname in os.listdir(reports_dir):
                    fpath = os.path.join(reports_dir, fname)
                    os.chmod(fpath, 0o644)
                repaired.append('reports directory permissions corrected')
            except Exception:
                pass

    return repaired, errors


def _repair_venv_permissions() -> tuple[list[str], list[str]]:
    """Repair Python virtual environment executable permissions."""
    repaired = []
    errors = []

    base_dir = os.path.dirname(os.path.realpath(__file__))
    venv_bin = os.path.join(base_dir, 'venv-bridge', 'bin')
    
    if os.path.isdir(venv_bin):
        try:
            for fname in os.listdir(venv_bin):
                fpath = os.path.join(venv_bin, fname)
                if os.path.isfile(fpath):
                    stat = os.stat(fpath)
                    # Add execute permission if text-based executable
                    if stat.st_size < 1000000 and stat.st_mode & 0o111 == 0:
                        try:
                            os.chmod(fpath, stat.st_mode | 0o111)
                        except Exception:
                            pass
            repaired.append('venv-bridge/bin executable permissions validated')
        except Exception as e:
            errors.append(f'failed to check venv perms: {e}')

    return repaired, errors


def _validate_critical_configs() -> tuple[list[str], list[str]]:
    """Validate and repair critical config files."""
    repaired = []
    errors = []

    home = os.path.expanduser('~')
    
    # Check mcp-config.json
    base_dir = os.path.dirname(os.path.realpath(__file__))
    mcp_config = os.path.join(base_dir, 'mcp-config.json')
    if os.path.isfile(mcp_config):
        try:
            with open(mcp_config, 'r') as f:
                json.load(f)
            repaired.append('mcp-config.json is valid JSON')
        except Exception as e:
            errors.append(f'mcp-config.json parse error: {e}')
    
    # Check .bashrc integrity
    bashrc = os.path.join(home, '.bashrc')
    if os.path.isfile(bashrc):
        try:
            with open(bashrc, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
                if len(content) > 0:
                    repaired.append('.bashrc file readable')
        except Exception as e:
            errors.append(f'failed to read .bashrc: {e}')
    
    # Check .ssh/config
    ssh_config = os.path.join(home, '.ssh', 'config')
    if os.path.isfile(ssh_config):
        try:
            stat = os.stat(ssh_config)
            if stat.st_mode & 0o777 != 0o600:
                os.chmod(ssh_config, 0o600)
                repaired.append('.ssh/config permissions fixed to 600')
        except Exception as e:
            errors.append(f'failed to fix .ssh/config: {e}')

    return repaired, errors


def _repair_broken_symlinks() -> tuple[list[str], list[str]]:
    """Find and report broken symlinks in home directory."""
    repaired = []
    errors = []

    home = os.path.expanduser('~')
    checked = 0
    broken = 0
    
    # Scan home for symlinks (shallow, not recursive to keep it fast)
    try:
        for entry in os.listdir(home):
            path = os.path.join(home, entry)
            if os.path.islink(path):
                checked += 1
                target = os.path.realpath(path)
                if not os.path.exists(target):
                    broken += 1
                    errors.append(f'broken symlink: {entry} -> {target}')
    except Exception as e:
        errors.append(f'failed to scan home symlinks: {e}')
    
    if checked > 0 and broken == 0:
        repaired.append(f'checked {checked} symlinks, all valid')
    elif checked > 0:
        repaired.append(f'checked {checked} symlinks, found {broken} broken')

    return repaired, errors


def _check_locale_settings() -> tuple[list[str], list[str]]:
    """Validate and report locale/encoding settings."""
    repaired = []
    errors = []

    try:
        locale_result = subprocess.run(['locale'], capture_output=True, text=True, timeout=5)
        if locale_result.returncode == 0:
            lines = locale_result.stdout.strip().split('\n')
            has_utf8 = any('UTF-8' in line or 'utf8' in line for line in lines)
            if has_utf8:
                repaired.append('locale configured with UTF-8 encoding')
            else:
                errors.append('locale may not have UTF-8 encoding set')
    except Exception as e:
        errors.append(f'failed to check locale: {e}')

    return repaired, errors


def _repair_selinux_errors() -> tuple[list[str], list[str]]:
    """Check and repair SELinux errors, restore contexts."""
    repaired = []
    errors = []

    try:
        # Check if SELinux is available and enabled
        getenforce_result = subprocess.run(['getenforce'], capture_output=True, text=True, timeout=5)
        if getenforce_result.returncode != 0:
            errors.append('SELinux tools not available')
            return repaired, errors

        selinux_status = getenforce_result.stdout.strip()
        if selinux_status == 'Disabled':
            repaired.append('SELinux is disabled (no action needed)')
            return repaired, errors

        repaired.append(f'SELinux status: {selinux_status}')

        # Try to run restorecon on common directories if available
        if shutil.which('restorecon'):
            for dir_path in ['/home', '/opt', '/srv']:
                if os.path.isdir(dir_path):
                    try:
                        result = subprocess.run(
                            ['restorecon', '-RF', dir_path],
                            capture_output=True,
                            text=True,
                            timeout=30
                        )
                        if result.returncode == 0:
                            repaired.append(f'restorecon completed on {dir_path}')
                    except Exception:
                        pass

        # Check for recent SELinux denials (if ausearch available)
        if shutil.which('ausearch'):
            try:
                result = subprocess.run(
                    ['ausearch', '-m', 'avc', '-ts', 'recent'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                denial_count = result.stdout.count('avc:')
                if denial_count > 0:
                    errors.append(f'found {denial_count} recent SELinux denials (check with ausearch)')
                else:
                    repaired.append('no recent SELinux denials detected')
            except Exception:
                repaired.append('unable to check recent SELinux denials')

    except Exception as e:
        errors.append(f'failed to check SELinux: {e}')

    return repaired, errors


def _repair_firewall_config() -> tuple[list[str], list[str]]:
    """Validate and update firewall configuration."""
    repaired = []
    errors = []

    try:
        # Check if firewalld is available
        if not shutil.which('firewall-cmd'):
            repaired.append('firewalld not installed (no action needed)')
            return repaired, errors

        # Check if firewalld service is running
        status_result = subprocess.run(
            ['systemctl', 'is-active', 'firewalld'],
            capture_output=True,
            text=True,
            timeout=5
        )

        if status_result.returncode != 0:
            errors.append('firewalld service is not running')
            return repaired, errors

        repaired.append('firewalld service is active')

        # Reload firewall configuration
        try:
            reload_result = subprocess.run(
                ['firewall-cmd', '--reload'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if reload_result.returncode == 0:
                repaired.append('firewall configuration reloaded')
            else:
                errors.append('firewall reload encountered errors')
        except Exception as e:
            errors.append(f'failed to reload firewall: {e}')

        # Verify zones are healthy
        try:
            zones_result = subprocess.run(
                ['firewall-cmd', '--get-zones'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if zones_result.returncode == 0:
                zones = zones_result.stdout.strip().split()
                repaired.append(f'firewall has {len(zones)} active zone(s)')
            else:
                errors.append('unable to list firewall zones')
        except Exception:
            pass

    except Exception as e:
        errors.append(f'failed to check firewall: {e}')

    return repaired, errors


def _verify_auto_update_timer() -> tuple[list[str], list[str]]:
    """Verify HAL auto-update timer is properly configured."""
    repaired = []
    errors = []

    try:
        # Check if timer service exists
        timer_file = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            'hal-auto-update.timer'
        )
        if not os.path.isfile(timer_file):
            errors.append('hal-auto-update.timer file not found')
            return repaired, errors

        repaired.append('hal-auto-update.timer configuration file exists')

        # Try to check systemd timer status (if running with sudo)
        try:
            status_result = subprocess.run(
                ['systemctl', 'is-enabled', 'hal-auto-update.timer'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if status_result.returncode == 0:
                repaired.append('hal-auto-update.timer is enabled')
            else:
                errors.append('hal-auto-update.timer is not enabled (run: sudo systemctl enable hal-auto-update.timer)')
        except Exception:
            # Non-root, just note the file exists
            repaired.append('(timer status check requires sudo)')

    except Exception as e:
        errors.append(f'failed to verify auto-update timer: {e}')

    return repaired, errors


def _manage_vault_password() -> tuple[list[str], list[str]]:
    """Ensure ~/.ansible/conf/.vaultpass.txt exists with a secure password."""
    repaired = []
    errors = []

    try:
        home = os.path.expanduser('~')
        vault_dir = os.path.join(home, '.ansible', 'conf')
        vault_file = os.path.join(vault_dir, '.vaultpass.txt')

        # Create directory if needed
        if not os.path.isdir(vault_dir):
            try:
                os.makedirs(vault_dir, mode=0o700, exist_ok=True)
                repaired.append(f'created {vault_dir} with secure permissions')
            except Exception as e:
                errors.append(f'failed to create vault directory: {e}')
                return repaired, errors

        # Check if vault password file exists
        if os.path.isfile(vault_file):
            # Verify it has content
            try:
                with open(vault_file, 'r') as f:
                    content = f.read().strip()
                    if content:
                        repaired.append('vault password file already exists and contains a password')
                    else:
                        errors.append('vault password file exists but is empty')
            except Exception as e:
                errors.append(f'failed to read vault password: {e}')

            # Ensure correct permissions (600)
            try:
                stat = os.stat(vault_file)
                if stat.st_mode & 0o777 != 0o600:
                    os.chmod(vault_file, 0o600)
                    repaired.append('corrected vault password file permissions to 600')
            except Exception as e:
                errors.append(f'failed to fix vault password permissions: {e}')
        else:
            # Generate new secure password (32 random chars)
            import secrets
            import string
            password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))

            try:
                with open(vault_file, 'w') as f:
                    f.write(password)
                os.chmod(vault_file, 0o600)
                repaired.append(f'created new vault password at {vault_file}')
                repaired.append('vault password generated with 32 secure random characters')
            except Exception as e:
                errors.append(f'failed to create vault password file: {e}')

    except Exception as e:
        errors.append(f'failed to manage vault password: {e}')

    return repaired, errors


def _scan_git_repos_for_secrets() -> tuple[list[str], list[str]]:
    """Scan git repositories for common unencrypted secrets patterns."""
    repaired = []
    errors = []

    try:
        home = os.path.expanduser('~')
        secrets_found = []
        repos_scanned = 0

        # Common patterns that indicate secrets
        secret_patterns = [
            r'password\s*[:=]\s*["\']?\w+["\']?',
            r'api[_-]?key\s*[:=]\s*["\']?[a-zA-Z0-9]+["\']?',
            r'secret\s*[:=]\s*["\']?\w+["\']?',
            r'token\s*[:=]\s*["\']?[a-zA-Z0-9]+["\']?',
            r'aws[_-]?secret\s*[:=]',
            r'private[_-]?key',
            r'\.pem\s*$',
            r'\.key\s*$',
        ]

        # Find all git repositories (max 50 deep to avoid huge traversals)
        def find_git_repos(start_path, max_depth=3, current_depth=0):
            repos = []
            if current_depth >= max_depth:
                return repos
            try:
                for entry in os.listdir(start_path):
                    entry_path = os.path.join(start_path, entry)
                    if os.path.isdir(entry_path):
                        if entry in {'.git', 'git'}:
                            # Found a git repo
                            repos.append(os.path.dirname(entry_path))
                        elif not entry.startswith('.') and entry not in {'__pycache__', 'node_modules', '.venv', 'venv'}:
                            repos.extend(find_git_repos(entry_path, max_depth, current_depth + 1))
            except (PermissionError, OSError):
                pass
            return repos

        # Scan home directory for git repos (but not too deep)
        git_repos = find_git_repos(home, max_depth=4)
        repos_scanned = len(git_repos)

        # Scan each repo for secret patterns in tracked files
        for repo_path in git_repos[:10]:  # Limit to first 10 repos for performance
            try:
                # Get list of tracked files
                result = subprocess.run(
                    ['git', 'ls-files'],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode != 0:
                    continue

                tracked_files = result.stdout.strip().split('\n')
                for tracked_file in tracked_files[:100]:  # Check first 100 files
                    file_path = os.path.join(repo_path, tracked_file)
                    if not os.path.isfile(file_path):
                        continue

                    # Skip binary and large files
                    try:
                        stat = os.stat(file_path)
                        if stat.st_size > 1000000:  # > 1MB
                            continue
                    except OSError:
                        continue

                    # Scan file content
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            for pattern in secret_patterns:
                                if re.search(pattern, content, re.IGNORECASE):
                                    secrets_found.append(f'{tracked_file} (repo: {os.path.basename(repo_path)})')
                                    break
                    except (IOError, OSError):
                        pass

            except Exception:
                pass

        if repos_scanned > 0:
            repaired.append(f'scanned {repos_scanned} git repository(ies) for secrets')
        
        if secrets_found:
            errors.append(f'found {len(secrets_found)} file(s) with potential unencrypted secrets:')
            for secret_file in secrets_found[:5]:
                errors.append(f'  → {secret_file}')
            if len(secrets_found) > 5:
                errors.append(f'  ... and {len(secrets_found) - 5} more')
        else:
            repaired.append('no obvious unencrypted secrets detected in tracked files')

    except Exception as e:
        errors.append(f'failed to scan git repos for secrets: {e}')

    return repaired, errors


def _check_git_credentials_exposure() -> tuple[list[str], list[str]]:
    """Check for exposed git credentials in common locations."""
    repaired = []
    errors = []

    try:
        home = os.path.expanduser('~')
        checked_locations = []

        # Check .gitconfig
        gitconfig = os.path.join(home, '.gitconfig')
        if os.path.isfile(gitconfig):
            checked_locations.append('~/.gitconfig')
            try:
                with open(gitconfig, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    if 'password' in content.lower():
                        errors.append('.gitconfig contains plaintext password (consider using credential helper)')
                    elif 'token' in content.lower():
                        errors.append('.gitconfig contains potential plaintext token (consider using credential helper)')
                    else:
                        repaired.append('.gitconfig checked (no obvious credentials)')
            except Exception as e:
                errors.append(f'failed to check .gitconfig: {e}')

        # Check git credentials storage
        git_creds = os.path.join(home, '.git-credentials')
        if os.path.isfile(git_creds):
            checked_locations.append('~/.git-credentials')
            errors.append('~/.git-credentials file detected (should use credential helper instead)')

        # Check SSH key exposure in git config
        git_ssh = os.path.join(home, '.ssh', 'config')
        if os.path.isfile(git_ssh):
            try:
                with open(git_ssh, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    if 'identityfile' in content.lower() and '.pem' in content.lower():
                        repaired.append('SSH keys properly referenced in ~/.ssh/config')
            except Exception:
                pass

        if not checked_locations:
            repaired.append('no git config files detected (expected state)')

    except Exception as e:
        errors.append(f'failed to check git credentials: {e}')

    return repaired, errors


def _run_comprehensive_auto_healing() -> dict[str, tuple[list[str], list[str]]]:
    """Execute all auto-healing helpers and aggregate results."""
    results = {}
    
    results['ssh_keys'] = _run_with_spinner('Repairing SSH key permissions', _repair_ssh_keys)
    results['mcp_ai'] = _run_with_spinner('Repairing .mcp-ai permissions & cache', _repair_mcp_ai_permissions)
    results['venv'] = _run_with_spinner('Validating venv permissions', _repair_venv_permissions)
    results['configs'] = _run_with_spinner('Validating critical configs', _validate_critical_configs)
    results['symlinks'] = _run_with_spinner('Checking for broken symlinks', _repair_broken_symlinks)
    results['locale'] = _run_with_spinner('Checking locale settings', _check_locale_settings)
    results['selinux'] = _run_with_spinner('Checking & repairing SELinux', _repair_selinux_errors)
    results['firewall'] = _run_with_spinner('Checking firewall configuration', _repair_firewall_config)
    results['auto_update'] = _run_with_spinner('Verifying auto-update timer', _verify_auto_update_timer)
    results['vault'] = _run_with_spinner('Managing vault password', _manage_vault_password)
    results['git_secrets'] = _run_with_spinner('Scanning git repos for secrets', _scan_git_repos_for_secrets)
    results['git_creds'] = _run_with_spinner('Checking git credentials exposure', _check_git_credentials_exposure)
    results['thermal'] = _run_with_spinner('Checking CPU thermal / fan state', _repair_cpu_thermal)
    
    return results


def _repair_cpu_thermal() -> tuple[list[str], list[str]]:
    """Attempt to reduce CPU temperature: kill top CPU hogs, check fan/throttle state."""
    repaired = []
    errors = []

    # Read current CPU temperature from sensors
    try:
        import subprocess as _sp
        sens = _sp.run(['sensors', '-u'], capture_output=True, text=True, timeout=10)
        temps = []
        for line in (sens.stdout or '').splitlines():
            if 'temp' in line.lower() and 'input' in line.lower():
                try:
                    val = float(line.split(':', 1)[1].strip())
                    temps.append(val)
                except Exception:
                    pass
        if temps:
            peak = max(temps)
            repaired.append(f'CPU peak temperature: {peak:.0f}°C')
            if peak >= 90:
                # Kill the top CPU-consuming user processes (excluding system)
                ps = _sp.run(
                    ['ps', '-eo', 'pid,comm,%cpu', '--sort=-%cpu', '--no-headers'],
                    capture_output=True, text=True, timeout=10
                )
                killed = []
                for line in (ps.stdout or '').splitlines()[:5]:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    pid_str, comm, cpu_pct = parts[0], parts[1], parts[2]
                    try:
                        if float(cpu_pct) < 20:
                            break
                        # Skip critical system processes
                        if comm in ('systemd', 'kthreadd', 'ksoftirqd', 'kworker',
                                    'migration', 'rcu_sched', 'sshd', 'python', 'python3',
                                    'hal', 'hal.py'):
                            continue
                        _sp.run(['kill', '-15', pid_str], timeout=5)
                        killed.append(f'{comm} (PID {pid_str}, {cpu_pct}% CPU)')
                    except Exception:
                        pass
                if killed:
                    repaired.append(f'Sent SIGTERM to high-CPU processes: {", ".join(killed)}')
                else:
                    repaired.append('No high-CPU user processes to terminate (system load from kernel threads)')
                # Suggest thermal check commands
                repaired.append(
                    'Tip: Run `ps -eo pid,ppid,cmd,%cpu --sort=-%cpu | head` to see current load'
                )
                repaired.append(
                    'Tip: Check fan with `sensors | grep fan` — if fans are stalled, clean vents or migrate workloads'
                )
            else:
                repaired.append(f'Temperature {peak:.0f}°C is within normal range (< 90°C)')
    except Exception as e:
        errors.append(f'thermal check failed: {e}')

    # Check if thermald/power-profiles-daemon can help
    try:
        import subprocess as _sp
        for svc in ('thermald', 'power-profiles-daemon', 'cpupower'):
            st = _sp.run(['systemctl', 'is-active', svc], capture_output=True, text=True, timeout=5)
            if st.stdout.strip() == 'active':
                repaired.append(f'{svc} is active (thermal management enabled)')
            elif st.stdout.strip() == 'inactive':
                errors.append(f'{svc} is installed but inactive — consider enabling it')
    except Exception:
        pass

    return repaired, errors


def _execute_health_remediation(user_query: str, diag_json, entry_path: str, mode: str) -> None:
    """Run guided or full remediation after health check, depending on user selection."""
    if mode not in {'2', '3'}:
        return

    diag_snippet = json.dumps(diag_json, indent=2)[:4000] if isinstance(diag_json, dict) else str(diag_json)
    plan_prompt = (
        f"User requested remediation mode {mode} for: {user_query}\n"
        f"Diagnostics snapshot:\n{diag_snippet}\n\n"
        "Create a concise remediation plan prioritized by impact and safety. "
        "Include immediate fixes, performance improvements, and validation checks. "
        "Return plain text only with numbered steps."
    )
    plan_raw = _run_with_spinner('Building remediation plan', call_bridge, plan_prompt)
    plan_text = extract_assistant_content(plan_raw) or 'Unable to generate remediation plan from LLM.'

    print('\nRemediation plan:\n')
    print(plan_text)

    try:
        append_feedback(entry_path, f'health_remediation_mode={mode}\n{plan_text[:6000]}')
    except Exception:
        pass

    if mode == '3':
        print('\nStarting full remediation...')

        # Phase 1: Desktop/shortcut healing
        repaired, repair_errors = _run_with_spinner('Repairing user shortcuts/folders', _repair_user_shortcuts)
        if repaired:
            print('\nShortcut/folder repairs applied:')
            for r in repaired[:10]:
                print(f'  - {r}')
        if repair_errors:
            print('\nShortcut/folder repair warnings:')
            for e in repair_errors[:10]:
                print(f'  - {e}')

        # Phase 2: Comprehensive system-level healing
        print('\n--- System-level auto-healing suite ---')
        healing_results = _run_comprehensive_auto_healing()
        
        # Aggregate and display results
        total_repaired = 0
        total_warnings = 0
        for category, (repaired_items, error_items) in healing_results.items():
            total_repaired += len(repaired_items)
            total_warnings += len(error_items)
            if repaired_items:
                print(f'\n[{category.upper()}] Repairs applied:')
                for item in repaired_items[:5]:
                    print(f'  ✓ {item}')
            if error_items:
                print(f'\n[{category.upper()}] Notices:')
                for item in error_items[:3]:
                    print(f'  ⓘ {item}')
        
        print(f'\n--- Auto-healing summary: {total_repaired} repairs, {total_warnings} notices ---')

        # Phase 3: Best-effort extra MCP context before execution.
        try:
            _run_with_spinner('Collecting MCP doctor report', _exec_local_tool, 'architect.mcp_doctor_report', {})
        except Exception:
            pass

        _run_with_spinner('Executing remediator', invoke_remediator, entry_path, True)

        # Post-remediation verification snapshot.
        try:
            post_diag = _run_with_spinner(
                'Running post-remediation health check',
                _exec_local_tool,
                'architect.full_diagnostics_json',
                {}
            )
            post_json = None
            try:
                post_json = json.loads(post_diag)
            except Exception:
                post_json = None

            if isinstance(post_json, dict):
                post_findings = _collect_diag_findings(post_json)
                print(f"\nPost-remediation summary: {len(post_findings)} finding(s) currently detected.")
            else:
                print('\nPost-remediation summary: diagnostics completed.')
        except Exception:
            print('\nPost-remediation summary: unable to run verification checks.')


def _is_low_quality_assistant_text(text: str) -> bool:
    """Detect trivial/placeholder model outputs that should be rejected."""
    if not text:
        return True

    s = text.strip()
    if not s:
        return True

    # Placeholder markdown or punctuation-only fragments (e.g., "###", "...", "-").
    if re.fullmatch(r'[\s#\-_.:;,*`~|]+', s):
        return True

    lower = s.lower()
    if lower in {'hello', 'hi', 'hey', 'sure', 'ok', 'okay', 'yes'}:
        return True

    words = re.findall(r'\b\w+\b', lower)
    if len(words) < 8:
        return True

    return False


def _is_operational_howto_query(query: str) -> bool:
    """Detect operational setup/patch/troubleshoot/integrate style asks."""
    if not query:
        return False
    q = query.lower()
    has_action = bool(re.search(
        r'\b(set\s*up|setup|install|configure|deploy|patch|update|upgrade|migrate|troubleshoot|fix|repair|integrate|connect|rollback|recover|harden)\b',
        q,
    ))
    has_howto = bool(re.search(r'\b(how\s+do\s+i|how\s+to|runbook|steps?|plan|strategy|best\s+way)\b', q))
    has_product = bool(re.search(r'\b(rhel|satellite|aap|ansible|idm|freeipa|openshift|mcp|server|linux)\b', q))
    return has_action and (has_howto or has_product)


def _infer_primary_product(query: str) -> str:
    q = (query or '').lower()
    if re.search(r'\b(idm|freeipa|identity\s+management)\b', q):
        return 'Red Hat IdM'
    if re.search(r'\b(aap|ansible\s+automation\s+platform|automation\s+controller)\b', q):
        return 'AAP'
    if re.search(r'\b(satellite|capsule|katello|foreman)\b', q):
        return 'Satellite'
    if re.search(r'\b(openshift|ocp)\b', q):
        return 'OpenShift'
    if re.search(r'\b(rhel|red\s*hat\s*enterprise\s*linux|linux)\b', q):
        return 'RHEL'
    if re.search(r'\b(mcp|model\s+context\s+protocol)\b', q):
        return 'MCP'
    return 'Platform'


def _extract_rag_sources(rag_context: str | None) -> list[str]:
    if not rag_context:
        return []
    found = re.findall(r'\[([^\]]+)\]\s*:', rag_context)
    # Keep unique order and hide noisy interaction record names.
    unique = []
    seen = set()
    for s in found:
        sl = s.lower().strip()
        if sl in seen:
            continue
        seen.add(sl)
        if sl.startswith('hal-') and sl.endswith('.jsonl'):
            continue
        unique.append(s.strip())
    return unique


def _estimate_rag_confidence(query: str, rag_context: str | None) -> str:
    if not rag_context:
        return 'low'
    sources = _extract_rag_sources(rag_context)
    if not sources:
        return 'low'
    q_words = [w for w in re.findall(r'\b[a-z0-9]+\b', (query or '').lower()) if len(w) > 3]
    overlap = 0
    rag_low = rag_context.lower()
    for w in set(q_words):
        if w in rag_low:
            overlap += 1
    if len(sources) >= 3 and overlap >= 3:
        return 'high'
    if len(sources) >= 1 and overlap >= 1:
        return 'medium'
    return 'low'


def _preferred_doc_source_patterns(query: str) -> list[str]:
    """Return source substrings that should be preferred for explicit product/version doc queries."""
    if not query:
        return []

    q = query.lower()
    patterns = []
    if re.search(r'\b(satellite|red\s*hat\s*satellite)\b', q) and re.search(r'\b6\.18\b', q):
        patterns.append('docs.redhat.com/en/documentation/red_hat_satellite/6.18')
    if re.search(r'\b(aap|ansible\s+automation\s+platform|red\s+hat\s+ansible\s+automation\s+platform)\b', q) and re.search(r'\b2\.6\b', q):
        patterns.append('docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6')
        if re.search(r'\b(event\s*[- ]?driven\s+ansible|automation\s+decisions|eda)\b', q):
            patterns.append('using_automation_decisions')
    if re.search(r'\b(idm|identity\s+management|freeipa)\b', q) and re.search(r'\b(5\.0|rhel\s*10|10)\b', q):
        patterns.append('docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/')
        patterns.append('identity_management')
    return patterns

def _build_generic_operational_runbook(query: str, rag_context: str | None) -> str:
    """Build a deterministic fallback runbook when LLM output quality is poor."""
    q = (query or '').lower()
    product = _infer_primary_product(query)
    action = 'Operational'
    if re.search(r'\b(patch|update|upgrade)\b', q):
        action = 'Patch/Upgrade'
    elif re.search(r'\b(set\s*up|setup|install|deploy|configure)\b', q):
        action = 'Setup'
    elif re.search(r'\b(troubleshoot|fix|repair|recover)\b', q):
        action = 'Troubleshooting'
    elif re.search(r'\b(integrate|connect|integration)\b', q):
        action = 'Integration'

    confidence = _estimate_rag_confidence(query, rag_context)
    sources = _extract_rag_sources(rag_context)

    lines = []
    lines.append(f'{product} {action} Runbook (Deterministic Fallback)')
    lines.append('=' * 72)
    lines.append('1. Preconditions')
    lines.append('- Confirm target version, topology, and maintenance window.')
    lines.append('- Validate access, DNS/FQDN, certificates, and time synchronization.')
    lines.append('- Ensure tested backup/restore path exists before changes.')
    lines.append('')
    lines.append('2. Execution Plan')
    lines.append('- Start in non-production and run a smoke-test checklist for critical workflows.')
    lines.append('- Apply changes in phases with approval checkpoints between phases.')
    lines.append('- Capture command output and logs for auditability.')
    lines.append('')
    lines.append('3. Validation')
    lines.append('- Verify health endpoints/services and core user workflows.')
    lines.append('- Confirm security and policy behavior (auth, RBAC/HBAC/sudo where applicable).')
    lines.append('- Compare post-change behavior against baseline metrics.')
    lines.append('')
    lines.append('4. Rollback Criteria')
    lines.append('- Roll back on repeated critical failures, service instability, or policy regressions.')
    lines.append('- Use documented restore sequence and communicate status quickly.')
    lines.append('')
    lines.append('5. Next Actions')
    lines.append('- Produce a final change report with outcomes, risks, and follow-up actions.')
    lines.append('- Convert validated steps into repeatable automation/workflow jobs.')
    lines.append('')
    lines.append('Grounding:')
    lines.append(f'- Confidence: {confidence}')
    if sources:
        lines.append(f'- Sources: {", ".join(sources[:5])}')
    else:
        lines.append('- Sources: none matched in local knowledge base')
    return '\n'.join(lines)


def _parse_rag_context_entries(rag_context: str | None) -> list[tuple[str, str]]:
    """Parse RAG context bullet lines into (source, snippet) tuples."""
    if not rag_context:
        return []

    entries = []
    for raw_line in rag_context.splitlines():
        line = raw_line.strip()
        if not line.startswith('• ['):
            continue
        m = re.match(r'^• \[([^\]]+)\]:\s*(.*)$', line)
        if not m:
            continue
        source = m.group(1).strip()
        snippet = m.group(2).strip()
        entries.append((source, snippet))
    return entries


def _build_offline_doc_summary(query: str, rag_context: str | None) -> str:
    """Build a concise, doc-grounded offline answer from retrieved context."""
    product = _infer_primary_product(query)
    confidence = _estimate_rag_confidence(query, rag_context)
    entries = _parse_rag_context_entries(rag_context)
    sources = [src for src, _ in entries]
    snippets = [sn for _, sn in entries if sn]
    q = (query or '').lower()

    def _keyword_sentences(keywords: list[str], max_items: int = 3) -> list[str]:
        picked = []
        seen = set()
        for sn in snippets:
            chunks = re.split(r'(?<=[.!?])\s+', sn)
            for c in chunks:
                c_norm = re.sub(r'\s+', ' ', c).strip(' .')
                if not c_norm:
                    continue
                low = c_norm.lower()
                if not any(k in low for k in keywords):
                    continue
                key = low[:180]
                if key in seen:
                    continue
                seen.add(key)
                picked.append(c_norm)
                if len(picked) >= max_items:
                    return picked
        return picked

    lines = []
    lines.append(f'{product} Offline Guidance (from local docs)')
    lines.append('=' * 72)

    if 'aap' in q and re.search(r'\b(event\s*[- ]?driven\s+ansible|automation\s+decisions|eda)\b', q):
        lines.append('Recommended approach:')
        lines.append('- Confirm AAP 2.6 EDA components and required permissions are installed and reachable.')
        lines.append('- Define event sources and rulebook decision logic for the automation decision flow.')
        lines.append('- Test decisions in non-production first, then promote to production with approval gates.')
        lines.append('- Add observability: capture rule activations, actions, and rollback conditions.')
        highlights = _keyword_sentences(['automation', 'decision', 'event-driven', 'eda', 'rulebook'])
    elif 'satellite' in q:
        is_install_query = bool(re.search(r'\b(install|automat|deploy|set\s*up|setup|provision)\b', q))
        if is_install_query:
            version_match = re.search(r'satellite\s+(\d+\.\d+)', q)
            ver = version_match.group(1) if version_match else '6.18'
            lines.append(f'Red Hat Satellite {ver} — Automated Installation Steps:')
            lines.append('')
            lines.append('1. Register and enable required repositories:')
            lines.append('   subscription-manager register --username <user> --password <pass>')
            lines.append('   subscription-manager attach --pool=<satellite_pool_id>')
            lines.append(f'   subscription-manager repos --enable=rhel-8-for-x86_64-baseos-rpms \\')
            lines.append(f'     --enable=rhel-8-for-x86_64-appstream-rpms \\')
            lines.append(f'     --enable=satellite-{ver}-for-rhel-8-x86_64-rpms \\')
            lines.append(f'     --enable=satellite-maintenance-{ver}-for-rhel-8-x86_64-rpms')
            lines.append('')
            lines.append('2. Install the Satellite package group and run the installer:')
            lines.append('   dnf install satellite')
            lines.append('   satellite-installer --scenario satellite \\')
            lines.append('     --foreman-initial-admin-username admin \\')
            lines.append('     --foreman-initial-admin-password <password> \\')
            lines.append('     --foreman-proxy-dns true \\')
            lines.append('     --foreman-proxy-dns-managed true \\')
            lines.append('     --foreman-proxy-dhcp true \\')
            lines.append('     --foreman-proxy-dhcp-managed true')
            lines.append('')
            lines.append('3. Automate via the Satellite Ansible Collection (recommended):')
            lines.append('   # Install the collection')
            lines.append('   ansible-galaxy collection install redhat.satellite')
            lines.append('   # Example inventory group_vars/satellite.yml snippet:')
            lines.append('   #   satellite_server_url: https://satellite.example.com')
            lines.append('   #   satellite_username: admin')
            lines.append('   #   satellite_password: "{{ vault_satellite_password }}"')
            lines.append('   # Use modules: redhat.satellite.organization, redhat.satellite.location,')
            lines.append('   #   redhat.satellite.repository, redhat.satellite.content_view')
            lines.append('')
            lines.append('4. Post-install validation:')
            lines.append('   satellite-maintain health check')
            lines.append('   hammer ping')
            lines.append('   hammer organization list')
            highlights = _keyword_sentences(['satellite-installer', 'ansible', 'collection', 'hammer', 'install'])
        else:
            lines.append('Recommended approach:')
            lines.append('- Validate Satellite/Capsule prerequisites: DNS, certs, time sync, and content lifecycle.')
            lines.append('- Stage changes in a lower environment, then apply to production in phased windows.')
            lines.append('- Verify host registration, content views, and provisioning/patch workflows post-change.')
            lines.append('- Keep rollback checkpoints for capsule/content changes before broad rollout.')
            highlights = _keyword_sentences(['satellite', 'capsule', 'content', 'provision', 'lifecycle'])
    elif 'idm' in q or 'freeipa' in q or 'identity management' in q:
        lines.append('Recommended approach:')
        lines.append('- Validate IdM topology, DNS/SRV records, and trust/authentication prerequisites.')
        lines.append('- Implement configuration incrementally, validating auth, HBAC, and sudo policies each step.')
        lines.append('- Run identity and access smoke tests before production cutover.')
        lines.append('- Document rollback and recovery for replication or policy regressions.')
        highlights = _keyword_sentences(['identity', 'idm', 'freeipa', 'hbac', 'sudo', 'auth'])
    else:
        lines.append('Recommended approach:')
        lines.append('- Use the referenced product documentation to implement in phased, test-first steps.')
        lines.append('- Validate core workflows and policy/security behavior after each phase.')
        lines.append('- Keep rollback checkpoints and record evidence for each change gate.')
        highlights = _keyword_sentences(['configure', 'install', 'deploy', 'update', 'validate'])

    if highlights:
        lines.append('')
        lines.append('Relevant highlights from local docs:')
        for h in highlights[:3]:
            lines.append(f'- {h[:220]}')

    lines.append('')
    lines.append('Grounding:')
    lines.append(f'- Confidence: {confidence}')
    if sources:
        uniq = []
        seen = set()
        for s in sources:
            k = s.lower().strip()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(s)
        lines.append(f'- Sources: {", ".join(uniq[:5])}')
    else:
        lines.append('- Sources: none matched in local knowledge base')

    return '\n'.join(lines)


def _log_quality_event(user: str, query: str, retried: bool, fallback_used: bool, rag_confidence: str) -> None:
    """Append lightweight quality telemetry for offline analysis."""
    try:
        ensure_dirs()
        out_path = os.path.join(REPORTS_DIR, 'hal-quality-events.jsonl')
        evt = {
            'timestamp': ts_now(),
            'user': user,
            'query': (query or '')[:500],
            'retried': bool(retried),
            'fallback_used': bool(fallback_used),
            'rag_confidence': rag_confidence,
        }
        with open(out_path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(evt) + '\n')
    except Exception:
        pass


def _is_well_query(text: str) -> bool:
    """Detect queries asking about HAL's wellbeing (e.g., 'how are you', 'how are you feeling').

    Returns True for conversational wellbeing questions that should trigger a health check.
    """
    if not text:
        return False
    s = text.strip().lower()
    # common phrasings
    if re.search(r"how\s+are\s+you", s):
        return True
    if re.search(r"how\s+are\s+you\s+feeling", s):
        return True
    if re.search(r"how\s+are\s+you\s+doing", s):
        return True
    if re.search(r"are\s+you\s+well", s):
        return True
    if re.search(r"how's\s+it\s+going", s):
        return True
    return False


def _extract_company_from_contact_query(query: str) -> str:
    if not query:
        return ''
    q = query.strip()
    m = re.search(r"\b(?:contacts?|contacrs|email(?:\s+addresses?)?)\b.*?\bat\s+([A-Za-z0-9 .,&'_-]+)$", q, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip(" .?\"")
    m = re.search(r"\bat\s+([A-Za-z0-9 .,&'_-]+)$", q, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip(" .?\"")
    return ''


def _flat_company_email_list_from_training(query: str) -> str | None:
    q = (query or '').lower()
    asks_contacts = bool(re.search(r"\b(contact|contacts|contacrs)\b", q))
    asks_emails = bool(re.search(r"\b(email|emails|email\s+addresses?)\b", q))
    if not (asks_contacts or asks_emails):
        return None

    company = _extract_company_from_contact_query(query)
    if not company:
        return None

    company_l = company.lower()
    emails = set()
    sheet_re = re.compile(r"(?ms)^# Sheet:\s*(.+?)\n(.*?)(?=^# Sheet:\s*|\Z)")
    email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    try:
        train_path = Path(TRAIN_DIR)
        for json_file in sorted(train_path.glob('*.json'), reverse=True):
            try:
                with open(json_file, 'r', encoding='utf-8') as fh:
                    record = json.load(fh)
            except Exception:
                continue

            if record.get('type') != 'supplemental_document':
                continue

            text = (record.get('text', '') or '').strip()
            if not text:
                continue

            for sheet_name, body in sheet_re.findall(text):
                sn = sheet_name.lower().strip()
                if company_l in sn or sn in company_l or any(tok and tok in sn for tok in company_l.split()):
                    emails.update(e.lower() for e in email_re.findall(body))

            # Fallback: any row that mentions the company and has email(s)
            if not emails:
                for line in text.splitlines():
                    ll = line.lower()
                    if company_l in ll and '@' in line:
                        emails.update(e.lower() for e in email_re.findall(line))

            # Last resort: infer mail domain from company token (e.g. centene -> @centene.)
            if not emails:
                domain_token = re.sub(r'[^a-z0-9]', '', company_l)
                if domain_token:
                    domain_re = re.compile(rf"[A-Za-z0-9._%+-]+@{re.escape(domain_token)}\.[A-Za-z]{{2,}}", re.IGNORECASE)
                    emails.update(e.lower() for e in domain_re.findall(text))
    except Exception:
        return None

    if not emails:
        return None

    return '\n'.join(sorted(emails))


def _extract_company_city_from_address_query(query: str) -> tuple[str, str]:
    if not query:
        return '', ''
    q = query.strip()
    m = re.search(r"\baddress\b\s*(?:of|for)?\s+(.+?)(?:\s+in\s+([A-Za-z0-9 .,'_-]+))?$", q, flags=re.IGNORECASE)
    if m:
        company = (m.group(1) or '').strip(" .?\"")
        city = (m.group(2) or '').strip(" .?\"")
        return company, city
    # fallback patterns
    m = re.search(r"\bof\s+(.+?)(?:\s+in\s+([A-Za-z0-9 .,'_-]+))?$", q, flags=re.IGNORECASE)
    if m:
        return (m.group(1) or '').strip(" .?\""), (m.group(2) or '').strip(" .?\"")
    return '', ''


def _company_address_from_training(query: str) -> str | None:
    if not query or not re.search(r"\baddress\b", query, flags=re.IGNORECASE):
        return None

    company, city = _extract_company_city_from_address_query(query)
    if not company:
        return None

    company_l = company.lower()
    city_l = city.lower().strip()

    def _norm_city(v: str) -> str:
        v = (v or '').lower().strip()
        v = re.sub(r'[^a-z0-9\s]', ' ', v)
        v = re.sub(r'\bsaint\b', 'st', v)
        v = re.sub(r'\s+', ' ', v).strip()
        return v

    city_l_norm = _norm_city(city_l)
    rows = []

    try:
        train_path = Path(TRAIN_DIR)
        for json_file in sorted(train_path.glob('*.json'), reverse=True):
            try:
                with open(json_file, 'r', encoding='utf-8') as fh:
                    record = json.load(fh)
            except Exception:
                continue

            if record.get('type') != 'supplemental_document':
                continue

            text = (record.get('text', '') or '').strip()
            if not text:
                continue

            for line in text.splitlines():
                if '|' not in line:
                    continue
                ll = line.lower()
                if company_l not in ll:
                    continue
                cols = [c.strip() for c in line.split('|')]
                if len(cols) < 6:
                    continue

                account = cols[0]
                street = cols[3] if len(cols) > 3 else ''
                billing_city = cols[4] if len(cols) > 4 else ''
                country = cols[5] if len(cols) > 5 else ''

                if city_l_norm:
                    billing_city_norm = _norm_city(billing_city)
                    if city_l_norm not in billing_city_norm and billing_city_norm not in city_l_norm:
                        continue

                if not (street and billing_city):
                    continue

                rows.append(f"{account} | {street} | {billing_city} | {country}".strip())

    except Exception:
        return None

    if not rows:
        return None

    deduped = sorted(set(rows))
    return '\n'.join(deduped)


def _training_index_signature() -> tuple[int, int]:
    """Return a cheap directory signature: (file_count, newest_mtime)."""
    if not os.path.isdir(TRAIN_DIR):
        return (0, 0)
    count = 0
    newest = 0
    try:
        for entry in os.scandir(TRAIN_DIR):
            if not entry.is_file():
                continue
            if not (entry.name.endswith('.json') or entry.name.endswith('.jsonl')):
                continue
            count += 1
            try:
                mt = int(entry.stat().st_mtime)
            except Exception:
                mt = 0
            if mt > newest:
                newest = mt
    except Exception:
        return (0, 0)
    return (count, newest)


def _parse_record_epoch(record: dict, fallback_epoch: int) -> int:
    """Best-effort conversion of record timestamp to epoch seconds."""
    ts = str(record.get('timestamp', '') or '').strip()
    if ts:
        # Support HAL ts_now format and common ISO format.
        for fmt in ('%Y%m%dT%H%M%SZ', '%Y-%m-%dT%H:%M:%SZ'):
            try:
                return int(datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc).timestamp())
            except Exception:
                pass
    return fallback_epoch


def _freshness_bonus(epoch: int) -> int:
    """Prefer newer records without overpowering textual relevance."""
    if not epoch:
        return 0
    age_days = max(0.0, (time.time() - float(epoch)) / 86400.0)
    if age_days <= 1:
        return 12
    if age_days <= 7:
        return 8
    if age_days <= 30:
        return 4
    return 0


def _get_training_index() -> list[dict]:
    """Build/load cached training index for retrieval paths."""
    sig = _training_index_signature()
    if _TRAINING_INDEX_CACHE['signature'] == sig and _TRAINING_INDEX_CACHE['entries']:
        return _TRAINING_INDEX_CACHE['entries']

    entries = []
    if not os.path.isdir(TRAIN_DIR):
        _TRAINING_INDEX_CACHE.update({'signature': sig, 'built_at': time.time(), 'entries': entries})
        return entries

    train_path = Path(TRAIN_DIR)
    all_files = sorted(train_path.glob('*.json'), reverse=True)
    all_files += sorted(train_path.glob('*.jsonl'), reverse=True)

    for path in all_files:
        try:
            fallback_epoch = int(path.stat().st_mtime)
        except Exception:
            fallback_epoch = 0

        records = []
        try:
            if path.suffix == '.jsonl':
                with open(path, 'r', encoding='utf-8') as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except Exception:
                            continue
            else:
                with open(path, 'r', encoding='utf-8') as fh:
                    records = [json.load(fh)]
        except Exception:
            continue

        for record in records:
            if not isinstance(record, dict):
                continue

            doc_type = str(record.get('type', '') or '')
            if doc_type not in ('supplemental_document', 'hal_interaction', 'business_intel_account'):
                continue

            rec_epoch = _parse_record_epoch(record, fallback_epoch)
            source = str(record.get('source_name') or record.get('source') or record.get('source_file') or path.name)

            if doc_type == 'supplemental_document':
                searchable = ' '.join([
                    str(record.get('source_name', '')),
                    str(record.get('source', '')),
                    str(record.get('source_url', '')),
                    str(record.get('source_path', '')),
                    str(record.get('text', '') or record.get('extracted_text', '') or ''),
                ]).lower()
            elif doc_type == 'hal_interaction':
                searchable = ' '.join([
                    str(record.get('request', '')),
                    str(record.get('ai_summary', '') or record.get('ai_response_raw', '') or ''),
                ]).lower()
            else:
                searchable = ' '.join([
                    str(record.get('account_name', '')),
                    str(record.get('text', '')),
                    str(record.get('tags', '')),
                    str(record.get('redhat_focus_areas', '')),
                    str(record.get('contacts', '')),
                    str(record.get('notable_news_headlines', '')),
                    str(record.get('short_summary', '')),
                ]).lower()

            entries.append({
                'doc_type': doc_type,
                'source': source,
                'searchable': searchable,
                'record': record,
                'path_name': path.name,
                'epoch': rec_epoch,
            })

    _TRAINING_INDEX_CACHE.update({'signature': sig, 'built_at': time.time(), 'entries': entries})
    return entries


def search_training_data(query: str, limit: int = 5) -> str | None:
    """Search training data files for relevant content matching the user's query.
    
    Returns a formatted string with matching records or None if no matches found.
    """
    if not query or not os.path.exists(TRAIN_DIR):
        return None

    # For address lookup queries, return precise matching row(s) instead of snippets.
    address_match = _company_address_from_training(query)
    if address_match:
        return address_match

    # For contact/email lookup queries, return a flat list as requested.
    flat_emails = _flat_company_email_list_from_training(query)
    if flat_emails:
        return flat_emails
    
    # Extract keywords from query (lowercase, alphabetic only)
    keywords = [w.lower() for w in re.findall(r'\b[a-z]+\b', query.lower())]
    if not keywords:
        return None
    
    # Remove common stop words
    stop_words = {'the', 'a', 'an', 'and', 'or', 'is', 'are', 'who', 'what', 'where', 'when', 'why', 'how', 'my', 'your', 'i', 'you', 'me', 'to', 'of', 'in', 'at', 'for', 'by', 'do'}
    keywords = [k for k in keywords if k not in stop_words and len(k) > 2]
    
    if not keywords:
        return None

    preferred_patterns = _preferred_doc_source_patterns(query)

    matches = []
    try:
        for item in _get_training_index():
            doc_type = item['doc_type']
            searchable = item['searchable']
            record = item['record']
            source = item['source']

            match_count = sum(1 for kw in keywords if kw in searchable)
            if match_count == 0:
                continue

            source_low = str(source).lower()
            bonus = 100 if any(p in source_low for p in preferred_patterns) else 0
            freshness = _freshness_bonus(item.get('epoch', 0))

            if doc_type == 'business_intel_account':
                summary = record.get('short_summary', '') or record.get('text', '')[:200]
                tags = record.get('tags', [])
                rh_focus = record.get('redhat_focus_areas', [])
                contacts = record.get('contacts', [])
                snippet_parts = [f"Account: {record.get('account_name', '')}"]
                if summary:
                    snippet_parts.append(f"Summary: {summary[:300]}")
                if tags:
                    snippet_parts.append(f"Tech Signals: {', '.join(str(t) for t in tags[:8])}")
                if rh_focus:
                    snippet_parts.append(f"Red Hat Focus: {' | '.join(str(f) for f in rh_focus[:3])}")
                if contacts:
                    snippet_parts.append(f"Contacts: {', '.join(str(c) for c in contacts[:5])}")
                snippet = '\n   '.join(snippet_parts)
                out_type = 'intel'
            else:
                best_idx = -1
                for kw in keywords:
                    idx = searchable.find(kw)
                    if idx >= 0:
                        best_idx = idx
                        break
                if best_idx >= 0:
                    start = max(0, best_idx - 100)
                    end = min(len(searchable), best_idx + 200)
                    snippet = searchable[start:end]
                    if start > 0:
                        snippet = '...' + snippet
                    if end < len(searchable):
                        snippet = snippet + '...'
                else:
                    snippet = searchable[:300]
                out_type = 'document' if doc_type == 'supplemental_document' else 'interaction'

            matches.append({
                'source': source,
                'type': out_type,
                'matches': match_count,
                'score': match_count + bonus + freshness,
                'snippet': snippet.strip(),
            })
    except Exception:
        return None
    
    if not matches:
        return None

    matches = sorted(matches, key=lambda x: (-x.get('score', x['matches']), -x['matches'], x['source']))
    deduped = []
    seen_sources = set()
    for match in matches:
        source_key = str(match['source']).strip().lower()
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        deduped.append(match)
    
    # Format results
    result_lines = ['Found in your training data:\n']
    for i, match in enumerate(deduped[:limit], 1):
        result_lines.append(f"{i}. From {match['source']} ({match['type']}):")
        result_lines.append(f"   {match['snippet']}")
        result_lines.append('')
    
    return '\n'.join(result_lines)


def search_training_data_for_rag(query: str, limit: int = 5, max_chars: int = 3000) -> str | None:
    """Return a compact training-data context string to inject into LLM prompts (RAG).

    Returns a plain-text block capped at max_chars, suitable for inclusion in
    the system or user prompt, or None if nothing relevant is found.
    """
    if not query or not os.path.exists(TRAIN_DIR):
        return None

    keywords = [w.lower() for w in re.findall(r'\b[a-z]+\b', query.lower())]
    stop_words = {'the', 'a', 'an', 'and', 'or', 'is', 'are', 'who', 'what', 'where',
                  'when', 'why', 'how', 'my', 'your', 'i', 'you', 'me', 'to', 'of',
                  'in', 'at', 'for', 'by', 'do', 'give', 'make', 'can', 'me', 'us',
                  'please', 'help', 'about', 'with', 'this', 'that', 'from', 'on'}
    keywords = [k for k in keywords if k not in stop_words and len(k) > 2]
    if not keywords:
        return None

    allow_business_intel = _query_prefers_business_intel(query)
    preferred_patterns = _preferred_doc_source_patterns(query)

    matches = []
    try:
        for item in _get_training_index():
            doc_type = item['doc_type']
            if doc_type == 'business_intel_account' and not allow_business_intel:
                continue

            searchable = item['searchable']
            record = item['record']
            source = item['source']

            match_count = sum(1 for kw in keywords if kw in searchable)
            if match_count == 0:
                continue

            if doc_type == 'business_intel_account':
                parts = [f"Account: {record.get('account_name', 'Unknown')}"]
                if record.get('short_summary'):
                    parts.append(f"Summary: {str(record['short_summary'])[:400]}")
                if record.get('tags'):
                    parts.append(f"Tech Stack: {str(record['tags'])[:200]}")
                if record.get('redhat_focus_areas'):
                    parts.append(f"RH Focus: {str(record['redhat_focus_areas'])[:200]}")
                if record.get('contacts'):
                    parts.append(f"Contacts: {str(record['contacts'])[:200]}")
                if record.get('territory_owner'):
                    parts.append(f"Owner: {record['territory_owner']}")
                snippet = ' | '.join(parts)
                source = f"business_intel:{record.get('account_name', item.get('path_name', 'record'))}"
            else:
                best_idx = -1
                for kw in keywords:
                    idx = searchable.find(kw)
                    if idx >= 0:
                        best_idx = idx
                        break
                if best_idx >= 0:
                    start = max(0, best_idx - 80)
                    end = min(len(searchable), best_idx + 300)
                    snippet = ('...' if start > 0 else '') + searchable[start:end] + ('...' if end < len(searchable) else '')
                else:
                    snippet = searchable[:300]

            source_low = str(source).lower()
            bonus = 100 if any(p in source_low for p in preferred_patterns) else 0
            freshness = _freshness_bonus(item.get('epoch', 0))
            matches.append({'source': source, 'snippet': snippet.strip(), 'matches': match_count, 'score': match_count + bonus + freshness})

    except Exception:
        return None

    if not matches:
        return None

    matches = sorted(matches, key=lambda x: (-x.get('score', x['matches']), -x['matches'], x['source']))
    deduped = []
    seen_sources = set()
    for match in matches:
        source_key = str(match['source']).strip().lower()
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        deduped.append(match)

    # Build compact context block
    lines = ['[Relevant context from your personal knowledge base:]']
    total = len(lines[0])
    entries_added = 0
    for m in deduped[:limit]:
        entry = f"\n• [{m['source']}]: {m['snippet']}"
        if total + len(entry) > max_chars:
            # Ensure we still return at least one usable entry instead of only a header.
            if entries_added == 0:
                space_left = max_chars - total
                if space_left > 8:
                    truncated = entry[: space_left - 3] + '...'
                    lines.append(truncated)
                    entries_added += 1
            break
        lines.append(entry)
        total += len(entry)
        entries_added += 1

    if entries_added == 0:
        return None

    return '\n'.join(lines)


def _safely_parse_json_or_list(value):
    """Attempt to parse JSON strings; return as-is if already a list/dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed
        except Exception:
            return value
    if isinstance(value, list):
        # Recursively parse any JSON strings in the list
        result = []
        for item in value:
            if isinstance(item, str):
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, list):
                        result.extend(parsed)
                    else:
                        result.append(parsed)
                except Exception:
                    result.append(item)
            else:
                result.append(item)
        return result
    return value


def _flatten_value(value):
    """Recursively flatten nested dicts/lists to readable strings."""
    if isinstance(value, dict):
        if len(value) == 0:
            return None
        # Try to extract human-readable field
        for key in ['name', 'title', 'value', 'text', 'content']:
            if key in value:
                return str(value[key])
        # Fallback: comma-separated key=value pairs
        pairs = [f"{k}: {v}" for k, v in list(value.items())[:5]]
        return "; ".join(pairs)
    if isinstance(value, list):
        if len(value) == 0:
            return None
        # If list of dicts, extract readable parts
        if isinstance(value[0], dict):
            items = []
            for v in value[:10]:
                flat = _flatten_value(v)
                if flat:
                    items.append(flat)
            return "; ".join(items) if items else None
        # Otherwise join as comma-separated
        return ", ".join(str(v) for v in value[:15])
    if isinstance(value, str):
        # Try to parse as JSON
        try:
            parsed = json.loads(value)
            return _flatten_value(parsed)
        except Exception:
            return value.strip() if value else None
    return str(value) if value else None


def _known_accounts_from_training(max_accounts: int = 500) -> list[str]:
    """Return account/company names discovered from local intel and enrichment records."""
    now = time.time()
    if (now - float(_ACCOUNT_NAME_CACHE.get('built_at', 0.0) or 0.0)) < 300:
        return list(_ACCOUNT_NAME_CACHE.get('names', []))

    names: list[str] = []
    seen: set[str] = set()

    # Keep a small static seed for first-run utility.
    static_seed = [
        'centene', 'davita', 'arrow', 'ascension', 'ameren', 'elevance',
        'avaya', 'enterprise mobility', 'owens minor', "o'reilly", 'maxar',
        'rga', 'principal financial', 'western union', 'world wide technology',
    ]
    for n in static_seed:
        key = n.strip().lower()
        if key and key not in seen:
            seen.add(key)
            names.append(key)

    try:
        train_path = Path(TRAIN_DIR)
        for fp in sorted(train_path.glob('*.json'), reverse=True):
            try:
                rec = json.loads(fp.read_text(encoding='utf-8'))
            except Exception:
                continue

            rec_type = str(rec.get('type', ''))
            cand = ''
            if rec_type == 'business_intel_account':
                cand = str(rec.get('account_name', '') or '').strip()
            elif rec_type == 'supplemental_document' and str(rec.get('subtype', '')) == 'company_public_enrichment':
                cand = str(rec.get('company', '') or '').strip()

            if not cand:
                continue
            key = cand.lower()
            if key not in seen:
                seen.add(key)
                names.append(key)
            if len(names) >= max_accounts:
                break
    except Exception:
        pass

    _ACCOUNT_NAME_CACHE['built_at'] = now
    _ACCOUNT_NAME_CACHE['names'] = names
    return list(names)


def _extract_company_name_from_query(query: str) -> str | None:
    """Extract company/account phrase from natural-language intel requests."""
    if not query:
        return None

    q = ' '.join(query.strip().split())
    patterns = [
        r'\b(?:tell\s+me\s+anything\s+about|what\s+do\s+you\s+know\s+about|intel\s+report\s+for|account\s+brief\s+for|account\s+report\s+for)\s+(?:the\s+company\s+)?(.+)$',
        r'\b(?:company|account)\s+(.+)$',
    ]
    for pat in patterns:
        m = re.search(pat, q, re.IGNORECASE)
        if m:
            name = (m.group(1) or '').strip().strip('"\' .?')
            if name:
                return name
    return None


def _detect_account_in_query(query: str) -> str | None:
    """Detect if a query mentions a known account and return the account name."""
    if not query or len(query.strip()) < 2:
        return None

    known_accounts = _known_accounts_from_training()
    query_lower = query.lower()
    # Look for account mentions
    for account in known_accounts:
        if account in query_lower:
            return account

    extracted = _extract_company_name_from_query(query)
    if extracted:
        return extracted

    return None


def _is_company_intel_query(query: str) -> bool:
    """Detect free-text requests asking for company/account intelligence."""
    if not query:
        return False
    q = query.lower()
    has_intent = bool(re.search(r'\b(intel|intelligence|brief|report|about|profile|news|stakeholder|contacts?)\b', q))
    has_scope = bool(re.search(r'\b(company|account|customer|client)\b', q))
    return has_intent and has_scope


def _enrich_companies_public(companies: list[str], max_companies: int = 10) -> tuple[bool, str]:
    """Run public web enrichment pipeline for company names."""
    clean = []
    seen = set()
    for c in companies:
        name = (c or '').strip().strip('"\'')
        if not name:
            continue
        k = name.lower()
        if k in seen:
            continue
        seen.add(k)
        clean.append(name)
        if len(clean) >= max(1, max_companies):
            break

    if not clean:
        return False, 'No company names provided for enrichment.'

    enr = os.path.join(BASE_DIR, 'mcp-ai', 'enrich_companies.py')
    if not os.path.exists(enr):
        return False, f'Company enrichment script not found at {enr}'

    cmd = ['/usr/bin/env', 'python3', enr, *clean]
    try:
        proc = _run_subprocess_with_spinner('Enriching company intelligence from public web sources', cmd, text=True, capture_output=True)
        out = ((proc.stdout or '') + (proc.stderr or '')).strip()
        return proc.returncode == 0, out
    except Exception as exc:
        return False, f'ERR: {exc}'


def _generate_intel_report_live(account_query: str, allow_public_enrich: bool = True) -> str | None:
    """Generate account intel using local+LLM context; optionally refresh from public web first."""
    account = (account_query or '').strip().strip('"\'')
    if not account:
        return None

    cache_ttl_days = max(1, int(os.environ.get('HAL_INTEL_CACHE_TTL_DAYS', '3') or 3))
    cache_daily = os.environ.get('HAL_INTEL_CACHE_DAILY', '1').lower() not in ('0', 'false', 'no', 'n')

    _cleanup_intel_cache(max_age_days=cache_ttl_days)
    cached = _load_intel_cache(account, max_age_days=cache_ttl_days, require_same_day=cache_daily)
    if cached:
        return cached + '\n\nNote: Served from daily intel cache.'

    enrich_on_query = allow_public_enrich and os.environ.get('HAL_ENRICH_ON_COMPANY_QUERY', '1').lower() not in ('0', 'false', 'no', 'n')
    enrich_note = None
    if enrich_on_query:
        ok, out = _enrich_companies_public([account], max_companies=1)
        if ok:
            enrich_note = f'Public web enrichment refreshed for {account}.'
        elif out:
            enrich_note = f'Public web enrichment note: {out.splitlines()[-1]}'

    report = generate_business_account_brief(account) or generate_intel_report(account)
    if report and enrich_note and enrich_note not in report:
        report = f'{report}\n\nNote: {enrich_note}'
    if report:
        _save_intel_cache(account, report)
    return report


def _query_prefers_business_intel(query: str) -> bool:
    """Return True when a query is clearly business/account/intel-oriented."""
    if not query:
        return False

    q = query.lower()
    if _detect_account_in_query(q):
        return True

    return bool(re.search(
        r'\b(account|customer|company|business|seller|sales|territory|stakeholder|contact|contacts|executive|ae|ssa|pod|intel|intelligence|brief|stock|ticker|news)\b',
        q,
    ))


def _is_strategy_query(query: str) -> bool:
    """Detect consulting-strategy style requests."""
    if not query:
        return False
    q = query.lower()
    return bool(re.search(r"\b(strategy|consulting strategy|plan|roadmap|go[- ]to[- ]market|gtm)\b", q))


def _is_stakeholder_query(query: str) -> bool:
    """Detect account stakeholder/contact lookup intent."""
    if not query:
        return False
    q = query.lower()
    return bool(
        re.search(
            r"\b(who\s+is|who\s+are|contacts?|stakeholders?|invite|cadence|cio|ciso|vp|operations\s+lead|security\s+lead)\b",
            q,
        )
    )


def _is_subscription_csv_query(query: str) -> bool:
    """Detect requests for CSV exports of subscription counts across customers."""
    if not query:
        return False
    q = query.lower()
    has_csv = 'csv' in q
    has_customer_scope = bool(re.search(r'\b(customers?|accounts?)\b', q))
    has_subscriptions = bool(re.search(r'\b(subscriptions?|subs?|entitlements?)\b', q))
    has_product = bool(re.search(r'\b(rhel|red\s*hat\s*enterprise\s*linux|aap|ansible|openshift|ocp)\b', q))
    return has_csv and has_customer_scope and has_subscriptions and has_product


def _is_stock_price_query(query: str) -> bool:
    """Detect stock/ticker lookup requests."""
    if not query:
        return False
    q = query.lower()
    return bool(re.search(r'\b(stock\s+price|share\s+price|ticker|price\s+for)\b', q))


def _is_server_update_strategy_query(query: str) -> bool:
    """Detect requests for a server update/patch strategy (including misspellings)."""
    if not query:
        return False
    q = query.lower()
    has_update = bool(re.search(r'\b(update|patch|upgrade|maintenance)\b', q))
    has_strategy = bool(re.search(r'\b(strategy|stratagy|strat(?:e|a)gy|plan|roadmap)\b', q))
    has_server_scope = bool(re.search(r'\b(server|servers|systems|hosts|nodes|fleet)\b', q))
    return has_update and has_strategy and has_server_scope


def _is_aap_migration_strategy_query(query: str) -> bool:
    """Detect migration strategy requests for Ansible/AAP upgrades (typo-tolerant)."""
    if not query:
        return False
    q = query.lower()
    # Handle glued typos like "formigrating" by normalizing a common phrase.
    q = q.replace('formigrating', 'for migrating')
    has_strategy = bool(re.search(r'\b(strategy|stratagy|strat(?:e|a)gy|plan|roadmap)\b', q))
    has_migration = bool(re.search(r'\b(migrate|migrating|migration|upgrade|convert|transition)\b', q))
    has_product = bool(re.search(r'\b(aap|ansible|automation\s+platform)\b', q))
    has_versions = bool(re.search(r'\b2\.5\b', q) and re.search(r'\b2\.6\b', q))
    return has_strategy and has_migration and has_product and has_versions


def _is_aap_patch_strategy_query(query: str) -> bool:
    """Detect practical patch/update runbook requests for AAP."""
    if not query:
        return False
    q = query.lower()
    has_aap = bool(re.search(r'\b(aap|ansible\s+automation\s+platform|controller|automation\s+controller)\b', q))
    has_patch_intent = bool(re.search(r'\b(patch(?:ing)?|update|upgrade|hotfix|security\s+fix)\b', q))
    has_howto = bool(re.search(r'\b(how\s+do\s+i|how\s+to|steps?|runbook|plan|strategy|best\s+way)\b', q))
    return has_aap and has_patch_intent and has_howto


def _is_satellite_aap_connection_strategy_query(query: str) -> bool:
    """Detect strategy requests for connecting Satellite with AAP."""
    if not query:
        return False
    q = query.lower()
    has_strategy = bool(re.search(r'\b(strategy|stratagy|strat(?:e|a)gy|plan|roadmap)\b', q))
    has_connect_intent = bool(
        re.search(
            r'\b(connect|connecting|integrate|integration|intergrate|intergrating|intergration|link|hook|set\s*up|setup|configure|configuring)\b',
            q,
        )
    )
    has_satellite = bool(re.search(r'\b(satellite|satellite\s*6\.18|satellite\s*6)\b', q))
    has_aap = bool(re.search(r'\b(aap|ansible\s+automation\s+platform|ansible)\b', q))
    return has_strategy and has_connect_intent and has_satellite and has_aap


def _is_satellite_pxe_strategy_query(query: str) -> bool:
    """Detect Satellite PXE provisioning strategy requests."""
    if not query:
        return False
    q = query.lower()
    has_strategy = bool(re.search(r'\b(strategy|stratagy|strat(?:e|a)gy|plan|roadmap|set\s*up|setup|configure)\b', q))
    has_satellite = bool(re.search(r'\b(satellite|satellite\s*6\.18|satellite\s*6)\b', q))
    has_pxe_stack = bool(re.search(r'\b(pxe|dhcp|dns|tftp|provision)\b', q))
    return has_strategy and has_satellite and has_pxe_stack


def _is_aap_mcp_setup_query(query: str) -> bool:
    """Detect requests for setting up MCP server in AAP."""
    if not query:
        return False
    q = query.lower()
    has_setup = bool(re.search(r'\b(set\s*up|setup|configure|install|deploy|how\s+do\s+i)\b', q))
    has_mcp = bool(re.search(r'\b(mcp|model\s+context\s+protocol)\b', q))
    has_aap = bool(re.search(r'\b(aap|ansible\s+automation\s+platform|controller)\b', q))
    return has_setup and has_mcp and has_aap


def _is_satellite_mcp_setup_query(query: str) -> bool:
    """Detect requests for setting up MCP server in Satellite environments."""
    if not query:
        return False
    q = query.lower()
    has_setup = bool(re.search(r'\b(set\s*up|setup|configure|install|deploy|how\s+do\s+i)\b', q))
    has_mcp = bool(re.search(r'\b(mcp|model\s+context\s+protocol)\b', q))
    has_satellite = bool(re.search(r'\b(satellite|capsule|foreman|katello)\b', q))
    return has_setup and has_mcp and has_satellite


def _is_satellite_end_to_end_setup_query(query: str) -> bool:
    """Detect broad Satellite setup strategy queries spanning content + provisioning."""
    if not query:
        return False
    q = query.lower()
    has_satellite = bool(re.search(r'\b(satellite|capsule|foreman|katello)\b', q))
    has_strategy = bool(re.search(r'\b(strategy|stratagy|plan|roadmap|set\s*up|setup|configure)\b', q))
    has_ansible = bool(re.search(r'\b(ansible|aap|automation)\b', q))
    has_content = bool(re.search(r'\b(manifest|activation\s+keys?|content\s+view|lifecycle|rhel\s*9|rhel\s*10)\b', q))
    has_provision = bool(re.search(r'\b(dhcp|dns|tftp|pxe|bootc|compute\s+resources?)\b', q))
    return has_satellite and has_strategy and has_ansible and has_content and has_provision


def _is_satellite_patch_strategy_query(query: str) -> bool:
    """Detect practical patch/upgrade runbook requests for Satellite."""
    if not query:
        return False
    q = query.lower()
    has_satellite = bool(re.search(r'\b(satellite|capsule|foreman|katello)\b', q))
    has_patch_intent = bool(re.search(r'\b(patch|update|upgrade|errata|hotfix|security\s+fix)\b', q))
    has_howto = bool(re.search(r'\b(how\s+do\s+i|how\s+to|what\s+is\s+the\s+best\s+way|runbook|steps?|plan|strategy)\b', q))
    return has_satellite and has_patch_intent and has_howto


def _is_idm_setup_query(query: str) -> bool:
    """Detect practical Red Hat IdM setup requests."""
    if not query:
        return False
    q = query.lower()
    has_idm = bool(re.search(r'\b(idm|identity\s+management|freeipa|ipa\s+server)\b', q))
    has_setup = bool(re.search(r'\b(set\s*up|setup|install|configure|deploy|how\s+do\s+i|how\s+to)\b', q))
    has_rhel = bool(re.search(r'\b(rhel|red\s*hat\s*enterprise\s*linux)\b', q))
    return has_idm and has_setup and has_rhel


def _is_ansible_codegen_query(query: str) -> bool:
    """Detect requests to write/generate Ansible playbooks, roles, collections, tasks, or modules."""
    if not query:
        return False
    q = query.lower()
    has_verb = bool(re.search(
        r'\b(write|create|generate|build|make|scaffold|show\s+me|give\s+me|draft|produce|how\s+do\s+i\s+write|how\s+to\s+write)\b', q))
    has_artifact = bool(re.search(
        r'\b(playbook|play\s*book|role|collection|task|handler|inventory|module|galaxy|ansible\s+script)\b', q))
    return has_verb and has_artifact


def _is_jinja2_codegen_query(query: str) -> bool:
    """Detect requests to write/generate Jinja2 templates."""
    if not query:
        return False
    q = query.lower()
    has_verb = bool(re.search(
        r'\b(write|create|generate|build|make|scaffold|show\s+me|give\s+me|draft|produce)\b', q))
    has_artifact = bool(re.search(r'\b(jinja2?|j2\b|\.j2\b|jinja\s+template|j2\s+template)\b', q))
    return has_verb and has_artifact


def _is_python_codegen_query(query: str) -> bool:
    """Detect requests to write/generate Python scripts, modules, or classes."""
    if not query:
        return False
    q = query.lower()
    has_verb = bool(re.search(
        r'\b(write|create|generate|build|make|scaffold|show\s+me|give\s+me|draft|produce)\b', q))
    has_artifact = bool(re.search(r'\b(python\s+script|python\s+module|python\s+class|python\s+function|py\s+script|\.py\b)\b', q))
    return has_verb and has_artifact


def _is_dependency_advisor_query(query: str) -> bool:
    """Detect requests asking what to install/import/require for Ansible, Jinja2, Python, or Git."""
    if not query:
        return False
    q = query.lower()
    has_dep = bool(re.search(
        r'\b(install|import|require|dependency|dependencies|package|pip|galaxy|what\s+do\s+i\s+need|what\s+needs|prerequisites?|what\s+collections|which\s+collections)\b', q))
    has_domain = bool(re.search(r'\b(ansible|jinja2?|python|git|automation|rhel\s+automation)\b', q))
    return has_dep and has_domain


def _is_ansible_eda_use_case_query(query: str) -> bool:
    """Detect questions asking what Event-Driven Ansible can be used for."""
    if not query:
        return False
    q = query.lower()
    has_eda = bool(re.search(r'\b(event\s*[- ]?driven\s+ansible|ansible\s+eda|eda)\b', q))
    has_use_case = bool(re.search(
        r'\b(use\s+cases?|used\s+for|use\s+for|what\s+can|what\s+might|list|examples?|do\s+with|good\s+for|where\s+would)\b',
        q,
    ))
    return has_eda and has_use_case


def _extract_redhat_docsets_from_query(query: str) -> list[str]:
    """Extract supported Red Hat documentation set keys from a user query."""
    if not query:
        return []

    q = query.lower()
    docsets = []

    if re.search(r'\b(satellite|red\s*hat\s*satellite)\b', q) and re.search(r'\b6\.18\b', q):
        docsets.append('satellite-6.18')
    if re.search(r'\b(aap|ansible\s+automation\s+platform|red\s+hat\s+ansible\s+automation\s+platform)\b', q) and re.search(r'\b2\.6\b', q):
        docsets.append('aap-2.6')
    if re.search(r'\b(idm|identity\s+management|freeipa)\b', q) and re.search(r'\b(5\.0|rhel\s*10|10)\b', q):
        docsets.append('idm-5.0')

    return docsets


def _is_redhat_docs_ingest_query(query: str) -> bool:
    """Detect requests to ingest/read/import Red Hat product documentation."""
    if not query:
        return False

    q = query.lower()
    has_action = bool(re.search(r'\b(go\s+through|ingest|import|load|crawl|read|study|pull|index)\b', q))
    has_docs = bool(re.search(r'\b(docs|documentation|manuals?|guides?|books?)\b', q))
    return has_action and has_docs and bool(_extract_redhat_docsets_from_query(query))


def _is_redhat_docs_sync_query(query: str) -> bool:
    """Detect requests to sync/refresh the default curated Red Hat documentation sets."""
    if not query:
        return False

    q = query.lower()
    has_action = bool(re.search(r'\b(sync|refresh|update|resync|re-sync|reindex|re-index)\b', q))
    has_docs = bool(re.search(r'\b(red\s*hat\s+docs?|docs?|documentation)\b', q))
    return has_action and has_docs


def _is_training_maintenance_query(query: str) -> bool:
    """Detect requests to optimize/clean/reindex local HAL training data."""
    if not query:
        return False

    q = query.lower()
    has_action = bool(re.search(r'\b(optimi[sz]e|clean(?:\s*up)?|dedupe|de-dup|maint(?:enance)?|audit|reindex|re-index)\b', q))
    has_target = bool(re.search(r'\b(training\s+data|knowledge\s+base|kb|local\s+data|docs\s+cache|corpus)\b', q))
    return has_action and has_target


def _is_training_bundle_export_query(query: str) -> bool:
    """Detect requests to export/zip HAL training data for transfer (USB/memory key)."""
    if not query:
        return False
    q = query.lower()
    has_export = bool(re.search(
        r'\b(zip|bundle|export|backup|package|copy|move|put\s+on|save\s+to|archive)\b', q))
    has_training = bool(re.search(
        r'\b(training\s+data|training|knowledge\s+base|kb|mcp-ai\s+training|hal\s+training)\b', q))
    has_transfer = bool(re.search(
        r'\b(memory\s+key|usb|thumb\s+drive|flash\s+drive|portable|another\s+machine|other\s+system)\b', q))
    has_deploy_docs = bool(re.search(
        r'\b(readme|manual(?:ly)?|deploy|deployment|helper\s*script|install\s*script)\b', q))
    return has_training and (has_export or has_transfer or has_deploy_docs)


def _is_training_data_import_query(query: str) -> bool:
    """Detect requests asking how to import training data from URL/txt files."""
    if not query:
        return False
    q = query.lower()
    has_action = bool(re.search(r'\b(import|ingest|load|add|pull|bring\s+in|sync)\b', q))
    has_training = bool(re.search(r'\b(training\s+data|training|knowledge\s+base|kb|hal\s+training)\b', q))
    has_source = bool(re.search(r'\b(urls?|links?|web\s*page|website|txt|text\s*file|plain\s*text|\.txt)\b', q))
    return has_action and (has_training or has_source) and has_source


def _extract_import_urls_from_query(query: str) -> list[str]:
    """Extract URL targets from a natural-language import query."""
    if not query:
        return []
    matches = re.findall(r'https?://[^\s"\']+', query, flags=re.IGNORECASE)
    out: list[str] = []
    seen = set()
    for m in matches:
        u = m.strip().rstrip('.,;)]')
        if not u:
            continue
        k = u.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(u)
    return out


def _extract_import_txt_paths_from_query(query: str) -> list[str]:
    """Extract .txt paths from a natural-language import query."""
    if not query:
        return []
    matches = re.findall(r'"([^"]+\.txt)"|\'([^\']+\.txt)\'|([^\s"\']+\.txt)', query, flags=re.IGNORECASE)
    out: list[str] = []
    seen = set()
    for g1, g2, g3 in matches:
        p = (g1 or g2 or g3 or '').strip()
        if not p:
            continue
        k = os.path.abspath(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _extract_import_depth_from_query(query: str, default_depth: int = 3) -> int:
    """Extract crawl depth (1..9) from a natural-language import query."""
    if not query:
        return default_depth
    q = query.lower()
    m = re.search(r'--([1-9])\b', q)
    if m:
        return int(m.group(1))
    m = re.search(r'\b(?:depth|layer|layers)\s*(?:=|:)?\s*([1-9])\b', q)
    if m:
        return int(m.group(1))
    m = re.search(r'\b([1-9])\s*(?:layer|layers|depth)\b', q)
    if m:
        return int(m.group(1))
    return default_depth


def _is_training_import_url_execute_query(query: str) -> bool:
    """Detect natural-language aliases that should execute URL import directly."""
    if not query:
        return False
    q = query.lower()
    has_alias = bool(re.search(r'\b(import|ingest|load|add|pull)\s+(?:training\s+data\s+)?(?:from\s+)?urls?\b', q))
    return has_alias and bool(_extract_import_urls_from_query(query))


def _is_training_import_txt_execute_query(query: str) -> bool:
    """Detect natural-language aliases that should execute TXT import directly."""
    if not query:
        return False
    q = query.lower()
    has_alias = bool(re.search(r'\b(import|ingest|load|add|pull)\s+(?:training\s+data\s+)?(?:from\s+)?(?:txt|text\s*file|text\s*files|\.txt)\b', q))
    return has_alias and bool(_extract_import_txt_paths_from_query(query))


def _build_training_bundle_readme(bundle_name: str, file_count: int, created_ts: str) -> str:
    """README content included in the exported training-data bundle."""
    return f"""# HAL Training Data Bundle

Bundle: {bundle_name}
Created (UTC): {created_ts}
Training files included: {file_count}

## Purpose
This bundle contains HAL local training data from `~/.mcp-ai/training`.
Use it to move your HAL knowledge base to another machine (for example via USB/memory key).

## Bundle Contents
- `training-data/` : exported HAL training files
- `deploy_training_bundle.sh` : helper script to deploy into `~/.mcp-ai/training`
- `README.md` : this guide

## Manual Deployment Steps
1. Copy this zip file to the target machine.
2. Unzip it:
   - `unzip {bundle_name}.zip -d {bundle_name}`
3. Create HAL data directory if needed:
   - `mkdir -p ~/.mcp-ai/training`
4. Copy exported data:
   - `cp -a {bundle_name}/training-data/. ~/.mcp-ai/training/`
5. Verify files:
   - `ls -lah ~/.mcp-ai/training`

## Helper Script Deployment
From the unzipped bundle directory:
- `chmod +x deploy_training_bundle.sh`
- `./deploy_training_bundle.sh`

Optional target path:
- `./deploy_training_bundle.sh /custom/path/to/training`

## Notes
- Existing files on target may be overwritten if names match.
- Data remains local; no cloud upload is performed by this bundle.
- If files are encrypted (`*.enc`), decrypt after deploy with HAL:
  - `HAL --decrypt-training`
"""


def generate_training_bundle_export_response(query: str, user: str) -> str:
    """Create a portable zip with training data, README, and deploy helper script."""
    del query  # Intent is binary for this handler; output path is deterministic.
    ensure_dirs()

    train_path = Path(TRAIN_DIR)
    if not train_path.exists() or not train_path.is_dir():
        return (
            "Training bundle export failed: training directory does not exist.\n"
            f"Expected path: {train_path}\n"
            "Create/import training data first, then run this command again."
        )

    training_files = sorted([p for p in train_path.rglob('*') if p.is_file()])
    if not training_files:
        return (
            "Training bundle export skipped: no files found in your training directory.\n"
            f"Path: {train_path}\n"
            "Import data first, then run this command again."
        )

    created_ts = ts_now()
    host = socket.gethostname()
    bundle_name = f"hal-training-bundle-{host}-{created_ts}"

    out_dir = Path(os.path.expanduser(os.environ.get('HAL_TRAIN_BUNDLE_DIR', os.path.join(HOME, 'Downloads'))))
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{bundle_name}.zip"

    staging_dir = Path(REPORTS_DIR) / f"{bundle_name}-staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    (staging_dir / 'training-data').mkdir(parents=True, exist_ok=True)

    copied = 0

    for src in training_files:
        rel = src.relative_to(train_path)
        dest = staging_dir / 'training-data' / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1

    readme_text = _build_training_bundle_readme(bundle_name=bundle_name, file_count=copied, created_ts=created_ts)
    (staging_dir / 'README.md').write_text(readme_text, encoding='utf-8')

    helper_script = """#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"
SOURCE_DIR=\"$SCRIPT_DIR/training-data\"
TARGET_DIR=\"${1:-$HOME/.mcp-ai/training}\"

if [[ ! -d \"$SOURCE_DIR\" ]]; then
  echo \"ERROR: training-data directory not found: $SOURCE_DIR\" >&2
  exit 2
fi

mkdir -p \"$TARGET_DIR\"
cp -a \"$SOURCE_DIR\"/. \"$TARGET_DIR\"/

echo \"Training data deployed successfully.\"
echo \"Source : $SOURCE_DIR\"
echo \"Target : $TARGET_DIR\"
echo \"Files  : $(find \"$SOURCE_DIR\" -type f | wc -l)\"
"""
    helper_path = staging_dir / 'deploy_training_bundle.sh'
    helper_path.write_text(helper_script, encoding='utf-8')
    helper_path.chmod(0o755)

    with zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging_dir.rglob('*')):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(staging_dir))

    shutil.rmtree(staging_dir, ignore_errors=True)

    return (
        "HAL | Training Data Bundle Created\n"
        f"{'=' * 60}\n"
        f"Bundle file : {zip_path}\n"
        f"Source      : {train_path}\n"
        f"Files copied: {copied}\n"
        f"Requested by: {user}\n"
        f"{'=' * 60}\n"
        "Bundle includes:\n"
        "  - training-data/\n"
        "  - README.md (manual deployment steps)\n"
        "  - deploy_training_bundle.sh (helper deployment script)\n"
        "\n"
        "Transfer this zip to your memory key, then unzip and run the helper script on the destination host."
    )


def generate_training_import_help_response() -> str:
    """Return deterministic help for training-data imports from URL and txt files."""
    lines = []
    lines.append('Import Training Data (URL and TXT)')
    lines.append('=' * 72)
    lines.append('URL import:')
    lines.append('- Default crawl depth is 3 layers.')
    lines.append('- Use numeric switches --1 .. --9 to change depth (max: --9).')
    lines.append('- Examples:')
    lines.append('  - hal --import-url https://docs.redhat.com/en/documentation/red_hat_satellite/6.18')
    lines.append('  - hal --import-url https://example.com --1')
    lines.append('  - hal --import-url https://example.com https://docs.redhat.com --5')
    lines.append('')
    lines.append('TXT import:')
    lines.append('- Import one or many local text files directly into HAL training records.')
    lines.append('- Examples:')
    lines.append('  - hal --import-txt /path/notes.txt')
    lines.append('  - hal --import-txt /path/a.txt /path/b.txt')
    lines.append('')
    lines.append('Notes:')
    lines.append('- URL imports are fetched with same-host crawl restriction by default.')
    lines.append('- TXT imports create deterministic files under ~/.mcp-ai/training (idempotent by source path).')
    return '\n'.join(lines)


# ── Ansible code generation ───────────────────────────────────────────────────

_ANSIBLE_SCAFFOLD_PLAYBOOK = """\
---
# Ansible Playbook — generated by HAL
# Purpose: {purpose}
#
# PREREQUISITES / DEPENDENCIES
# ─────────────────────────────
#   System packages:
#     dnf install -y python3 python3-pip ansible-core
#   Python packages:
#     pip install ansible-lint netaddr
#   Ansible Galaxy collections:
#     ansible-galaxy collection install community.general
#     ansible-galaxy collection install ansible.posix
#     # Add any product-specific collections below, e.g.:
#     #   ansible-galaxy collection install redhat.satellite
#     #   ansible-galaxy collection install redhat.rhel_idm
#     #   ansible-galaxy collection install redhat.insights
#
# USAGE
#   ansible-playbook -i inventory/hosts.yml {filename} [-e "variable=value"] [-l target_group]
# ─────────────────────────────────────────────────────────────────────────────

- name: {purpose}
  hosts: all                          # Replace with specific group from inventory
  gather_facts: true
  become: true                        # Remove if no privilege escalation needed
  vars:
    # Define variables here or pass with -e on CLI
    # example_var: "value"

  pre_tasks:
    - name: Ensure Python 3 is available
      ansible.builtin.raw: dnf install -y python3
      changed_when: false
      when: ansible_facts is not defined

  tasks:
    - name: TODO — replace with actual task
      ansible.builtin.debug:
        msg: "Replace this task block with the real work"

    # Example: install a package
    # - name: Install a package
    #   ansible.builtin.dnf:
    #     name: httpd
    #     state: present

    # Example: copy a file from a Jinja2 template
    # - name: Deploy config from template
    #   ansible.builtin.template:
    #     src: templates/myconfig.j2
    #     dest: /etc/myapp/config.conf
    #     owner: root
    #     group: root
    #     mode: '0644'
    #   notify: Restart service

    # Example: run a command
    # - name: Run a shell command
    #   ansible.builtin.shell: |
    #     echo "hello" > /tmp/hal_test.txt
    #   register: cmd_result
    #   changed_when: cmd_result.rc != 0

  handlers:
    - name: Restart service
      ansible.builtin.service:
        name: httpd          # Replace with your service name
        state: restarted
        enabled: true
"""

_ANSIBLE_SCAFFOLD_ROLE = """\
# Ansible Role Scaffold — generated by HAL
# Role name: {role_name}
#
# PREREQUISITES / DEPENDENCIES
# ─────────────────────────────
#   System packages:
#     dnf install -y ansible-core
#   Create this role structure:
#     ansible-galaxy role init {role_name}
#   Or create directories manually:
#     mkdir -p {role_name}/{{tasks,handlers,defaults,vars,templates,files,meta}}
#
#   Required collections (add to requirements.yml):
#     ---
#     collections:
#       - name: community.general
#       - name: ansible.posix
# ─────────────────────────────────────────────────────────────────────────────

# ── {role_name}/tasks/main.yml ───────────────────────────────────────────────
---
- name: Include OS-specific variables
  ansible.builtin.include_vars: "{{{{ ansible_os_family }}}}.yml"
  ignore_errors: true

- name: TODO — main task for role {role_name}
  ansible.builtin.debug:
    msg: "Replace with real tasks"

# ── {role_name}/defaults/main.yml ───────────────────────────────────────────
# (Lowest priority — easily overridden)
# ---
# {role_name}_enabled: true
# {role_name}_config_dir: /etc/{role_name}
# {role_name}_package: {role_name}

# ── {role_name}/vars/main.yml ────────────────────────────────────────────────
# (Higher priority — set role-internal non-overridable variables here)
# ---
# _internal_var: value

# ── {role_name}/handlers/main.yml ────────────────────────────────────────────
# ---
# - name: Restart {role_name}
#   ansible.builtin.service:
#     name: "{{{{ {role_name}_service_name | default('{role_name}') }}}}"
#     state: restarted

# ── {role_name}/meta/main.yml ────────────────────────────────────────────────
# ---
# galaxy_info:
#   author: your_name
#   description: Role for {role_name}
#   company: Your Company
#   license: Apache-2.0
#   min_ansible_version: "2.14"
#   platforms:
#     - name: RHEL
#       versions: ["8", "9", "10"]
#   galaxy_tags: [rhel, {role_name}]
# dependencies: []

# ── {role_name}/templates/config.j2 ─────────────────────────────────────────
# Example Jinja2 config template:
# # Managed by Ansible - do not edit manually
# [settings]
# enabled = {{{{ {role_name}_enabled | lower }}}}
# config_dir = {{{{ {role_name}_config_dir }}}}
"""

_ANSIBLE_SCAFFOLD_COLLECTION = """\
# Ansible Collection Scaffold — generated by HAL
# Collection: {namespace}.{collection_name}
#
# PREREQUISITES / DEPENDENCIES
# ─────────────────────────────
#   System packages:
#     dnf install -y ansible-core tar
#   Create collection skeleton:
#     ansible-galaxy collection init {namespace}.{collection_name}
#   This creates:
#     {namespace}/{collection_name}/
#       galaxy.yml         — collection metadata
#       README.md
#       plugins/
#         modules/         — custom modules (.py files)
#         inventory/       — dynamic inventory plugins
#         filter/          — custom Jinja2 filter plugins
#       roles/             — bundled roles
#       playbooks/         — bundled playbooks
#       tests/
# ─────────────────────────────────────────────────────────────────────────────

# ── galaxy.yml ───────────────────────────────────────────────────────────────
namespace: {namespace}
name: {collection_name}
version: 1.0.0
readme: README.md
description: Collection for {purpose}
license:
  - Apache-2.0
authors:
  - Your Name <you@example.com>
dependencies:
  "community.general": ">=8.0.0"
  "ansible.posix": ">=1.5.0"
  # Add other collection deps here

# ── plugins/modules/example_module.py ────────────────────────────────────────
# #!/usr/bin/python
# from __future__ import absolute_import, division, print_function
# __metaclass__ = type
# DOCUMENTATION = r\"\"\"
# ---
# module: example_module
# short_description: Example module for {collection_name}
# version_added: "1.0.0"
# options:
#   name:
#     description: Name parameter
#     required: true
#     type: str
# \"\"\"
# from ansible.module_utils.basic import AnsibleModule
# def run_module():
#     module_args = dict(name=dict(type='str', required=True))
#     module = AnsibleModule(argument_spec=module_args, supports_check_mode=True)
#     result = dict(changed=False, name=module.params['name'])
#     module.exit_json(**result)
# if __name__ == '__main__':
#     run_module()

# ── Build and install locally ─────────────────────────────────────────────────
# ansible-galaxy collection build {namespace}/{collection_name}
# ansible-galaxy collection install {namespace}-{collection_name}-1.0.0.tar.gz
"""


def _extract_codegen_topic(text: str) -> str:
    """Pull a short topic phrase from a code-gen request for use in scaffolds.

    Handles patterns like:
      "write me a playbook to install satellite"  -> "install satellite"
      "write an ansible playbook to register RHEL" -> "register RHEL"
      "create an ansible role for nginx"           -> "nginx"
      "write a jinja2 template for nginx config"   -> "nginx config"
      "write a python script to check status"      -> "check subscription status"
    """
    q = text.strip()
    # Stage 1: strip "verb [me] [a/an] [ansible/python/jinja2] artifact [to|for]"
    q = re.sub(
        r'^(?:write|create|generate|build|make|scaffold|give\s+me|show\s+me|draft|produce)'
        r'\s+(?:me\s+)?(?:an?\s+)?(?:ansible\s+|python\s+|jinja2?\s+)?'
        r'(?:playbook|play\s*book|role|collection|task|handler|module|script|template|j2\s+template)'
        r'\s+(?:to|for|that|which)\s+',
        '', q, flags=re.IGNORECASE)
    # Stage 2: strip bare "verb [me] [a/an]" if Stage 1 didn't match
    q = re.sub(r'^(?:write|create|generate|build|make)\s+me\s+(?:an?\s+)?', '', q, flags=re.IGNORECASE)
    q = re.sub(r'^(?:write|create|generate|build|make)\s+(?:an?\s+)?', '', q, flags=re.IGNORECASE)
    # Stage 3: remove trailing noise
    q = re.sub(r'\s+for\s+me\s*$', '', q, flags=re.IGNORECASE)
    return q.strip()[:120] if q.strip() else 'general automation'


def _llm_codegen(prompt: str) -> str | None:
    """Call the bridge with a strict code-generation system prompt; return output or None."""
    code_system = (
        "You are an expert Ansible/Python/Jinja2 code generator. "
        "ALWAYS respond with complete, production-ready code blocks using fenced markdown (```yaml, ```python, ```jinja2). "
        "Include a DEPENDENCIES section listing required pip packages, ansible-galaxy collections, and system packages. "
        "Do not explain — output ONLY: dependencies section, then code block(s). "
        "If the request requires multiple files, output each as a separate labelled fenced code block."
    )
    full_prompt = f"[SYSTEM: {code_system}]\n\nUser request: {prompt}"
    try:
        raw = call_bridge(full_prompt, timeout=90)
        text = extract_assistant_content(raw)
        # Require a non-trivial code block: opening fence + at least 3 non-empty lines inside
        if text and re.search(r'```[^\n]*\n(?:[^\n]+\n){3,}', text):
            return text
    except Exception:
        pass
    return None


# ── Known-product playbook templates ─────────────────────────────────────────

_PLAYBOOK_SATELLITE_INSTALL = '''\
---
# Ansible Playbook — Install Red Hat Satellite 6.x on RHEL 9
# Generated by HAL
#
# PREREQUISITES / DEPENDENCIES
# ─────────────────────────────
#   Ansible Galaxy collections (install before running):
#     ansible-galaxy collection install redhat.satellite
#     ansible-galaxy collection install community.general
#     ansible-galaxy collection install ansible.posix
#   Python packages:
#     pip install apypie                 # required by redhat.satellite modules
#
# INVENTORY (inventory/hosts.yml):
#   satellite:
#     hosts:
#       satellite.example.com:
#         ansible_user: root
#
# REQUIRED VARIABLES (pass with -e or set in group_vars/satellite.yml):
#   satellite_version: "6.15"           # e.g. 6.13, 6.14, 6.15
#   rhsm_username: "your-rhn-user"
#   rhsm_password: "your-rhn-password"  # use ansible-vault
#   satellite_admin_username: "admin"
#   satellite_admin_password: "changeme"
#   satellite_initial_organization: "Default Organization"
#   satellite_initial_location: "Default Location"
#
# USAGE:
#   ansible-playbook -i inventory/hosts.yml install_satellite.yml \\
#     -e satellite_version=6.15 \\
#     -e rhsm_username=user \\
#     -e @secrets.yml --ask-vault-pass
# ─────────────────────────────────────────────────────────────────────────────

- name: Install Red Hat Satellite {{ satellite_version }} on RHEL 9
  hosts: satellite
  gather_facts: true
  become: true

  vars:
    satellite_version: "6.15"
    satellite_admin_username: "admin"
    satellite_admin_password: "changeme"      # CHANGE: use ansible-vault
    satellite_initial_organization: "Default Organization"
    satellite_initial_location: "Default Location"
    rhsm_username: ""                         # CHANGE or pass with -e
    rhsm_password: ""                         # CHANGE: use ansible-vault
    satellite_fqdn: "{{ ansible_fqdn }}"
    satellite_tune_size: "default"            # options: default, medium, large, extra-large
    firewall_ports:
      - "53/tcp"    # DNS
      - "53/udp"
      - "67/udp"    # DHCP
      - "69/udp"    # TFTP
      - "80/tcp"    # HTTP
      - "443/tcp"   # HTTPS
      - "3000/tcp"  # Foreman Node.js
      - "3306/tcp"  # MySQL (internal)
      - "5432/tcp"  # PostgreSQL (internal)
      - "5646/tcp"  # Candlepin
      - "5647/tcp"  # Qpid
      - "8000/tcp"  # Provisioning/PXE
      - "8140/tcp"  # Puppet
      - "8443/tcp"  # Smart Proxy
      - "9090/tcp"  # Smart Proxy HTTPS

  pre_tasks:
    - name: Verify RHEL 9 is the target OS
      ansible.builtin.assert:
        that:
          - ansible_distribution == "RedHat"
          - ansible_distribution_major_version | int == 9
        fail_msg: "This playbook requires RHEL 9. Detected: {{ ansible_distribution }} {{ ansible_distribution_version }}"

    - name: Check minimum RAM (20 GB required)
      ansible.builtin.assert:
        that: ansible_memtotal_mb | int >= 20480
        fail_msg: "Satellite requires at least 20 GB RAM. Found: {{ ansible_memtotal_mb }} MB"

    - name: Verify FQDN resolves
      ansible.builtin.command: hostname -f
      register: hostname_check
      changed_when: false
      failed_when: hostname_check.rc != 0 or '.' not in hostname_check.stdout

  tasks:
    # ── Step 1: Register to RHSM ─────────────────────────────────────────────
    - name: Register system to Red Hat Subscription Manager
      community.general.redhat_subscription:
        state: present
        username: "{{ rhsm_username }}"
        password: "{{ rhsm_password }}"
        auto_attach: false
      when: rhsm_username != ""
      no_log: true

    - name: Enable required RHSM repositories
      community.general.rhsm_repository:
        name:
          - "rhel-9-for-x86_64-baseos-rpms"
          - "rhel-9-for-x86_64-appstream-rpms"
          - "satellite-{{ satellite_version }}-for-rhel-9-x86_64-rpms"
          - "satellite-maintenance-{{ satellite_version }}-for-rhel-9-x86_64-rpms"
        state: enabled

    # ── Step 2: System preparation ────────────────────────────────────────────
    - name: Update all system packages
      ansible.builtin.dnf:
        name: "*"
        state: latest
        update_cache: true

    - name: Install satellite package group
      ansible.builtin.dnf:
        name: satellite
        state: present

    # ── Step 3: Configure firewall ────────────────────────────────────────────
    - name: Enable firewalld
      ansible.builtin.service:
        name: firewalld
        state: started
        enabled: true

    - name: Open Satellite firewall ports
      ansible.posix.firewalld:
        port: "{{ item }}"
        permanent: true
        state: enabled
        immediate: true
      loop: "{{ firewall_ports }}"

    # ── Step 4: Run satellite-installer ───────────────────────────────────────
    - name: Run satellite-installer
      ansible.builtin.command: >
        satellite-installer --scenario satellite
        --foreman-initial-admin-username {{ satellite_admin_username }}
        --foreman-initial-admin-password {{ satellite_admin_password }}
        --foreman-initial-organization "{{ satellite_initial_organization }}"
        --foreman-initial-location "{{ satellite_initial_location }}"
        --tuning {{ satellite_tune_size }}
      register: satellite_installer_result
      changed_when: "'Success' in satellite_installer_result.stdout"
      failed_when: satellite_installer_result.rc != 0
      no_log: false
      timeout: 1800   # 30 min timeout

    # ── Step 5: Verify installation ───────────────────────────────────────────
    - name: Check Satellite service status
      ansible.builtin.command: satellite-maintain service status
      register: service_status
      changed_when: false

    - name: Display Satellite URL
      ansible.builtin.debug:
        msg:
          - "Satellite installation complete!"
          - "URL  : https://{{ satellite_fqdn }}"
          - "Login: {{ satellite_admin_username }}"
          - "Org  : {{ satellite_initial_organization }}"

  handlers:
    - name: Restart foreman
      ansible.builtin.service:
        name: foreman
        state: restarted
'''

_PLAYBOOK_SATELLITE_REGISTER_HOST = '''\
---
# Ansible Playbook — Register RHEL hosts to Red Hat Satellite
# Generated by HAL
#
# PREREQUISITES / DEPENDENCIES
#   ansible-galaxy collection install redhat.satellite
#   ansible-galaxy collection install community.general
#   pip install apypie
#
# USAGE:
#   ansible-playbook -i inventory/hosts.yml register_to_satellite.yml \\
#     -e satellite_hostname=satellite.example.com \\
#     -e activationkey=rhel9-prod \\
#     -e organization="Default Organization"
# ─────────────────────────────────────────────────────────────────────────────

- name: Register RHEL hosts to Satellite
  hosts: all
  gather_facts: true
  become: true

  vars:
    satellite_hostname: "satellite.example.com"   # CHANGE
    satellite_ca_cert_url: "https://{{ satellite_hostname }}/pub/katello-ca-consumer-latest.noarch.rpm"
    organization: "Default Organization"          # CHANGE
    activationkey: "rhel9-prod"                  # CHANGE

  tasks:
    - name: Download Katello CA certificate
      ansible.builtin.dnf:
        name: "{{ satellite_ca_cert_url }}"
        state: present
        disable_gpg_check: true

    - name: Register with subscription-manager to Satellite
      community.general.redhat_subscription:
        state: present
        server_hostname: "{{ satellite_hostname }}"
        activationkey: "{{ activationkey }}"
        org_id: "{{ organization }}"
        auto_attach: false

    - name: Enable required repositories via Satellite
      community.general.rhsm_repository:
        name:
          - "rhel-9-for-x86_64-baseos-rpms"
          - "rhel-9-for-x86_64-appstream-rpms"
        state: enabled

    - name: Install katello-agent / insights-client
      ansible.builtin.dnf:
        name:
          - katello-host-tools
          - insights-client
        state: present

    - name: Register with Red Hat Insights
      ansible.builtin.command: insights-client --register
      register: insights_result
      changed_when: "'Successfully registered' in insights_result.stdout"
      failed_when: false
'''

_PLAYBOOK_IDM_CLIENT = '''\
---
# Ansible Playbook — Enroll RHEL hosts into Red Hat IdM (FreeIPA client)
# Generated by HAL
#
# PREREQUISITES / DEPENDENCIES
#   ansible-galaxy collection install redhat.rhel_idm
#   ansible-galaxy collection install ansible.posix
#
# USAGE:
#   ansible-playbook -i inventory/hosts.yml enroll_idm.yml \\
#     -e idm_server=idm.example.com \\
#     -e idm_domain=example.com \\
#     -e idm_realm=EXAMPLE.COM \\
#     -e idm_admin_password=secret   # use ansible-vault
# ─────────────────────────────────────────────────────────────────────────────

- name: Enroll RHEL hosts into Red Hat IdM
  hosts: all
  gather_facts: true
  become: true

  vars:
    idm_server: "idm.example.com"        # CHANGE
    idm_domain: "example.com"            # CHANGE
    idm_realm: "EXAMPLE.COM"            # CHANGE (uppercase)
    idm_admin_password: "changeme"       # CHANGE: use ansible-vault
    idm_mkhomedir: true

  tasks:
    - name: Install ipa-client package
      ansible.builtin.dnf:
        name: ipa-client
        state: present

    - name: Enroll host into IdM
      redhat.rhel_idm.ipaclient:
        state: present
        ipaservers: ["{{ idm_server }}"]
        ipadomain: "{{ idm_domain }}"
        iparealm: "{{ idm_realm }}"
        ipaadmin_password: "{{ idm_admin_password }}"
        ipaforce_join: false
        ipamkhomedir: "{{ idm_mkhomedir }}"
      no_log: true

    - name: Enable SSSD and oddjobd services
      ansible.builtin.service:
        name: "{{ item }}"
        state: started
        enabled: true
      loop:
        - sssd
        - oddjobd
'''

_PLAYBOOK_AAP_DEPLOY = '''\
---
# Ansible Playbook — Deploy Ansible Automation Platform (AAP) 2.x
# Generated by HAL
#
# PREREQUISITES / DEPENDENCIES
#   This playbook uses the official Red Hat AAP installer bundle.
#   Download from: https://access.redhat.com/downloads (Ansible Automation Platform)
#   Unpack and set aap_installer_dir to the extracted directory.
#
#   ansible-galaxy collection install infra.controller_configuration
#   ansible-galaxy collection install ansible.posix
#
# USAGE:
#   ansible-playbook -i inventory/hosts.yml deploy_aap.yml \\
#     -e aap_installer_dir=/opt/aap-installer \\
#     -e @vault.yml --ask-vault-pass
# ─────────────────────────────────────────────────────────────────────────────

- name: Deploy Ansible Automation Platform 2.x
  hosts: localhost
  gather_facts: false
  become: false

  vars:
    aap_installer_dir: "/opt/aap-installer"   # CHANGE: path to extracted bundle
    aap_inventory_file: "{{ aap_installer_dir }}/inventory"

  tasks:
    - name: Verify installer bundle exists
      ansible.builtin.stat:
        path: "{{ aap_installer_dir }}/setup.sh"
      register: installer_stat
      failed_when: not installer_stat.stat.exists

    - name: Ensure inventory file exists
      ansible.builtin.stat:
        path: "{{ aap_inventory_file }}"
      register: inv_stat
      failed_when: not inv_stat.stat.exists

    - name: Run AAP installer
      ansible.builtin.command:
        cmd: ./setup.sh -i {{ aap_inventory_file }}
        chdir: "{{ aap_installer_dir }}"
      register: aap_install_result
      changed_when: true
      timeout: 3600   # 60 min timeout

    - name: Display installer output summary
      ansible.builtin.debug:
        msg: "{{ aap_install_result.stdout_lines[-20:] }}"
'''

_PLAYBOOK_INSIGHTS_REGISTER = '''\
---
# Ansible Playbook — Register RHEL hosts to Red Hat Insights
# Generated by HAL
#
# PREREQUISITES / DEPENDENCIES
#   ansible-galaxy collection install redhat.insights
#   ansible-galaxy collection install community.general
#
# USAGE:
#   ansible-playbook -i inventory/hosts.yml register_insights.yml
# ─────────────────────────────────────────────────────────────────────────────

- name: Register RHEL hosts with Red Hat Insights
  hosts: all
  gather_facts: true
  become: true

  vars:
    insights_display_name: "{{ inventory_hostname }}"   # optional custom name
    insights_proxy_url: ""                               # optional proxy

  tasks:
    - name: Install insights-client
      ansible.builtin.dnf:
        name: insights-client
        state: present

    - name: Configure Insights proxy (if needed)
      ansible.builtin.lineinfile:
        path: /etc/insights-client/insights-client.conf
        regexp: '^proxy='
        line: "proxy={{ insights_proxy_url }}"
        state: "{{ 'present' if insights_proxy_url else 'absent' }}"

    - name: Register with Insights
      ansible.builtin.command: insights-client --register --display-name="{{ insights_display_name }}"
      register: insights_reg
      changed_when: "'Successfully registered' in insights_reg.stdout"
      failed_when: insights_reg.rc != 0 and 'already registered' not in insights_reg.stderr

    - name: Run initial Insights collection
      ansible.builtin.command: insights-client --collect-only
      changed_when: false
      failed_when: false

    - name: Display registration status
      ansible.builtin.debug:
        msg: "{{ insights_reg.stdout_lines }}"
'''

# Map of (product_pattern, optional_action_pattern) -> playbook string
_KNOWN_PRODUCT_PLAYBOOKS = [
    # (product_re, action_re, title, playbook_str)
    (r'\bsatellite\b', r'\b(install|deploy|setup|set\s*up)\b', 'Install Red Hat Satellite 6.x on RHEL 9', _PLAYBOOK_SATELLITE_INSTALL),
    (r'\bsatellite\b', r'\b(register|enroll|add\s+host|subscribe)\b', 'Register RHEL hosts to Satellite', _PLAYBOOK_SATELLITE_REGISTER_HOST),
    (r'\bsatellite\b', None, 'Register RHEL hosts to Satellite', _PLAYBOOK_SATELLITE_REGISTER_HOST),
    (r'\b(idm|freeipa|ipa\s+client|identity\s+management)\b', None, 'Enroll RHEL hosts into Red Hat IdM', _PLAYBOOK_IDM_CLIENT),
    (r'\b(aap|ansible\s+automation\s+platform)\b', r'\b(install|deploy|setup)\b', 'Deploy Ansible Automation Platform 2.x', _PLAYBOOK_AAP_DEPLOY),
    (r'\b(insights|red\s*hat\s+insights)\b', None, 'Register RHEL hosts with Red Hat Insights', _PLAYBOOK_INSIGHTS_REGISTER),
]


def _match_known_product_playbook(query: str) -> tuple[str, str] | None:
    """Return (title, playbook_yaml) if query matches a known product, else None."""
    q = query.lower()
    for product_re, action_re, title, playbook in _KNOWN_PRODUCT_PLAYBOOKS:
        if not re.search(product_re, q):
            continue
        if action_re and not re.search(action_re, q):
            continue
        return title, playbook
    return None


def generate_ansible_codegen_response(query: str) -> str:
    """Generate an Ansible playbook, role, collection, or task — LLM-first with deterministic scaffold fallback."""
    q_lower = query.lower()
    topic = _extract_codegen_topic(query)

    # Determine artifact type
    if re.search(r'\bcollection\b', q_lower):
        artifact = 'collection'
    elif re.search(r'\brole\b', q_lower):
        artifact = 'role'
    else:
        artifact = 'playbook'

    # For playbooks: check known-product library first (real, fully-filled playbooks)
    if artifact == 'playbook':
        product_match = _match_known_product_playbook(query)
        if product_match:
            title, playbook_yaml = product_match
            return (
                f"HAL | Ansible Playbook — {title}\n"
                f"{'─' * 60}\n"
                f"```yaml\n{playbook_yaml}\n```\n"
                f"{'─' * 60}\n"
                "NEXT STEPS:\n"
                "  1. Update all CHANGE markers with your environment values\n"
                "  2. Protect passwords with: ansible-vault encrypt_string 'secret' --name var\n"
                "  3. Install collections: ansible-galaxy collection install -r requirements.yml\n"
                "  4. Dry run: ansible-playbook -i inventory/ playbook.yml --check --diff\n"
            )

    # Try LLM next (when bridge is up it returns real targeted code)
    llm_out = _llm_codegen(query)
    if llm_out:
        header = (
            f"HAL | Ansible {artifact.title()} — {topic}\n"
            f"{'─' * 60}\n"
        )
        footer = (
            f"\n{'─' * 60}\n"
            "TIP: Validate with `ansible-lint` and test with `--check --diff` before applying.\n"
            "Collections: ansible-galaxy collection install -r requirements.yml\n"
        )
        return header + llm_out + footer

    # Bridge offline — generic starter scaffold
    if artifact == 'collection':
        parts = re.search(r'(\w+)[\.\s]+(\w+)\s+collection', q_lower)
        ns = parts.group(1) if parts else 'myorg'
        cn = parts.group(2) if parts else re.sub(r'\W+', '_', topic)[:30]
        scaffold = _ANSIBLE_SCAFFOLD_COLLECTION.format(
            namespace=ns, collection_name=cn, purpose=topic)
    elif artifact == 'role':
        role_name = re.sub(r'\W+', '_', topic)[:40].strip('_') or 'myrole'
        scaffold = _ANSIBLE_SCAFFOLD_ROLE.format(role_name=role_name)
    else:
        filename = re.sub(r'\W+', '_', topic)[:40].strip('_') or 'site'
        scaffold = _ANSIBLE_SCAFFOLD_PLAYBOOK.format(
            purpose=topic, filename=f"{filename}.yml")

    return (
        f"HAL | Ansible {artifact.title()} Scaffold — {topic}\n"
        f"(Bridge offline — returning starter scaffold; fill in the TODO sections)\n"
        f"{'─' * 60}\n"
        f"```yaml\n{scaffold}\n```\n"
        f"{'─' * 60}\n"
        "NEXT STEPS:\n"
        "  1. Fill in all TODO markers with real tasks\n"
        "  2. Run: ansible-lint your_playbook.yml\n"
        "  3. Test: ansible-playbook -i inventory/ your_playbook.yml --check --diff\n"
    )


# ── Jinja2 template generation ────────────────────────────────────────────────

_JINJA2_SCAFFOLD = """\
{#
  Jinja2 Template — generated by HAL
  Purpose: {purpose}

  PREREQUISITES
  ─────────────
  In Ansible:
    No extra installs — ansible-core includes Jinja2.
  Standalone Python:
    pip install Jinja2          # core engine
    pip install jinja2-time     # optional: date/time extension
  Usage in Python:
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader('templates/'))
    tmpl = env.get_template('{filename}')
    print(tmpl.render(variable='value'))

  Common Jinja2 built-in filters:
    | lower, | upper, | capitalize, | title
    | default('fallback'), | mandatory
    | int, | float, | string, | bool
    | trim, | replace('a','b'), | regex_replace('^foo', 'bar')
    | join(','), | list, | sort, | unique
    | to_json, | to_yaml, | from_json, | from_yaml
    | b64encode, | b64decode
    | ipaddr, | ipv4, | ipv6            (requires netaddr: pip install netaddr)
    | dirname, | basename, | expanduser
    | hash('sha256')
#}

{# ── Variable definitions (set or pass in from calling context) ── #}
{%- set app_name = app_name | default('myapp') -%}
{%- set environment = environment | default('production') -%}
{%- set debug_mode = debug_mode | default(false) | bool -%}
{%- set server_list = server_list | default([]) -%}

# Managed by Ansible/HAL — do not edit manually
# Generated: {{ ansible_date_time.iso8601 | default('') }}
# Purpose  : {{ app_name }} — {{ environment }} configuration

[general]
app_name  = {{ app_name }}
env       = {{ environment }}
debug     = {{ debug_mode | lower }}

[servers]
{%- for server in server_list %}
server_{{ loop.index }} = {{ server }}
{%- else %}
# No servers defined — set server_list variable
{%- endfor %}

[feature_flags]
{%- if environment == 'production' %}
enable_caching = true
log_level      = warn
{%- elif environment == 'staging' %}
enable_caching = true
log_level      = info
{%- else %}
enable_caching = false
log_level      = debug
{%- endif %}

{# ── Example: macro for repeated config block ── #}
{%- macro render_endpoint(name, host, port=8080) %}
[{{ name }}]
host = {{ host }}
port = {{ port }}
url  = http://{{ host }}:{{ port }}/api
{%- endmacro %}

{{ render_endpoint('api', 'api.internal', 8080) }}
"""


def generate_jinja2_template_response(query: str) -> str:
    """Generate a Jinja2 template — LLM-first with deterministic scaffold fallback."""
    topic = _extract_codegen_topic(query)

    llm_out = _llm_codegen(query)
    if llm_out:
        return (
            f"HAL | Jinja2 Template — {topic}\n"
            f"{'─' * 60}\n"
            f"{llm_out}\n"
            f"{'─' * 60}\n"
            "TIP: Test with: python3 -c \"from jinja2 import Template; print(Template(open('tmpl.j2').read()).render(var='val'))\"\n"
        )

    filename = re.sub(r'\W+', '_', topic)[:40].strip('_') or 'config'
    scaffold = _JINJA2_SCAFFOLD.replace('{purpose}', topic).replace('{filename}', f"{filename}.j2")
    return (
        f"HAL | Jinja2 Template Scaffold — {topic}\n"
        f"(Bridge offline — returning starter scaffold)\n"
        f"{'─' * 60}\n"
        f"```jinja2\n{scaffold}\n```\n"
        f"{'─' * 60}\n"
        "NEXT STEPS:\n"
        "  1. Set your variables and fill in the config sections\n"
        f"  2. Place file as templates/{filename}.j2 in your Ansible role/playbook\n"
        "  3. Use ansible.builtin.template module to deploy it\n"
    )


# ── Python script generation ──────────────────────────────────────────────────

_PYTHON_SCAFFOLD = """\
#!/usr/bin/env python3
\"\"\"
{purpose}
Generated by HAL.

PREREQUISITES / DEPENDENCIES
─────────────────────────────
System packages:
  dnf install -y python3 python3-pip git

Common pip packages (install what you need):
  pip install requests          # HTTP client
  pip install boto3             # AWS/S3 automation
  pip install pyvmomi           # vSphere/VMware automation
  pip install paramiko          # SSH client
  pip install ansible-runner    # Invoke Ansible from Python
  pip install jinja2            # Templating
  pip install pyyaml            # YAML parsing (yaml module)
  pip install netaddr           # IP address manipulation
  pip install cryptography      # TLS/cert operations
  pip install ldap3             # LDAP/IdM queries
  pip install python-gitlab     # GitLab API
  pip install pygithub          # GitHub API
  pip install rich              # Pretty terminal output
  pip install click             # CLI argument parsing (alternative to argparse)

Git operations (no pip needed — uses subprocess or gitpython):
  pip install gitpython         # Git repo operations from Python
\"\"\"

import argparse
import json
import logging
import sys
from pathlib import Path

# Optional: uncomment as needed
# import os
# import subprocess
# import requests
# import yaml
# import boto3
# from jinja2 import Environment, FileSystemLoader

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_CONFIG_PATH = Path('/etc/myapp/config.yml')


# ── Core logic ────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    \"\"\"Main entry point. Returns exit code (0 = success, non-zero = failure).\"\"\"
    log.info("Starting: {purpose}")

    # TODO: implement your logic here
    log.info("Input: %s", args.input)

    # Example: read a file
    # data = Path(args.input).read_text()

    # Example: call an API
    # import requests
    # resp = requests.get('https://api.example.com/endpoint', timeout=30)
    # resp.raise_for_status()
    # payload = resp.json()

    # Example: write output
    result = {{"status": "ok", "message": "TODO — replace with real output"}}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["message"])

    return 0


# ── CLI argument parsing ───────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="{purpose}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('input', nargs='?', default='-',
                        help='Input file or value (default: stdin)')
    parser.add_argument('-o', '--output', default='-',
                        help='Output file (default: stdout)')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG_PATH,
                        help='Config file path')
    parser.add_argument('--json', action='store_true',
                        help='Emit JSON output')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable debug logging')
    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    sys.exit(run(args))


if __name__ == '__main__':
    main()
"""


def generate_python_script_response(query: str) -> str:
    """Generate a Python script — LLM-first with deterministic scaffold fallback."""
    topic = _extract_codegen_topic(query)

    llm_out = _llm_codegen(query)
    if llm_out:
        return (
            f"HAL | Python Script — {topic}\n"
            f"{'─' * 60}\n"
            f"{llm_out}\n"
            f"{'─' * 60}\n"
            "TIP: Run `python3 -m py_compile script.py` to check syntax before running.\n"
        )

    filename = re.sub(r'\W+', '_', topic)[:40].strip('_') or 'script'
    scaffold = _PYTHON_SCAFFOLD.format(purpose=topic)
    return (
        f"HAL | Python Script Scaffold — {topic}\n"
        f"(Bridge offline — returning starter scaffold)\n"
        f"{'─' * 60}\n"
        f"```python\n{scaffold}\n```\n"
        f"{'─' * 60}\n"
        f"Save as: {filename}.py\n"
        "NEXT STEPS:\n"
        "  1. Install needed packages: pip install <package>\n"
        "  2. Fill in the run() function with real logic\n"
        "  3. Test: python3 {filename}.py --help\n"
    )


# ── Dependency advisor ────────────────────────────────────────────────────────

_DEPENDENCY_MATRIX = {
    'ansible': {
        'system': [
            'dnf install -y python3 python3-pip git',
            'dnf install -y ansible-core          # RHEL 9/10 AppStream',
            'pip install ansible-lint              # linting',
            'pip install ansible-runner            # invoke Ansible from Python',
        ],
        'collections': [
            'ansible-galaxy collection install community.general      # general Linux modules',
            'ansible-galaxy collection install ansible.posix          # POSIX/Linux modules',
            'ansible-galaxy collection install ansible.netcommon      # networking modules',
            'ansible-galaxy collection install redhat.satellite       # Satellite 6/7',
            'ansible-galaxy collection install redhat.rhel_idm        # Red Hat IdM / FreeIPA',
            'ansible-galaxy collection install redhat.insights        # Red Hat Insights',
            'ansible-galaxy collection install infra.ah_configuration # Automation Hub',
            'ansible-galaxy collection install infra.controller_configuration  # AAP Controller',
            'ansible-galaxy collection install community.vmware       # VMware vSphere',
            'ansible-galaxy collection install amazon.aws             # AWS automation',
            'ansible-galaxy collection install azure.azcollection     # Azure automation',
            'ansible-galaxy collection install community.postgresql   # PostgreSQL',
            'ansible-galaxy collection install community.mysql        # MySQL/MariaDB',
            'ansible-galaxy collection install community.docker       # Docker/Podman',
        ],
        'python_for_modules': [
            'pip install netaddr                   # IP address/network modules (ipaddr filter)',
            'pip install boto3 botocore            # amazon.aws collection',
            'pip install PyVmomi                   # community.vmware collection',
            'pip install azure-mgmt-compute        # azure.azcollection',
            'pip install python-ldap ldap3         # LDAP/IdM automation',
            'pip install requests                  # generic REST API modules',
            'pip install pyyaml                    # YAML parsing in custom modules',
            'pip install cryptography              # certificate/TLS modules',
            'pip install jinja2                    # template rendering (built into ansible-core)',
        ],
    },
    'jinja2': {
        'system': [
            'dnf install -y python3 python3-pip',
        ],
        'pip': [
            'pip install Jinja2                    # core engine (also included in ansible-core)',
            'pip install jinja2-time               # now() / datetime extension',
            'pip install MarkupSafe                # auto-installed with Jinja2',
        ],
        'python_imports': [
            'from jinja2 import Environment, FileSystemLoader',
            'from jinja2 import Template                       # single string template',
            'from jinja2 import StrictUndefined               # fail on undefined vars',
            'from jinja2.exceptions import UndefinedError, TemplateSyntaxError',
        ],
    },
    'python': {
        'system': [
            'dnf install -y python3 python3-pip python3-venv git',
        ],
        'pip': [
            'pip install requests                  # HTTP',
            'pip install boto3                     # AWS',
            'pip install pyyaml                    # YAML',
            'pip install click                     # CLI framework',
            'pip install rich                      # pretty output',
            'pip install paramiko                  # SSH',
            'pip install ansible-runner            # Ansible from Python',
            'pip install gitpython                 # Git operations',
            'pip install cryptography              # TLS/certs',
            'pip install netaddr                   # IP/network',
            'pip install python-dateutil           # date parsing',
            'pip install psycopg2-binary           # PostgreSQL',
            'pip install pymysql                   # MySQL',
            'pip install redis                     # Redis',
            'pip install celery                    # async task queue',
            'pip install fastapi uvicorn           # REST API server',
            'pip install pydantic                  # data validation',
            'pip install pytest                    # unit testing',
            'pip install black isort flake8        # code quality',
        ],
        'stdlib_imports': [
            'import os, sys, re, json, yaml, subprocess, pathlib',
            'from pathlib import Path',
            'import argparse                       # CLI arg parsing',
            'import logging                        # structured logging',
            'import shutil                         # file/dir operations',
            'import hashlib, hmac                  # hashing/signing',
            'import base64                         # encoding',
            'import socket                         # network/hostname',
            'import datetime                       # date/time',
            'from typing import Optional, List, Dict, Any',
            'from dataclasses import dataclass, field',
            'from functools import lru_cache       # memoization',
            'import concurrent.futures             # parallel execution',
            'import contextlib                     # context managers',
            'from collections import defaultdict, Counter',
        ],
    },
    'git': {
        'system': [
            'dnf install -y git git-lfs',
        ],
        'pip': [
            'pip install gitpython                 # full Git repo API from Python',
            'pip install pygit2                    # libgit2 bindings (faster)',
        ],
        'common_git_commands': [
            'git init / git clone <url>',
            'git add -A && git commit -m "msg"',
            'git push origin <branch>',
            'git pull --rebase',
            'git branch -b <feature>',
            'git log --oneline --graph --all',
            'git stash / git stash pop',
            'git tag -a v1.0.0 -m "release"',
            'git submodule add <url>',
        ],
    },
}


def generate_dependency_advice(query: str) -> str:
    """Return a formatted dependency/import guide for Ansible, Jinja2, Python, or Git."""
    q = query.lower()

    domains_requested: list[str] = []
    if re.search(r'\bansible\b', q):
        domains_requested.append('ansible')
    if re.search(r'\bjinja2?\b', q):
        domains_requested.append('jinja2')
    if re.search(r'\bgit\b', q):
        domains_requested.append('git')
    if re.search(r'\bpython\b', q):
        domains_requested.append('python')
    if not domains_requested:
        domains_requested = list(_DEPENDENCY_MATRIX.keys())

    lines = [
        "HAL | Dependency & Import Reference",
        "=" * 60,
    ]
    for domain in domains_requested:
        info = _DEPENDENCY_MATRIX.get(domain, {})
        lines.append(f"\n── {domain.upper()} ──────────────────────────────────────────")
        for category, items in info.items():
            lines.append(f"\n  {category.replace('_', ' ').title()}:")
            for item in items:
                lines.append(f"    {item}")

    lines += [
        "",
        "=" * 60,
        "QUICKSTART — install all common deps:",
        "  dnf install -y python3 python3-pip git ansible-core",
        "  pip install ansible-lint netaddr requests pyyaml boto3 jinja2 rich",
        "  ansible-galaxy collection install community.general ansible.posix",
        "",
        "Create a requirements.yml to pin collections:",
        "  ---",
        "  collections:",
        "    - name: community.general",
        "      version: '>=8.0.0'",
        "    - name: ansible.posix",
        "    - name: redhat.satellite",
        "",
        "Install from requirements.yml:",
        "  ansible-galaxy collection install -r requirements.yml",
    ]
    return '\n'.join(lines)


def _walk_key_values(obj, prefix=''):
    """Yield (key_path, value) pairs recursively for dict/list structures."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f'{prefix}.{k}' if prefix else str(k)
            yield kp, v
            yield from _walk_key_values(v, kp)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            kp = f'{prefix}[{i}]' if prefix else f'[{i}]'
            yield kp, v
            yield from _walk_key_values(v, kp)


def _extract_subscription_count(record: dict, product: str) -> str:
    """Best-effort extraction of subscription counts for a product from an intel record.

    Returns string count, or 'unknown' when product is present but count not explicit,
    or empty string when there is no product signal.
    """
    product = product.lower()
    product_tokens = {
        'rhel': ('rhel', 'red hat enterprise linux', 'enterprise linux'),
        'aap': ('aap', 'ansible automation platform', 'ansible'),
        'openshift': ('openshift', 'ocp'),
    }[product]

    key_hints = {
        'rhel': ('rhel', 'enterprise_linux', 'linux'),
        'aap': ('aap', 'ansible', 'automation_platform'),
        'openshift': ('openshift', 'ocp'),
    }[product]

    count_hints = ('subscription', 'subscriptions', 'subs', 'entitlement', 'entitlements', 'count', 'qty', 'quantity')

    # 1) Structured numeric extraction from keys resembling product+count fields.
    for key_path, value in _walk_key_values(record):
        k = key_path.lower()
        if any(ph in k for ph in key_hints) and any(ch in k for ch in count_hints):
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return str(int(value))
            if isinstance(value, str):
                m = re.search(r'\b(\d{1,9})\b', value)
                if m:
                    return m.group(1)

    # 2) Regex extraction from full record text.
    blob = json.dumps(record).lower()
    token_pat = '|'.join(re.escape(t) for t in product_tokens)
    regexes = [
        rf'(?:{token_pat})[^0-9]{{0,60}}(?:subscriptions?|subs?|entitlements?)[^0-9]{{0,12}}(\d+)',
        rf'(?:subscriptions?|subs?|entitlements?)[^0-9]{{0,12}}(\d+)[^a-z]{{0,20}}(?:{token_pat})',
    ]
    for pat in regexes:
        m = re.search(pat, blob)
        if m:
            return m.group(1)

    # 3) If product signal exists but no count is explicit, mark as unknown.
    tags_blob = ' '.join(str(x) for x in (
        record.get('tags', []),
        record.get('stack_signals', []),
        record.get('redhat_focus_areas', []),
        record.get('short_summary', ''),
    )).lower()
    if any(tok in tags_blob for tok in product_tokens) or any(tok in blob for tok in product_tokens):
        return 'unknown'

    return ''


def generate_subscription_csv_report() -> tuple[str, str] | tuple[None, None]:
    """Generate CSV of customers and product subscription counts from business intel."""
    if not os.path.exists(TRAIN_DIR):
        return None, None

    train_path = Path(TRAIN_DIR)
    rows = []

    for json_file in sorted(train_path.glob('intel-*.json'), reverse=True):
        try:
            with open(json_file, 'r', encoding='utf-8') as fh:
                record = json.load(fh)
        except Exception:
            continue

        if record.get('type') != 'business_intel_account':
            continue

        account_name = (record.get('account_name') or '').strip().strip('"')
        if not account_name:
            continue
        if account_name.lower() in ('unknown', 'n/a', 'na', 'none', 'null'):
            continue

        rows.append({
            'customer': account_name,
            'rhel_subscriptions': _extract_subscription_count(record, 'rhel'),
            'aap_subscriptions': _extract_subscription_count(record, 'aap'),
            'openshift_subscriptions': _extract_subscription_count(record, 'openshift'),
        })

    if not rows:
        return None, None

    # Deduplicate by customer, keeping first (newest) row.
    dedup = {}
    for r in rows:
        dedup.setdefault(r['customer'].lower(), r)
    final_rows = sorted(dedup.values(), key=lambda x: x['customer'].lower())

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['customer', 'rhel_subscriptions', 'aap_subscriptions', 'openshift_subscriptions'])
    writer.writeheader()
    writer.writerows(final_rows)
    csv_text = output.getvalue()

    ensure_dirs()
    out_path = os.path.join(REPORTS_DIR, f'customer-subscriptions-{ts_now()}.csv')
    with open(out_path, 'w', encoding='utf-8', newline='') as fh:
        fh.write(csv_text)

    return csv_text, out_path


def generate_stock_price_response(account_query: str) -> str | None:
    """Generate a concise stock price response from business intel."""
    rec = _find_best_business_intel_record(account_query)
    if not rec:
        return None

    account = (rec.get('account_name') or account_query or 'Unknown Account').strip('"')
    ticker = str(rec.get('ticker') or '').strip()
    price = rec.get('stock_price_snapshot')
    date = str(rec.get('date') or rec.get('timestamp') or '').strip()

    # Best effort parse if stock snapshot is embedded in text.
    if (price is None or str(price).strip() == ''):
        blob = json.dumps(rec)
        m = re.search(r'"stock_price_snapshot"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?', blob)
        if m:
            price = m.group(1)

    if price is None or str(price).strip() == '':
        return (
            f"I found account intel for {account}, but no explicit stock price in the imported dataset.\n"
            f"Ticker: {ticker or 'unknown'}"
        )

    lines = []
    lines.append(f"Stock price snapshot for {account}:")
    lines.append(f"- Price: {price}")
    if ticker:
        lines.append(f"- Ticker: {ticker}")
    if date:
        lines.append(f"- As of: {date}")
    lines.append('')
    lines.append('Note: This is a stored snapshot from your imported business intel, not a live market feed.')
    return '\n'.join(lines)


def generate_server_update_strategy_response() -> str:
    """Generate a practical deterministic server update strategy."""
    lines = []
    lines.append('Server Update Strategy')
    lines.append('=' * 72)
    lines.append('1. Preparation (Day 0-1)')
    lines.append('- Build inventory by criticality (prod, staging, dev) and maintenance windows.')
    lines.append('- Capture backups/snapshots and verify rollback procedure per tier.')
    lines.append('- Run dry-run validation first: ansible-playbook ansible/remediate-kaso.yml --check -v')
    lines.append('')
    lines.append('2. Pilot Ring (Day 2)')
    lines.append('- Patch 5-10% of non-critical nodes first.')
    lines.append('- Validate service health, boot success, and key app transactions for 24h.')
    lines.append('')
    lines.append('3. Broad Rollout (Day 3-7)')
    lines.append('- Roll out by ring: staging -> low-risk prod -> high-risk prod.')
    lines.append('- Use phased concurrency and pause gates between rings.')
    lines.append('- Apply fixes: sudo ansible-playbook ansible/remediate-kaso.yml')
    lines.append('')
    lines.append('4. Optional Full Package Upgrade')
    lines.append('- Only after pilot success and app-owner signoff:')
    lines.append('- sudo ansible-playbook ansible/remediate-kaso.yml -e allow_system_upgrade=true')
    lines.append('')
    lines.append('5. Post-Update Validation')
    lines.append('- Check CPU, audio, auditd, and service baselines from remediation outputs.')
    lines.append('- Confirm monitoring/alerts and capture change report for leadership.')
    lines.append('')
    lines.append('6. Rollback Criteria')
    lines.append('- Trigger rollback on failed health checks, performance regressions, or critical service impact.')
    return '\n'.join(lines)


def generate_satellite_patch_strategy_response(query: str) -> str:
    """Generate deterministic, practical Satellite patching guidance."""
    q = (query or '').lower()
    sat_ver = '6.18' if re.search(r'\b6\.18\b', q) else '6.x'

    lines = []
    lines.append(f'Satellite {sat_ver} Patch Strategy (Practical Runbook)')
    lines.append('=' * 72)
    lines.append('1. Pre-Change Preparation')
    lines.append('- Read the latest Satellite release notes, known issues, and required upgrade path for your current z-stream.')
    lines.append('- Confirm full backups: Satellite DB, Pulp/content storage, and VM or filesystem snapshots.')
    lines.append('- Verify DNS/FQDN resolution, cert validity, and time sync before maintenance window.')
    lines.append('')
    lines.append('2. Health Baseline and Risk Check')
    lines.append('- Run pre-checks for task backlog, service health, and free disk/inode capacity.')
    lines.append('- Validate Capsules are in sync and host/content operations are stable before patching.')
    lines.append('- Freeze non-essential changes (content view promotions, template edits, lifecycle moves).')
    lines.append('')
    lines.append('3. Stage Then Production')
    lines.append('- Patch a non-production Satellite/Capsule first and execute a smoke test matrix.')
    lines.append('- Smoke tests: host registration, errata applicability, content view publish/promote, remote execution job.')
    lines.append('- Promote to production only after non-prod checks pass with no critical regressions.')
    lines.append('')
    lines.append('4. Patch Execution Window')
    lines.append('- Stop or pause high-volume jobs during maintenance to reduce contention.')
    lines.append('- Apply Satellite packages/updates using the supported vendor procedure for your deployment model.')
    lines.append('- Run post-update upgrade/check command sequence and confirm all core services return healthy.')
    lines.append('')
    lines.append('5. Post-Patch Validation')
    lines.append('- Re-run smoke tests: content sync, registration, CV promotion, activation key flows, and PXE/provisioning if used.')
    lines.append('- Validate API responsiveness and automation integration (AAP/MCP jobs, if configured).')
    lines.append('- Monitor logs and queue latency for 24h hypercare.')
    lines.append('')
    lines.append('6. Rollback Triggers')
    lines.append('- Roll back if core services fail to stabilize, content operations fail repeatedly, or provisioning breaks in production.')
    lines.append('- Use documented restore path (DB + content + snapshot) and communicate outage/update status immediately.')
    lines.append('')
    lines.append('Suggested first actions today:')
    lines.append('1. Validate backup and restore drill time for Satellite and content volumes.')
    lines.append('2. Build a non-prod smoke test checklist tied to your critical workflows.')
    lines.append('3. Schedule a staged patch window (non-prod first, then production).')
    return '\n'.join(lines)


def generate_aap_patch_strategy_response(query: str) -> str:
    """Generate deterministic patch guidance for AAP environments."""
    q = (query or '').lower()
    aap_ver = '2.6' if re.search(r'\b2\.6\b', q) else '2.x'

    lines = []
    lines.append(f'AAP {aap_ver} Patch Strategy (Practical Runbook)')
    lines.append('=' * 72)
    lines.append('1. Pre-Change Planning')
    lines.append('- Review AAP release notes/advisories and confirm supported upgrade path from your current build.')
    lines.append('- Identify deployment model (operator/containerized vs installer-based) and maintenance window owners.')
    lines.append('- Backup controller/hub/EDA databases and persistent storage; snapshot VMs/nodes when applicable.')
    lines.append('')
    lines.append('2. Baseline Health Checks')
    lines.append('- Validate cluster health, job queue state, and available capacity before patching.')
    lines.append('- Export key assets: inventories, credentials metadata, job templates, schedules, and workflow definitions.')
    lines.append('- Freeze high-risk changes during patch window (new EEs, RBAC changes, large project sync changes).')
    lines.append('')
    lines.append('3. Stage First, Then Production')
    lines.append('- Apply patch to non-production AAP first.')
    lines.append('- Run smoke tests: project sync, credential use, job template launch, workflow execution, notifications, SSO/LDAP auth.')
    lines.append('- Promote to production only after non-prod is stable and critical automations pass.')
    lines.append('')
    lines.append('4. Patch Execution')
    lines.append('- Pause heavy schedules and long-running workflows during maintenance.')
    lines.append('- Apply AAP patch using the official method for your deployment type.')
    lines.append('- Reconcile services/pods and verify controller, hub, and gateway/API health endpoints.')
    lines.append('')
    lines.append('5. Post-Patch Validation')
    lines.append('- Re-run prioritized business workflows and compare runtime/queue performance against baseline.')
    lines.append('- Validate execution environment compatibility and collection dependencies for top playbooks.')
    lines.append('- Monitor logs and failed jobs for 24h hypercare.')
    lines.append('')
    lines.append('6. Rollback Triggers')
    lines.append('- Roll back if auth breaks, critical workflows fail repeatedly, or control-plane services remain unhealthy.')
    lines.append('- Use documented restore path for DB + persistent volumes + snapshots and communicate rollback decision quickly.')
    lines.append('')
    lines.append('Suggested first actions today:')
    lines.append('1. Build a non-prod smoke-test list for your top automation workflows.')
    lines.append('2. Verify backup + restore timings for AAP databases and persistent data.')
    lines.append('3. Schedule patch in non-prod and capture evidence before production rollout.')
    return '\n'.join(lines)


def generate_idm_setup_response(query: str) -> str:
    """Generate deterministic setup guidance for Red Hat IdM on RHEL."""
    q = (query or '').lower()
    rhel_ver = '10' if re.search(r'\brhel\s*10\b|\b10\b', q) else '9/10'

    lines = []
    lines.append(f'Red Hat IdM Setup on RHEL {rhel_ver} (Practical Runbook)')
    lines.append('=' * 72)
    lines.append('1. Plan Topology and Naming')
    lines.append('- Define IdM realm and DNS domain (example: EXAMPLE.COM / example.com).')
    lines.append('- Decide first server + replica layout and admin access model.')
    lines.append('- Reserve static IP/FQDN for IdM servers and ensure forward/reverse DNS consistency.')
    lines.append('')
    lines.append('2. Host Prerequisites')
    lines.append('- Ensure RHEL system is registered and can reach required repositories.')
    lines.append('- Set hostname/FQDN, NTP/chrony, and validate DNS resolution end-to-end.')
    lines.append('- Open required ports in security controls and keep SELinux policy aligned with IdM services.')
    lines.append('')
    lines.append('3. Install and Initialize IdM Server')
    lines.append('- Install server packages for IdM on the primary node.')
    lines.append('- Run IdM server install with integrated DNS if IdM will own DNS; otherwise configure external DNS integration.')
    lines.append('- Validate Kerberos (kinit), LDAP, and web UI after installation.')
    lines.append('')
    lines.append('4. Client Enrollment and Access Policies')
    lines.append('- Install IdM client packages on target RHEL hosts and enroll to the realm.')
    lines.append('- Configure HBAC, sudo rules, and host groups before broad rollout.')
    lines.append('- Use role-based groups for least-privilege access from day one.')
    lines.append('')
    lines.append('5. Hardening and Operations')
    lines.append('- Add at least one replica for HA and backup resilience.')
    lines.append('- Configure automated backups and test restore procedure on non-production.')
    lines.append('- Enable monitoring for cert expiry, replication health, and auth failures.')
    lines.append('')
    lines.append('6. Validation Checklist')
    lines.append('- Validate login via Kerberos/SSSD from enrolled clients.')
    lines.append('- Confirm sudo/HBAC policy behavior for allowed and denied scenarios.')
    lines.append('- Verify DNS and certificate services if integrated.')
    lines.append('')
    lines.append('Suggested first actions today:')
    lines.append('1. Confirm FQDN + DNS + NTP prerequisites on your first IdM server.')
    lines.append('2. Install primary IdM server and run post-install kinit/web checks.')
    lines.append('3. Enroll one pilot RHEL client and validate HBAC/sudo policy flow.')
    return '\n'.join(lines)


def generate_ansible_eda_use_cases_response() -> str:
    """Generate a concise, practical list of Ansible EDA use cases."""
    lines = []
    lines.append('Common Ansible EDA Use Cases')
    lines.append('=' * 72)
    lines.append('1. Incident response and alert-driven remediation')
    lines.append('- Trigger a playbook when monitoring detects a failed service, high CPU, disk pressure, or cert expiry.')
    lines.append('- Open a ticket, gather diagnostics, restart a service, or fail over to a standby node.')
    lines.append('')
    lines.append('2. Security response automation')
    lines.append('- Respond to SIEM, IDS, or firewall events by isolating hosts, blocking IPs, rotating credentials, or collecting evidence.')
    lines.append('- Tie EDA into SSH brute-force, auditd, or vulnerability alerts for consistent first-response actions.')
    lines.append('')
    lines.append('3. Infrastructure drift and policy enforcement')
    lines.append('- React when a config file changes, a service stops, or a host falls out of policy.')
    lines.append('- Re-apply the approved state or trigger an approval workflow before remediation.')
    lines.append('')
    lines.append('4. ServiceNow / ITSM / webhook-driven operations')
    lines.append('- Launch automation from ticket creation, change approval, CMDB updates, or webhook events from other systems.')
    lines.append('- Use EDA to turn business events into controlled operational actions.')
    lines.append('')
    lines.append('5. AAP / controller workflow orchestration')
    lines.append('- Use an event to launch job templates, workflows, notifications, or approvals in Ansible Automation Platform.')
    lines.append('- Good for event-to-workflow routing instead of polling or manual execution.')
    lines.append('')
    lines.append('6. Network event response')
    lines.append('- React to interface flaps, device down alerts, config drift, or threshold violations from network monitoring tools.')
    lines.append('- Run validation, capture state, or push a known-safe network change automatically.')
    lines.append('')
    lines.append('7. Cloud and platform automation')
    lines.append('- Trigger scale actions, tagging, remediation, or housekeeping when cloud events occur in AWS, Azure, or OpenShift.')
    lines.append('- Useful for event-based lifecycle tasks instead of cron-driven jobs.')
    lines.append('')
    lines.append('8. File, message bus, and API event processing')
    lines.append('- Watch Kafka, AMQP, webhooks, file drops, or REST events and route them into automation rules.')
    lines.append('- Common for integration-heavy operational platforms.')
    lines.append('')
    lines.append('9. Compliance and audit workflows')
    lines.append('- Trigger evidence collection, config validation, or corrective actions when a compliance event or scan result arrives.')
    lines.append('- Helps reduce delay between finding and response.')
    lines.append('')
    lines.append('10. Human-in-the-loop automation')
    lines.append('- Use EDA to detect an event, enrich context, and then request approval before executing a high-risk action.')
    lines.append('- Good fit for production changes, security containment, and privileged operations.')
    lines.append('')
    lines.append('Good first demos:')
    lines.append('1. Restart Apache when monitoring reports it down.')
    lines.append('2. Open a ServiceNow ticket and gather logs when disk usage exceeds threshold.')
    lines.append('3. Launch an AAP workflow when a webhook reports a failed deployment.')
    return '\n'.join(lines)


def generate_aap_migration_strategy_response(query: str) -> str:
    """Generate a practical migration strategy for AAP/Ansible 2.5 -> 2.6 transitions."""
    q = (query or '').lower()
    mentions_container = bool(re.search(r'\b(container|podman|k8s|kubernetes|operator)\b', q))
    mentions_rpm = bool(re.search(r'\b(rpm|installer|bundle)\b', q))

    lines = []
    lines.append('AAP/Ansible 2.5 -> 2.6 Migration Strategy')
    lines.append('=' * 72)
    lines.append('1. Assessment and Prerequisites')
    lines.append('- Inventory all 2.5 components: controller, hub, EDA, execution nodes, integrations, and custom EE images.')
    lines.append('- Export inventories, credentials metadata, job templates, schedules, notifications, projects, and RBAC mappings.')
    lines.append('- Freeze high-risk changes and define migration maintenance windows.')
    lines.append('')
    lines.append('2. Backup and Rollback Design')
    lines.append('- Backup PostgreSQL/database and all persistent storage for controller/hub.')
    lines.append('- Snapshot host VMs or volumes before cutover.')
    lines.append('- Define explicit rollback triggers: failed smoke tests, auth failures, or job execution regressions.')
    lines.append('')
    lines.append('3. Target Architecture for 2.6')
    if mentions_rpm and mentions_container:
        lines.append('- Plan a side-by-side transition from RPM-based 2.5 to containerized 2.6 components.')
    elif mentions_container:
        lines.append('- Build 2.6 target using containerized deployment patterns and persistent volumes.')
    else:
        lines.append('- Prefer side-by-side deployment for 2.6 to minimize blast radius during cutover.')
    lines.append('- Validate SSO/LDAP, certs, proxies, and registry access for execution environments.')
    lines.append('')
    lines.append('4. Execution Environment (EE) Migration')
    lines.append('- Rebuild and sign EE images against 2.6-supported ansible-core and collections.')
    lines.append('- Run ansible-lint/sanity and smoke-playbook tests against each EE before production use.')
    lines.append('')
    lines.append('5. Data and Configuration Migration')
    lines.append('- Import/export artifacts in waves: orgs/users -> credentials -> inventories -> templates/workflows.')
    lines.append('- Rebind credentials and vault integrations; validate webhook and SCM tokens.')
    lines.append('')
    lines.append('6. Validation Gates')
    lines.append('- Functional: launch critical job templates and workflows in staging then prod.')
    lines.append('- Operational: verify schedules, callback endpoints, notifications, and audit logs.')
    lines.append('- Performance: compare queue times, job runtime, and EE startup overhead versus 2.5 baseline.')
    lines.append('')
    lines.append('7. Cutover and Hypercare')
    lines.append('- Execute blue/green cutover with a timed rollback window.')
    lines.append('- Run 24-72h hypercare with hourly checks on failed jobs, capacity, and auth events.')
    lines.append('')
    lines.append('Suggested first actions today:')
    lines.append('1. Build migration inventory and classify critical automation workflows.')
    lines.append('2. Create backup/restore drill and prove rollback timing.')
    lines.append('3. Rebuild top 3 EEs and run staging smoke tests.')
    return '\n'.join(lines)


def generate_satellite_aap_connection_strategy_response(query: str) -> str:
    """Generate a practical strategy for connecting Satellite 6.18 with AAP 2.6."""
    q = (query or '').lower()
    sat_ver = '6.18' if re.search(r'\b6\.18\b', q) else '6.x'
    aap_ver = '2.6' if re.search(r'\b2\.6\b', q) else '2.x'

    lines = []
    lines.append(f'Satellite {sat_ver} to AAP {aap_ver} Connection Strategy')
    lines.append('=' * 72)
    lines.append('1. Integration Design')
    lines.append('- Define source-of-truth boundaries: Satellite for content/lifecycle, AAP for orchestration/automation.')
    lines.append('- Choose auth model (service account + token) and scope least privilege for API calls.')
    lines.append('')
    lines.append('2. Prerequisites and Security')
    lines.append('- Validate DNS/FQDN reachability, TLS trust chains, and clock sync between Satellite and AAP nodes.')
    lines.append('- Import Satellite CA into AAP execution environments if private PKI is used.')
    lines.append('- Store credentials in AAP Credential types (not plain variables).')
    lines.append('')
    lines.append('3. Build Connectivity in AAP')
    lines.append('- Create a Satellite inventory source (or sync job) using the foreman/katello APIs.')
    lines.append('- Create job templates for content view publish/promote and host patch orchestration.')
    lines.append('- Parameterize lifecycle environment, content view, host collection, and maintenance window.')
    lines.append('')
    lines.append('4. Workflow Pattern')
    lines.append('- Step A: Satellite pre-check (sync status, content view version, host applicability).')
    lines.append('- Step B: AAP execute rolling patch/update by host group with concurrency controls.')
    lines.append('- Step C: Satellite post-check (errata compliance, drift, and remediation summary).')
    lines.append('')
    lines.append('5. Validation and Rollback')
    lines.append('- Validate on non-prod host collections first, then progressively promote to prod rings.')
    lines.append('- Keep rollback playbooks ready: service restart, package revert where applicable, and snapshot restore path.')
    lines.append('')
    lines.append('6. Day-2 Operations')
    lines.append('- Schedule biweekly automation windows and capture job evidence for compliance reporting.')
    lines.append('- Add alerting on failed syncs, expired credentials, and API/TLS errors.')
    lines.append('')
    lines.append('Suggested first actions today:')
    lines.append('1. Create AAP credential + inventory sync to Satellite API.')
    lines.append('2. Run a non-prod content view promote + patch workflow.')
    lines.append('3. Add post-run compliance checks and executive summary report output.')
    return '\n'.join(lines)


def generate_satellite_pxe_strategy_response(query: str) -> str:
    """Generate a practical strategy for Satellite PXE services (DHCP/DNS/TFTP)."""
    q = (query or '').lower()
    sat_ver = '6.18' if re.search(r'\b6\.18\b', q) else '6.x'

    lines = []
    lines.append(f'Satellite {sat_ver} PXE Provisioning Strategy (DHCP/DNS/TFTP)')
    lines.append('=' * 72)
    lines.append('1. Network and Service Boundaries')
    lines.append('- Define provisioning VLAN/subnet scopes and PXE relay behavior (ip helper-address).')
    lines.append('- Choose authoritative services: Satellite-managed DHCP/DNS/TFTP or existing enterprise services.')
    lines.append('- Reserve static ranges for infra and dynamic pools for provisioning clients.')
    lines.append('')
    lines.append('2. Core Prerequisites')
    lines.append('- Validate forward/reverse DNS and FQDN for Satellite/Capsule endpoints.')
    lines.append('- Ensure time sync (NTP/chrony), certificate trust, and firewall rules for PXE traffic.')
    lines.append('- Confirm TFTP and HTTP(S) reachability from target subnets.')
    lines.append('')
    lines.append('3. Satellite/Capsule Setup Order')
    lines.append('- Configure subnet objects with DHCP, TFTP, and template associations.')
    lines.append('- Publish synced OS content and activation keys before first PXE test.')
    lines.append('- Enable Smart Proxy/Capsule features required for DHCP/DNS/TFTP in the provisioning zone.')
    lines.append('')
    lines.append('4. PXE Boot Workflow')
    lines.append('- Build hostgroup with kickstart/provisioning templates and partitioning profile.')
    lines.append('- Validate DHCP options, PXE bootloader paths, and TFTP root consistency.')
    lines.append('- Test one host end-to-end: PXE -> registration -> content attach -> post-install config.')
    lines.append('')
    lines.append('5. Security and Reliability')
    lines.append('- Restrict PXE services to provisioning VLANs and approved MAC/vendor classes.')
    lines.append('- Keep templates version-controlled and require approval for production changes.')
    lines.append('- Monitor DHCP lease exhaustion, TFTP errors, and failed provisioning jobs.')
    lines.append('')
    lines.append('6. Scale and Operations')
    lines.append('- Introduce ring-based rollout: lab -> staging -> production subnets.')
    lines.append('- Add periodic validation job for DNS/DHCP/TFTP health and template drift.')
    lines.append('- Capture provisioning KPIs: success rate, mean build time, and failure causes.')
    lines.append('')
    lines.append('Suggested first actions today:')
    lines.append('1. Define subnet + helper-address + DHCP options for one pilot VLAN.')
    lines.append('2. Configure one hostgroup and run a single PXE provisioning test.')
    lines.append('3. Add monitoring/alerts for DHCP leases, TFTP failures, and build errors.')
    return '\n'.join(lines)


def generate_aap_mcp_setup_response() -> str:
    """Generate a practical setup guide for MCP server integration in AAP."""
    lines = []
    lines.append('MCP Server Setup in AAP (Practical Runbook)')
    lines.append('=' * 72)
    lines.append('1. Prepare the MCP host')
    lines.append('- Ensure the MCP service host has Python runtime, project repo access, and outbound network reachability.')
    lines.append('- Validate MCP server entrypoint works locally (example: python3 server.py).')
    lines.append('- Confirm auth material (tokens/certs) is stored securely, not in plaintext files.')
    lines.append('')
    lines.append('2. Define AAP credentials and inventories')
    lines.append('- Create AAP credentials for SSH/API access needed by MCP workflows.')
    lines.append('- Create inventory/group for MCP target hosts and assign variables by environment (dev/stage/prod).')
    lines.append('- Keep secrets in AAP Credential objects or Ansible Vault, never in job template extra-vars.')
    lines.append('')
    lines.append('3. Create project and execution environment')
    lines.append('- Add this repository as an AAP Project source (SCM sync enabled).')
    lines.append('- Build/select an Execution Environment image that includes required Python deps and ansible-core.')
    lines.append('- Validate EE by running a lightweight test job against a non-prod host.')
    lines.append('')
    lines.append('4. Build MCP job templates')
    lines.append('- Template A: MCP health check (service running, API reachable, required ports open).')
    lines.append('- Template B: MCP start/restart workflow using controlled service actions.')
    lines.append('- Template C: MCP diagnostics capture (logs, status, connectivity checks) for troubleshooting.')
    lines.append('')
    lines.append('5. Wire workflow and schedules')
    lines.append('- Create a Workflow Job Template: pre-check -> deploy/update -> post-check -> notify.')
    lines.append('- Add approval node for production cutovers.')
    lines.append('- Schedule periodic health checks and on-demand remediation runs.')
    lines.append('')
    lines.append('6. Validate and operationalize')
    lines.append('- Run end-to-end in dev first, then stage, then production ring rollout.')
    lines.append('- Add notifications (email/Slack/webhook) for failed jobs and degraded MCP checks.')
    lines.append('- Track runbook KPIs: success rate, time-to-recover, and failed step trends.')
    lines.append('')
    lines.append('Suggested first actions today:')
    lines.append('1. Create AAP project + inventory + credentials for MCP host.')
    lines.append('2. Run a health-check template against MCP host.')
    lines.append('3. Build workflow template with approval gate for production.')
    return '\n'.join(lines)


def generate_satellite_mcp_setup_response() -> str:
    """Generate a practical setup guide for MCP server use with Satellite/Capsule."""
    lines = []
    lines.append('MCP Server Setup for Satellite (Practical Runbook)')
    lines.append('=' * 72)
    lines.append('1. Prepare MCP host and Satellite API access')
    lines.append('- Deploy MCP service on a managed host with Python runtime and repo access.')
    lines.append('- Validate MCP entrypoint locally (example: python3 server.py).')
    lines.append('- Create Satellite service account/API token with least privileges for host, content, and job status reads.')
    lines.append('')
    lines.append('2. Network, DNS, TLS prerequisites')
    lines.append('- Ensure MCP host resolves Satellite/Capsule FQDNs and trusts their certificates.')
    lines.append('- Open required egress paths from MCP host to Satellite API endpoints.')
    lines.append('- Verify time sync to avoid token/cert validation issues.')
    lines.append('')
    lines.append('3. Define integration scope')
    lines.append('- Decide which workflows MCP will orchestrate: health checks, patch planning, content view promotion, or provisioning checks.')
    lines.append('- Map environments and org/location boundaries before automation.')
    lines.append('- Keep secret material in Vault/credential store, not static files.')
    lines.append('')
    lines.append('4. Implement MCP workflows')
    lines.append('- Workflow A: Satellite connectivity + auth test.')
    lines.append('- Workflow B: Inventory/content visibility checks per org/environment.')
    lines.append('- Workflow C: Controlled remediation hooks with approval checkpoints.')
    lines.append('')
    lines.append('5. Operate safely')
    lines.append('- Start in read-only mode for baseline validation.')
    lines.append('- Add approval gates before any write/update actions in production.')
    lines.append('- Capture run logs and API responses for auditability.')
    lines.append('')
    lines.append('6. Validate and scale')
    lines.append('- Run dev -> stage -> prod ring rollout for MCP-enabled jobs.')
    lines.append('- Add alerting for auth failures, API latency, and workflow errors.')
    lines.append('- Track success rate, time-to-remediate, and recurring failure patterns.')
    lines.append('')
    lines.append('Suggested first actions today:')
    lines.append('1. Create Satellite API credential for MCP with least privilege.')
    lines.append('2. Run an MCP connectivity and certificate trust check.')
    lines.append('3. Enable one read-only Satellite workflow and validate outputs.')
    return '\n'.join(lines)


def generate_satellite_end_to_end_setup_response(query: str) -> str:
    """Generate end-to-end Satellite setup strategy from manifest to provisioning with Ansible."""
    q = (query or '').lower()
    sat_ver = '6.18' if re.search(r'\b6\.18\b', q) else '6.x'

    lines = []
    lines.append(f'Satellite {sat_ver} End-to-End Setup Strategy with Ansible')
    lines.append('=' * 72)
    lines.append('1. Foundation and Access')
    lines.append('- Prepare Satellite/Capsule host sizing, storage, DNS, and TLS trust.')
    lines.append('- Establish admin + automation service accounts and least-privilege API access.')
    lines.append('- Define org/location model and naming conventions before data import.')
    lines.append('')
    lines.append('2. Subscription and Content Baseline')
    lines.append('- Import Red Hat subscription manifest and verify entitlement visibility.')
    lines.append('- Enable/sync required repositories for RHEL 9 and RHEL 10.')
    lines.append('- Create lifecycle environments (dev -> stage -> prod).')
    lines.append('- Publish/promote content views with explicit versioning policy.')
    lines.append('')
    lines.append('3. Activation Keys and Host Registration')
    lines.append('- Create activation keys per environment and OS major version (RHEL 9/10).')
    lines.append('- Map host collections/content hosts to lifecycle + content view strategy.')
    lines.append('- Validate registration command templates and bootstrap flow.')
    lines.append('')
    lines.append('4. Provisioning Stack (DHCP/DNS/TFTP + Compute Resources)')
    lines.append('- Define subnets, domains, DHCP/TFTP/DNS ownership, and PXE relay paths.')
    lines.append('- Configure compute resources and image/template associations.')
    lines.append('- Build hostgroups with provisioning templates and partition profiles.')
    lines.append('')
    lines.append('5. PXE and bootc Paths')
    lines.append('- PXE path: validate bootloader paths, DHCP options, and TFTP root consistency.')
    lines.append('- bootc path: define image pipeline, registry trust, and host rollout policy.')
    lines.append('- Test one golden build for each path end-to-end before scale-out.')
    lines.append('')
    lines.append('6. Ansible Automation Orchestration')
    lines.append('- Use Ansible/AAP playbooks to automate: repo sync checks, CV promote, activation-key verification, and provisioning pre-checks.')
    lines.append('- Add workflow gates for production promotions and provisioning changes.')
    lines.append('- Capture job evidence for compliance and change management.')
    lines.append('')
    lines.append('7. Validation, Rollback, and Day-2 Ops')
    lines.append('- Validate registration, package compliance, errata status, and build success rates.')
    lines.append('- Keep rollback plan for content view promotion and provisioning template changes.')
    lines.append('- Monitor KPI set: sync success, build time, failure causes, and remediation cycle time.')
    lines.append('')
    lines.append('Suggested first actions today:')
    lines.append('1. Import manifest and sync minimum RHEL 9/10 repositories.')
    lines.append('2. Create lifecycle + content views + activation keys for dev.')
    lines.append('3. Run one PXE and one bootc pilot provisioning flow, then codify in Ansible workflow.')
    return '\n'.join(lines)


def _filter_rag_context_for_account(rag_context: str | None, account_query: str) -> str | None:
    """Keep only RAG bullet lines that match account tokens; fallback to original if nothing matches."""
    if not rag_context or not account_query:
        return rag_context

    account_tokens = [t for t in re.findall(r'\b[a-z0-9]+\b', account_query.lower()) if len(t) > 2]
    if not account_tokens:
        return rag_context

    lines = rag_context.splitlines()
    header = '[Relevant context from your personal knowledge base:]'
    filtered = []
    for line in lines:
        s = line.strip().lower()
        if s.startswith('•'):
            if any(tok in s for tok in account_tokens):
                filtered.append(line)

    if not filtered:
        return None
    return header + '\n' + '\n'.join(filtered)


def _find_best_business_intel_record(account_query: str) -> dict | None:
    """Find the best matching business intel account record for an account query."""
    if not account_query or not os.path.exists(TRAIN_DIR):
        return None

    account_l = account_query.strip().lower()
    best = None
    best_score = 0

    try:
        train_path = Path(TRAIN_DIR)
        for json_file in sorted(train_path.glob('intel-*.json'), reverse=True):
            try:
                with open(json_file, 'r', encoding='utf-8') as fh:
                    record = json.load(fh)
            except Exception:
                continue

            if record.get('type') != 'business_intel_account':
                continue

            record_name = (record.get('account_name') or '').lower()
            if record_name == account_l:
                score = 100
            elif record_name.startswith(account_l) or account_l.startswith(record_name):
                score = 80
            elif account_l in record_name:
                score = 60
            else:
                tokens = [t for t in account_l.split() if len(t) > 2]
                hits = sum(1 for t in tokens if t in record_name)
                score = hits * 10

            if score > best_score:
                best_score = score
                best = record
    except Exception:
        return None

    if not best or best_score < 10:
        return None
    return best


def _looks_like_script_objective(text: str) -> bool:
    """Detect non-company objective strings produced by tooling pipelines."""
    t = (text or '').strip().lower()
    if not t:
        return False
    patterns = [
        r'\bcollect\b.*\btraining\b',
        r'\btraining-ready\b',
        r'\bproduce\b.*\btraining\b',
        r'\baccount intelligence\b.*\bae reports\b',
        r'\bnormalize\b.*\brecords\b',
    ]
    return any(re.search(p, t) for p in patterns)


def _select_company_primary_objective(record: dict, fallback_summary: str = '') -> str:
    """Return the best company objective, avoiding script/tooling objective text."""
    candidate_fields = [
        'primary_objective',
        'use_case',
    ]
    for field in candidate_fields:
        raw = record.get(field)
        clean = (_flatten_value(raw) or '').strip().strip('"')
        if len(clean) < 12:
            continue
        # Reject procedural/script-like content as objective text.
        if clean.count(',') > 3 or len(clean) > 260:
            continue
        if _looks_like_script_objective(clean):
            continue
        return clean

    # No safe company objective found.
    return ''


def generate_intel_report(account_query: str) -> str | None:
    """Generate a professional account intel report from training data.

    Searches business_intel_account records and formats a seller-ready brief
    with professional formatting and cleaned data extraction.
    Returns a formatted Markdown report string or None if no records found.
    """
    if not account_query or not os.path.exists(TRAIN_DIR):
        return None

    best = _find_best_business_intel_record(account_query)
    if not best:
        return None

    # Build professional Markdown report
    account = best.get('account_name', 'Unknown Account').strip('"')
    lines = []
    
    # Professional header
    lines.append('═' * 80)
    lines.append(f'Account Intelligence Brief')
    lines.append('═' * 80)
    lines.append('')
    lines.append(f'# {account}')
    lines.append('')

    # Key metrics header
    date = best.get('date', '')
    ticker = best.get('ticker', '')
    score_val = best.get('weighted_score', '')
    guidance = best.get('sales_guidance_level', '')
    activity_score = best.get('activity_score', '')
    
    metrics = []
    if date:
        metrics.append(f'**Report Date:** {date}')
    if ticker and ticker != 'Privately Held':
        metrics.append(f'**Ticker:** {ticker}')
    if score_val:
        metrics.append(f'**Engagement Score:** {score_val}')
    if activity_score:
        metrics.append(f'**Activity Level:** {activity_score}')
    if guidance:
        metrics.append(f'**Sales Guidance:** {guidance}')
    
    if metrics:
        lines.append(' | '.join(metrics))
        lines.append('')

    # Account Ownership Section
    ae = best.get('account_executive', '')
    sa = best.get('account_sa', '')
    pod = best.get('pod_name', '')
    territory_data = best.get('territory_name', '') or best.get('territory_owner', '')
    territory = _flatten_value(territory_data) if territory_data else ''
    cross_territory = best.get('cross_territory_contacts', [])
    
    if any([ae, sa, pod, territory]):
        lines.append('─' * 80)
        lines.append('## ACCOUNT OWNERSHIP & TEAM')
        lines.append('─' * 80)
        lines.append('')
        
        ownership_lines = []
        if pod:
            clean_pod = _flatten_value(pod)
            ownership_lines.append(f'**POD:** {clean_pod}')
        if territory:
            ownership_lines.append(f'**Territory:** {territory}')
        if ae:
            clean_ae = (_flatten_value(ae) or '').strip('"')
            ownership_lines.append(f'**Account Executive:** {clean_ae}')
        if sa:
            clean_sa = (_flatten_value(sa) or '').strip('"')
            ownership_lines.append(f'**Solution Architect:** {clean_sa}')
        
        lines.extend(ownership_lines)
        
        # Cross-territory contacts
        if cross_territory:
            cross_flat = _flatten_value(cross_territory)
            if cross_flat:
                lines.append(f'**Extended Team:** {cross_flat}')
        
        lines.append('')

    # Executive Summary
    summary = best.get('short_summary', '')
    clean_summary = ''
    if summary:
        lines.append('─' * 80)
        lines.append('## EXECUTIVE SUMMARY')
        lines.append('─' * 80)
        lines.append('')
        clean_summary = (_flatten_value(summary) or '').strip('"')
        lines.append(clean_summary)
        lines.append('')

    # Technology Signals & Stack
    tags = best.get('tags', [])
    stack_signals = best.get('stack_signals', [])
    tag_signals = [(_flatten_value(s) or '').strip() for s in (tags or [])]
    tag_signals = [s for s in tag_signals if s]
    all_signals = list(dict.fromkeys(tag_signals + [(_flatten_value(s) or '').strip() for s in (stack_signals or [])]))
    all_signals = [s for s in all_signals if s]
    
    if all_signals:
        lines.append('─' * 80)
        lines.append('## TECHNOLOGY LANDSCAPE')
        lines.append('─' * 80)
        lines.append('')
        # Format in columns for readability
        signal_cols = [all_signals[i:i+3] for i in range(0, len(all_signals), 3)]
        for col_set in signal_cols[:5]:
            lines.append(' • '.join([f'**{s}**' for s in col_set[:3]]))
        lines.append('')

        lines.append('─' * 80)
        lines.append('## DETECTED INTEGRATION SIGNALS')
        lines.append('─' * 80)
        lines.append('')
        detected_signals = tag_signals if tag_signals else all_signals
        for idx, sig in enumerate(detected_signals, 1):
            lines.append(f'{idx}. {sig}')
        lines.append('')

    # Red Hat Strategic Focus
    rh_focus = best.get('redhat_focus_areas', [])
    if rh_focus:
        lines.append('─' * 80)
        lines.append('## RED HAT STRATEGIC FOCUS')
        lines.append('─' * 80)
        lines.append('')
        focus_values = []
        if isinstance(rh_focus, str):
            focus_values = [x for x in (_flatten_value(rh_focus) or '').split('; ') if x]
        elif isinstance(rh_focus, list):
            focus_values = rh_focus
        for i, focus_area in enumerate(focus_values[:5], 1):
            clean_focus = (_flatten_value(focus_area) or '').strip('"')
            lines.append(f'{i}. {clean_focus}')
        lines.append('')

    # Primary Contacts
    contacts = best.get('contacts', [])
    if contacts:
        lines.append('─' * 80)
        lines.append('## PRIMARY CONTACTS')
        lines.append('─' * 80)
        lines.append('')
        contact_list = _safely_parse_json_or_list(contacts)
        if isinstance(contact_list, list):
            for contact in contact_list[:15]:
                clean_contact = (_flatten_value(contact) or '').strip('"')
                if clean_contact and '@' in clean_contact:
                    # Split if comma-separated
                    for email in [e.strip() for e in clean_contact.split(',') if '@' in e]:
                        lines.append(f'• {email}')
        elif isinstance(contact_list, str) and ',' in contact_list:
            for email in [e.strip() for e in contact_list.split(',')[:15] if '@' in e]:
                lines.append(f'• {email}')
        lines.append('')

    # Notable Headlines & Recent Activity
    headlines = best.get('notable_news_headlines', [])
    if headlines:
        lines.append('─' * 80)
        lines.append('## RECENT ACTIVITY & HEADLINES')
        lines.append('─' * 80)
        lines.append('')
        hl_list = _safely_parse_json_or_list(headlines)
        if isinstance(hl_list, list):
            for headline in hl_list[:10]:
                clean_hl = (_flatten_value(headline) or '').strip('"')
                if clean_hl:
                    # Truncate long headlines for readability
                    display_text = clean_hl
                    if len(clean_hl) > 100:
                        display_text = clean_hl[:97] + '...'
                    # Create search link for headline
                    search_url = f"https://www.google.com/search?q={clean_hl.replace(' ', '+')}"
                    lines.append(f'• [{display_text}]({search_url})')
        elif isinstance(hl_list, str) and ',' in hl_list:
            for hl in [h.strip() for h in hl_list.split(',')[:10]]:
                if hl:
                    display_text = hl
                    if len(hl) > 100:
                        display_text = hl[:97] + '...'
                    search_url = f"https://www.google.com/search?q={hl.replace(' ', '+')}"
                    lines.append(f'• [{display_text}]({search_url})')
        lines.append('')

    # Use Case & Objectives
    objective = _select_company_primary_objective(best, fallback_summary=clean_summary)
    lines.append('─' * 80)
    lines.append('## PRIMARY OBJECTIVE')
    lines.append('─' * 80)
    lines.append('')
    if objective:
        lines.append(objective)
    else:
        lines.append('Not explicitly provided in the imported company record.')
    lines.append('')

    # Use Case Questions
    use_case_qs = best.get('use_case_questions', [])
    if use_case_qs:
        lines.append('─' * 80)
        lines.append('## KEY DISCOVERY QUESTIONS')
        lines.append('─' * 80)
        lines.append('')
        qs_list = _safely_parse_json_or_list(use_case_qs)
        if isinstance(qs_list, list):
            for q_idx, q in enumerate(qs_list[:6], 1):
                clean_q = (_flatten_value(q) or '').strip('"')
                lines.append(f'{q_idx}. {clean_q}')
        lines.append('')

    # Footer
    lines.append('═' * 80)
    lines.append(f'Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}')
    lines.append('═' * 80)

    return '\n'.join(lines)


def generate_consulting_strategy(account_query: str, user_query: str) -> str | None:
    """Generate a consulting strategy, preferring LLM+RAG but with solid offline fallback."""
    account = (account_query or '').strip()
    if not account:
        return None

    rag_context = search_training_data_for_rag(user_query or f'consulting strategy for {account}', limit=8, max_chars=5000)
    rag_context = _filter_rag_context_for_account(rag_context, account)
    strategy_prompt = (
        f"Create a consulting strategy for {account}. "
        "Return sections in this exact order: Executive Summary, Strategic Priorities, "
        "90-Day Action Plan, Risks and Mitigations, Stakeholder Engagement, Next 3 Meetings. "
        "Keep recommendations practical and outcome-driven."
    )

    llm_resp = call_bridge(strategy_prompt, rag_context=rag_context)
    llm_text = extract_assistant_content(llm_resp)
    if (
        isinstance(llm_resp, str)
        and not llm_resp.startswith('ERR:')
        and llm_text
        and llm_text.strip()
        and not _is_low_quality_assistant_text(llm_text)
    ):
        return llm_text.strip()

    # Offline deterministic strategy fallback.
    rec = _find_best_business_intel_record(account)
    if not rec and not rag_context:
        return None

    account_name = (rec.get('account_name') if rec else account) or account
    priorities = rec.get('redhat_focus_areas', []) if rec else []
    objective = rec.get('primary_objective', '') if rec else ''
    contacts = rec.get('contacts', []) if rec else []
    questions = rec.get('use_case_questions', []) if rec else []

    lines = []
    lines.append('Consulting Strategy (offline — from knowledge base)')
    lines.append('=' * 72)
    lines.append(f'Account: {account_name}')
    lines.append('')
    lines.append('Executive Summary:')
    if objective:
        lines.append(f'- Primary objective: {_flatten_value(objective)}')
    else:
        lines.append('- Drive measurable business outcomes through platform modernization and automation.')

    lines.append('')
    lines.append('Strategic Priorities:')
    if priorities:
        for p in priorities[:5]:
            lines.append(f'- {_flatten_value(p)}')
    else:
        lines.append('- Validate top 3 modernization priorities with executive sponsors.')
        lines.append('- Align platform roadmap to compliance and operational efficiency goals.')

    lines.append('')
    lines.append('90-Day Action Plan:')
    lines.append('- Days 0-30: Discovery workshop, baseline architecture/risk assessment, success metrics.')
    lines.append('- Days 31-60: Pilot one high-value use case and define operating model.')
    lines.append('- Days 61-90: Scale pilot, formalize adoption plan, and executive readout with ROI signals.')

    lines.append('')
    lines.append('Risks and Mitigations:')
    lines.append('- Risk: unclear ownership; Mitigation: assign executive sponsor and working team RACI.')
    lines.append('- Risk: slow adoption; Mitigation: phased rollout with enablement and measurable milestones.')

    lines.append('')
    lines.append('Stakeholder Engagement:')
    if contacts:
        flat_contacts = _flatten_value(contacts)
        if flat_contacts:
            lines.append(f'- Existing contacts: {flat_contacts}')
    lines.append('- Add CIO/VP platform, security lead, and operations lead into a biweekly steering cadence.')

    lines.append('')
    lines.append('Next 3 Meetings:')
    lines.append('1. Executive alignment and outcome definition.')
    lines.append('2. Technical deep dive and use-case prioritization.')
    lines.append('3. Pilot decision review and 90-day execution sign-off.')

    if questions:
        lines.append('')
        lines.append('Open Questions to Validate:')
        for q in questions[:5]:
            lines.append(f'- {_flatten_value(q)}')

    if rag_context:
        evidence = rag_context.replace('[Relevant context from your personal knowledge base:]', '').strip()
        if evidence:
            lines.append('')
            lines.append('Evidence Used:')
            lines.append(evidence)

    return '\n'.join(lines)


def generate_account_stakeholder_response(account_query: str) -> str | None:
    """Generate a concise stakeholder/contact response for an account from local intel."""
    rec = _find_best_business_intel_record(account_query)
    if not rec:
        return None

    account_name = rec.get('account_name', account_query)
    contacts_raw = rec.get('contacts', [])
    contacts = _safely_parse_json_or_list(contacts_raw)

    extracted = []
    if isinstance(contacts, list):
        for c in contacts:
            s = _flatten_value(c)
            if s:
                extracted.append(str(s).strip())
    elif isinstance(contacts, str):
        extracted.append(contacts.strip())

    # Split comma-separated lists and keep unique entries preserving order.
    flat = []
    seen = set()
    for item in extracted:
        for part in [p.strip() for p in item.split(',') if p.strip()]:
            key = part.lower()
            if key not in seen:
                seen.add(key)
                flat.append(part)

    lines = []
    lines.append(f'Stakeholder recommendations for {account_name}:')
    lines.append('')
    lines.append('Suggested steering cadence roles:')
    lines.append('- CIO / VP Platform')
    lines.append('- Security Lead (CISO delegate)')
    lines.append('- Operations Lead')
    lines.append('')
    lines.append('Known contacts from account intel:')
    if flat:
        for c in flat[:15]:
            lines.append(f'- {c}')
    else:
        lines.append('- No explicit contacts found in imported intel.')

    lines.append('')
    lines.append('Recommended next step:')
    lines.append('- Send a biweekly steering invite with agenda: priorities, risks, milestones, and decisions needed.')
    return '\n'.join(lines)


def _parse_business_command(text: str) -> tuple[str | None, str | None]:
    """Parse high-level business commands from free-text CLI input.

    Supported v1 command:
      business account-brief --account <name>
      business account-brief <name>
      business account brief <name>
    """
    if not text:
        return None, None

    normalized = ' '.join(text.strip().split())
    low = normalized.lower()
    if not low.startswith('business '):
        return None, None

    rest = normalized[len('business '):].strip()

    m = re.match(r'(?i)^account(?:-|\s+)brief\s+(?:--account\s+)?(.+)$', rest)
    if m:
        account = m.group(1).strip().strip('"\'')
        return 'account-brief', account

    if re.match(r'(?i)^account(?:-|\s+)brief\s*$', rest):
        return 'account-brief', None

    if rest.lower() in ('help', '--help', '-h'):
        return 'help', None

    return 'unknown', rest


def generate_business_account_brief(account_query: str) -> str | None:
    """Generate an account brief from local intel, with bridge enhancement when available."""
    account = (account_query or '').strip().strip('"\'')
    if not account:
        return None

    # Base structured brief from imported business intel data.
    base_report = generate_intel_report(account)

    # Broader RAG context helps produce strategy even when structured intel is sparse.
    rag_query = (
        f"consulting strategy account brief for {account} "
        "business priorities technology stack stakeholders risks opportunities next steps"
    )
    rag_context = search_training_data_for_rag(rag_query, limit=8, max_chars=5000)
    rag_context = _filter_rag_context_for_account(rag_context, account)

    prompt = (
        f"Create an Account Brief for {account}. "
        "Output sections in this exact order: Executive Summary, Business Priorities, "
        "Technology Signals, Risks, Recommended 30-60-90 Plan, Open Questions, Evidence Used. "
        "Keep it concise, practical, and seller-ready. If evidence is limited, clearly state assumptions."
    )

    llm_resp = call_bridge(prompt, rag_context=rag_context)
    llm_text = extract_assistant_content(llm_resp)
    if isinstance(llm_resp, str) and not llm_resp.startswith('ERR:') and llm_text and llm_text.strip():
        return llm_text.strip()

    # Offline/bridge-failed fallback path.
    if base_report:
        return base_report

    if rag_context:
        display = rag_context.replace('[Relevant context from your personal knowledge base:]', '').strip()
        if display:
            return (
                f"Account Brief (offline) — {account}\n\n"
                "Executive Summary:\n"
                "- Built from local knowledge base entries (bridge unavailable).\n\n"
                "Evidence Used:\n"
                f"{display}\n"
            )

    return None


def find_entry(prefix_or_path):
    # if exact path exists, return it; otherwise search TRAIN_DIR for matching prefix
    if os.path.exists(prefix_or_path):
        return prefix_or_path
    p = Path(TRAIN_DIR)
    matches = sorted(p.glob(f"{prefix_or_path}*"))
    return str(matches[-1]) if matches else None


def append_feedback(entry_path, feedback_text):
    try:
        with open(entry_path, 'r', encoding='utf-8') as fh:
            e = json.load(fh)
    except Exception as exc:
        print('Failed to load entry:', exc, file=sys.stderr)
        return
    fb = {'feedback': feedback_text, 'ts': datetime.now(timezone.utc).isoformat() + 'Z', 'user': os.environ.get('USER', '')}
    e.setdefault('user_feedback', []).append(fb)
    with open(entry_path, 'w', encoding='utf-8') as fh:
        json.dump(e, fh, indent=2)
    print('Feedback appended to', entry_path)


def invoke_remediator(entry_path, do_exec=False):
    rem = os.path.join(BASE_DIR, 'mcp-ai', 'remediate.py')
    if os.path.exists(rem):
        cmd = ['/usr/bin/env', 'python3', rem, '--input', entry_path]
        env = os.environ.copy()
        env['HAL_INTERACTION_ID'] = entry_path
        if do_exec:
            env['ALLOW_AUTO_FIX'] = '1'
            cmd.append('--exec')
        print('Invoking remediator:', ' '.join(cmd))
        subprocess.run(cmd, env=env)
    else:
        print('Remediator not found at', rem)


HELP_TOPICS = {
    'overview': {
        'title': 'HAL Overview',
        'description': 'What is HAL?',
        'content': '''
HAL is an AI-powered assistant that combines local LLM access with intelligent
training data management. HAL learns from your documents, business intelligence,
and interactions to provide contextual answers.

Key capabilities:
  • Chat with local Ollama bridge (offline-first)
  • Import and search training data locally
  • Generate business intelligence reports  
  • Encrypt sensitive training data
  • Auto-ingest new documents and intelligence

For model switching and profile pinning guidance:
  $ HAL help model-routing

All data stays local on your machine — never sent externally.
'''
    },
    'quick-start': {
        'title': 'Quick Start',
        'description': 'Get started in 5 minutes',
        'content': '''
1. Start the bridge (one-time setup):
   $ bash mcp-ai/start-bridge.sh
   (Runs in background; Ctrl+C to stop)
   
2. Ask a question:
   $ HAL "what needs attention?"
   
3. Import documents (optional):
   $ HAL --import-docs ~/Downloads/*.xlsx ~/Downloads/*.pdf
   
4. Import business intelligence:
   $ HAL --import-business-intel ~/GIT/Business_Tools/Training_Data/
   
5. Generate reports:
   $ HAL --intel-report centene
   $ HAL "intel report for davita"

6. Sync curated Red Hat docs (Satellite/AAP/IdM):
    $ HAL --sync-redhat-docs
    $ HAL "sync red hat docs"

7. Keep training data healthy (dedupe/audit):
    $ HAL --training-maintenance
    $ HAL --training-maintenance-apply
    $ HAL --setup-training-maintenance
    $ HAL "optimize training data"
   
8. Set up auto-ingestion (optional):
   $ bash mcp-ai/setup_auto_ingest.sh

That's it! HAL learns and stays updated automatically.
'''
    },
    'import': {
        'title': 'Import Training Data',
        'description': 'Load documents and intelligence into training',
        'content': '''
Import local documents:
  $ HAL --import-docs <paths...>
  
Supported formats: xlsx, xls, csv, tsv, txt, md, pdf, docx, json, yaml, etc.

Examples:
  $ HAL --import-docs ~/Downloads/*.xlsx
  $ HAL --import-docs ~/Documents/report.pdf ~/Downloads/contacts.csv
  $ HAL --import-docs ~/GIT/my-repo/  # imports all supported files
  
Import Business_Tools intelligence:
  $ HAL --import-business-intel <paths...>
  
Examples:
  $ HAL --import-business-intel ~/GIT/Business_Tools/Training_Data/
  $ HAL --import-business-intel file1.jsonl file2.jsonl

Import/sync curated Red Hat product docs:
    $ HAL --import-redhat-docs satellite-6.18 aap-2.6 idm-5.0
    $ HAL --sync-redhat-docs
    $ HAL "go through all the documentation for red hat satellite 6.18, ansible automation platform 2.6, and idm 5.0"

Training data maintenance (dedupe + health report):
    $ HAL --training-maintenance
    $ HAL --training-maintenance-apply
    $ HAL --setup-training-maintenance
    $ HAL "optimize training data"

All imported data is stored locally in ~/.mcp-ai/training/
'''
    },
    'search': {
        'title': 'Search Training Data',
        'description': 'Find information in imported documents',
        'content': '''
When the LLM bridge is offline, HAL automatically searches training data.

Examples:
  $ HAL "who are my contacts at centene"
  $ HAL "what's the address for enterprise"
  $ HAL "show me all accounts in texas"
  $ HAL "what technology does davita use"
  
HAL searches across:
  • Imported documents (spreadsheets, PDFs, etc.)
  • Business intelligence records (accounts, contacts, tech stacks)
  • Previous interactions and recordings
  
Results show source, snippets, and matching content.
'''
    },
    'reports': {
        'title': 'Generate Reports',
        'description': 'Create professional account intelligence reports',
        'content': '''
Generate account intelligence reports:
  $ HAL --intel-report centene
  $ HAL "intel report for davita"
  $ HAL "brief on arrow electronics"
  $ HAL "account brief for ameren"
  
Reports include:
  • Account ownership and territory
  • Technology landscape and stack
  • Red Hat strategic focus areas
  • Primary contacts (clickable email links)
  • Recent activity and headlines (searchable)
  • Key discovery questions
  
Available accounts: Centene, DaVita, Arrow, Ascension, Ameren, Elevance,
Avaya, Enterprise Mobility, Owens & Minor, O'Reilly, Maxar, RGA,
Principal Financial, Western Union, World Wide Technology
'''
    },
    'encrypt': {
        'title': 'Encrypt Training Data',
        'description': 'Encrypt sensitive training information',
        'content': '''
Encrypt all training files with a password:
  $ HAL --encrypt-training
  # Follow prompts to set password
  
Or set password in environment:
  $ export HAL_TRAINING_KEY=mypassword
  $ HAL --encrypt-training
  
Decrypt training files:
  $ HAL --decrypt-training
  # Or with environment password:
  $ export HAL_TRAINING_KEY=mypassword
  $ HAL --decrypt-training
  
Encrypted files are stored as .enc files with:
  • PBKDF2 key derivation (390,000 iterations)
  • Fernet symmetric encryption (AES-128)
  • SHA256 integrity check
  
Password is never stored — you must provide it each time.
'''
    },
    'auto-ingest': {
        'title': 'Automated Ingestion',
        'description': 'Set up auto-import of new data',
        'content': '''
Set up automatic ingestion with cron:
  $ bash mcp-ai/setup_auto_ingest.sh
  
Choose: Daily (recommended), 6-hourly, hourly, or custom

Manual triggers:
  $ HAL --auto-ingest             # Run once manually
  $ HAL --ingest-status           # Show what's been imported
  $ HAL --ingest-reset            # Reset to re-import all files

Auto-ingest monitors:
  • ~/GIT/Business_Tools/Training_Data/ for JSONL intelligence
  • ~/Downloads/ for recent documents
  • ~/Documents/ for recent documents
  
Smart features:
  • Tracks file hashes to detect changes
  • Skips unchanged files automatically
  • Only re-imports modified files
  • Full audit trail in ~/.mcp-ai/auto_ingest.log

View logs:
  $ tail -f ~/.mcp-ai/auto_ingest.log
'''
    },
    'chat': {
        'title': 'Chat Mode',
        'description': 'Interactive conversation with HAL',
        'content': '''
Ask questions and have conversations:
  $ HAL "what are the recent changes"
  $ HAL "how many customers do we have"
  $ HAL "tell me about davita"
  
HAL responds with:
  • Answers from the LLM bridge (if available)
  • Local training data search results (if bridge is offline)
  • Recordings of all interactions for future reference
  
Features:
  • Natural language questions
  • Account auto-detection ("tell me about centene" → full report)
  • Training data search when offline
  • Previous context is saved and retrievable
  
All interactions are recorded locally for learning and audit.
'''
    },
    'examples': {
        'title': 'Common Examples',
        'description': 'Real-world usage scenarios',
        'content': '''
Get sales intelligence:
  $ HAL --intel-report centene
  $ HAL "what technology do we see at davita"
  $ HAL "who do we know at arrow electronics"
  
Import new data:
  $ HAL --import-docs ~/Downloads/Customer_Contacts.xlsx
  $ HAL --import-business-intel ~/Business_Tools/
  
Find contacts:
  $ HAL "email for john at enterprise"
  $ HAL "show me all contacts in healthcare"
  
Search documents:
  $ HAL "what does the roadmap say about openshift"
  $ HAL "find all mentions of ansible"
  
Encrypt sensitive data:
  $ export HAL_TRAINING_KEY=mysecret
  $ HAL --encrypt-training
  
Set up daily automation:
  $ bash mcp-ai/setup_auto_ingest.sh
  # Choose option 1 (daily at 2 AM)
  
View ingestion logs:
  $ tail -f ~/.mcp-ai/auto_ingest.log
'''
    },
    'bridge': {
        'title': 'MCP Bridge Setup',
        'description': 'Connect HAL to the LLM via the bridge',
        'content': '''
The MCP Bridge proxies requests from HAL to your local Ollama LLM.
Without it, HAL searches training data only (offline mode).

Quick Setup:
  1. Ensure Ollama is running:
     $ ollama serve  (in another terminal)
  
  2. Start the bridge:
     $ bash mcp-ai/start-bridge.sh
  
  3. Verify it's working:
     $ HAL --bridge-check
  
Bridge Details:
  • Runs on: http://localhost:1776
  • Connects to Ollama: http://localhost:11434
  • Supports: qwen2.5-coder:7b, llama4:scout, llama3.2:3b, and more
  • Auto-detects available models and selects the best one
  • Gracefully falls back to training data search if unavailable
  
Debugging:
  • Check health: curl http://localhost:1776/health
  • View logs: HAL --bridge-check
  • Check available models: ollama list
  
Running Bridge in Background (systemd):
  [Unit]
  Description=HAL MCP Bridge
  After=network.target
  
  [Service]
  Type=simple
  ExecStart=bash /home/sgallego/GIT/mcp-rhel-manager/mcp-ai/start-bridge.sh
  Restart=on-failure
  
  [Install]
  WantedBy=multi-user.target
  
  Then: sudo systemctl enable --now hal-bridge
'''
    },
        'model-routing': {
                'title': 'Model Routing',
                'description': 'Dynamic model selection by task plus pinning options',
                'content': '''
HAL can switch models automatically by task type.

Task profiles:
    • general      — default chat and mixed requests
    • codegen      — playbooks, roles, collections, Jinja2, Python code
    • strategy     — setup/patch/migration/runbook planning
    • business     — account intel, briefs, stakeholder/business context
    • diagnostics  — troubleshooting, health checks, remediation summaries

See current picks:
    $ HAL --bridge-check

Per-command pinning:
    $ HAL --task-profile strategy "how should I patch aap"
    $ HAL --task-profile codegen "write a playbook for satellite"
    $ HAL --model llama4:scout "summarize this runbook"

Precedence:
    1) --model               (highest)
    2) --task-profile
    3) HAL_FORCE_MODEL
    4) HAL_FORCE_PROFILE
    5) Auto-inferred profile from prompt text

Global environment overrides:
    $ export HAL_FORCE_PROFILE=strategy
    $ export HAL_FORCE_MODEL=llama4:scout

Custom profile mappings (JSON):
    $ export HAL_MODEL_OVERRIDES='{"codegen":["qwen2.5-coder:7b"],"strategy":["llama4:scout"],"business":["mistral"],"diagnostics":["qwen2.5-coder:7b"],"general":["llama4:scout"]}'

Tip:
    Use a coding model for structured artifacts and a reasoning model for
    strategic/business synthesis. Verify available models with:
    $ ollama list
'''
        },
    'advanced': {
        'title': 'Advanced Topics',
        'description': 'Power user features',
        'content': '''
Diagnostics:
  $ HAL --diagnostics
  # Runs full system diagnostics and creates a report
  
Feedback on interactions:
  $ HAL --feedback entry_name_or_prefix "feedback text"
  # Attach feedback to previous interactions
  
Remediator (auto-fix):
  $ HAL --remediate "fix disk errors"
  $ HAL --exec --remediate "auto-fix any issues"
  # ALLOW_AUTO_FIX=1 enables automatic remediation
  
Environment variables:
  HAL_TRAINING_KEY           — Encryption password (optional)
  HAL_DISPLAY_NAME           — Your name (default: Dave)
  HAL_ASSISTANT_NAME         — Assistant name (default: HAL9000)
  HAL_CONVERSATIONAL         — Enable conversational mode (true/false)
    HAL_MODEL_OVERRIDES        — JSON map to override task-based model routing
    HAL_FORCE_PROFILE          — Force one profile globally (general/codegen/strategy/business/diagnostics)
    HAL_FORCE_MODEL            — Force exact model globally (e.g., qwen2.5-coder:7b)
  HAL_INTERACTION_DIR        — Custom interaction storage path
  
Training directory:
  ~/.mcp-ai/training/        — All training data and records
  ~/.mcp-ai/fixes/           — Remediator fixes
  ~/.mcp-ai/reports/         — Generated reports
  ~/.mcp-ai/auto_ingest.log  — Ingestion logs
'''
    },
    'troubleshooting': {
        'title': 'Troubleshooting',
        'description': 'Common issues and solutions',
        'content': '''
HAL doesn't respond (connection refused):
  → LLM bridge offline. HAL switches to training data search.
  → Start bridge: ollama serve
  → Check: curl http://localhost:1776/api/chat
  
Model not found error (404):
  → HAL auto-detects available models
  → Check available: HAL --bridge-check
  → Install missing: ollama pull qwen2.5-coder:7b
  → Supported: qwen2.5-coder:7b, llama4:scout, llama3.2:3b, etc.
  
Password required for decryption:
  → Set env var: export HAL_TRAINING_KEY=yourpassword
  → Or enter interactively when prompted
  
Training data not found:
  → Check import: HAL --ingest-status
  → Verify files: ls -la ~/.mcp-ai/training/
  → Re-import: HAL --import-docs /path/to/files
  
Auto-ingest not running:
  → Check cron: crontab -l
  → View logs: tail -f ~/.mcp-ai/auto_ingest.log
  → Test manually: HAL --auto-ingest
  
Can't import specific file type:
  → Check file extension is supported
  → Try manual import with verbose output
  → Verify file isn't corrupted
  
Report generation fails:
  → Check account name spelling
  → Verify data was imported: HAL --ingest-status
  → Try searching first: HAL "tell me about <account>"
  
Bridge & Model diagnostics:
  → Run: HAL --bridge-check
  → Shows: Ollama status, available models, bridge connection
'''
    },
    'interactive': {
        'title': 'HAL Interactive / REPL Mode',
        'description': 'Multi-turn conversational HAL session',
        'content': '''
Interactive mode keeps a conversation history across multiple turns in a single
terminal session — no need to reinvoke HAL per message.

Start interactive session:
  HAL --interactive

Session commands:
  history   — Show last N turns
  clear     — Clear conversation history and start fresh
  exit      — Exit interactive mode (also: quit, bye, Ctrl-D)

All intent routes are active in interactive mode:
  • Ansible/Jinja2/Python codegen
  • Strategy and runbook responses
  • Git helpers (git log, git commit-msg, etc.)
  • LLM bridge with conversation context injected

Tip: ansible-lint runs automatically after playbook generation in REPL mode.
'''
    },
    'git': {
        'title': 'HAL Git Helpers',
        'description': 'Git log, commit messages, PR descriptions via HAL',
        'content': '''
HAL can help with common Git tasks directly from the command line.

Commands:
  HAL git log           — Show recent git log (last 20 commits, one-line)
  HAL git status        — Show git working tree status (short format)
  HAL git diff          — Show changed file statistics vs HEAD
  HAL git commit-msg    — Generate a conventional commit message from staged diff
  HAL git pr-desc       — Generate a PR description from commits vs main/master

Examples:
  HAL git log
  HAL git commit-msg
  HAL git pr-desc

commit-msg uses the staged diff (or HEAD diff if nothing staged).
pr-desc compares HEAD to main or master branch.
Both use the LLM bridge for message generation; deterministic fallbacks provided.
'''
    },
    'eval': {
        'title': 'HAL Eval / Regression Harness',
        'description': 'Run intent routing accuracy tests',
        'content': '''
HAL includes a built-in regression suite to verify intent routing accuracy.

Run tests:
  HAL --run-tests
    HAL --run-offline-tests

The suites run known prompts through detector/routing checks and verify
each fires correctly. Results are printed to the terminal and saved to:
  ~/.mcp-ai/reports/hal-eval-<timestamp>.json
    ~/.mcp-ai/reports/hal-offline-eval-<timestamp>.json

Use after modifying detector functions to catch regressions.
'''
    },
    'training-report': {
        'title': 'HAL Training Data Report',
        'description': 'Quality and statistics for your training data',
        'content': '''
Show a comprehensive report on your HAL training data:
  HAL --training-report

Report includes:
  • File counts (JSONL, encrypted, other)
  • Total storage size
  • Record counts and date range
  • Record type breakdown
  • Hosts contributing records
  • Models seen in training
  • Data quality score (missing fields, empty responses)

Use to monitor data hygiene before exporting or deploying training bundles.
'''
    },
    'explain': {
        'title': 'HAL --explain: Routing Decision Log',
        'description': 'Show how HAL routed the last query',
        'content': '''
After any HAL query, you can inspect the routing decision:
  HAL --explain

Shows:
  • Timestamp of the last query
  • Intent name (e.g., ansible-codegen, llm-bridge)
  • Handler function that was called
  • Model used (or auto-selected)
  • Task profile (or auto-inferred)
  • First 200 chars of the query

Stored at: ~/.mcp-ai/reports/hal-last-route.json
'''
    },
    'playbook-run': {
        'title': 'HAL Playbook Run',
        'description': 'Run an Ansible playbook via HAL with --check mode',
        'content': '''
Run an Ansible playbook (generated or pre-existing) via HAL:
  HAL --playbook-run /path/to/playbook.yml

Runs: ansible-playbook --check /path/to/playbook.yml
(--check performs a dry-run without making changes)

To run for real, use ansible-playbook directly after reviewing the check output.
'''
    },
}


def show_help(topic: str | None = None) -> None:
    """Display help for a topic or list all topics."""
    if not topic:
        # Show all topics
        print('\n' + '='*80)
        print('HAL Help System')
        print('='*80)
        print('\nAvailable topics:\n')
        for key in sorted(HELP_TOPICS.keys()):
            desc = HELP_TOPICS[key]['description']
            print(f'  {key:20} — {desc}')
        print('\nUsage: HAL help <topic>')
        print('       HAL help overview')
        print('       HAL help quick-start')
        print('='*80 + '\n')
        return
    
    # Show specific topic
    if topic.lower() not in HELP_TOPICS:
        print(f'\nTopic not found: {topic}')
        print(f'Available topics: {", ".join(sorted(HELP_TOPICS.keys()))}')
        print('\nRun: HAL help')
        return
    
    info = HELP_TOPICS[topic.lower()]
    print('\n' + '='*80)
    print(f'{info["title"]}')
    print('='*80)
    print(info['content'])
    print('='*80 + '\n')


# ── Route decision logging ────────────────────────────────────────────────────
_LAST_ROUTE_FILE = os.path.join(os.path.expanduser('~'), '.mcp-ai', 'reports', 'hal-last-route.json')


def _log_route_decision(intent: str, handler: str, model: str | None, profile: str | None, text: str) -> None:
    """Write a small JSON with routing decision for --explain."""
    ensure_dirs()
    data = {
        'timestamp': ts_now(),
        'intent': intent,
        'handler': handler,
        'model': model,
        'profile': profile,
        'query_preview': (text or '')[:200],
    }
    try:
        with open(_LAST_ROUTE_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


# ── Intent routes registry (for --list-intents) ───────────────────────────────
_INTENT_ROUTES = [
    ('greeting',             'Friendly greeting response',                        _is_greeting,                        ['hi', 'hello', 'hey hal']),
    ('wellbeing-check',      'System health check + diagnostics offer',           _is_well_query,                      ['how are you', 'are you ok']),
    ('ansible-codegen',      'Generate Ansible playbooks/roles/collections',      _is_ansible_codegen_query,           ['write a playbook to install nginx', 'create a role for users']),
    ('jinja2-codegen',       'Generate Jinja2 templates',                         _is_jinja2_codegen_query,            ['write a jinja2 template for hosts', 'create a j2 config template']),
    ('python-codegen',       'Generate Python scripts or modules',                _is_python_codegen_query,            ['write a python script to parse json', 'create a python module']),
    ('dependency-advisor',   'Advise on Ansible/Python/Git install requirements', _is_dependency_advisor_query,        ['what do i need to install for ansible', 'what python packages for jinja2']),
    ('ansible-eda-use-cases','List practical Event-Driven Ansible uses',          _is_ansible_eda_use_case_query,      ['what is ansible eda used for', 'list ansible eda use cases']),
    ('training-import-url',  'Execute URL import from natural language',           _is_training_import_url_execute_query,['import training data from url https://example.com', 'ingest url https://docs.redhat.com ... depth 5']),
    ('training-import-txt',  'Execute TXT import from natural language',           _is_training_import_txt_execute_query,['import training data from txt /tmp/a.txt', 'ingest text file ./notes.txt']),
    ('training-import-help', 'How to import training data from URL/TXT',           _is_training_data_import_query,      ['how do i import training data from url', 'how to import txt into hal training']),
    ('training-bundle',      'Export training data as portable zip bundle',       _is_training_bundle_export_query,    ['zip up my training data', 'bundle hal knowledge for usb']),
    ('server-update-strat',  'Server patching and update strategy runbook',       _is_server_update_strategy_query,    ['how do i patch rhel servers', 'server update strategy']),
    ('satellite-patch-strat','Satellite patching strategy runbook',               _is_satellite_patch_strategy_query,  ['how do i patch with satellite', 'satellite patch strategy']),
    ('aap-patch-strat',      'AAP/Ansible patching strategy runbook',             _is_aap_patch_strategy_query,        ['aap patching strategy', 'how to patch with aap']),
    ('idm-setup',            'IdM/FreeIPA setup and enrollment runbook',          _is_idm_setup_query,                 ['set up idm', 'configure ipa server']),
    ('aap-migration-strat',  'AAP migration strategy and planning',               _is_aap_migration_strategy_query,    ['migrate from tower to aap', 'aap migration plan']),
    ('satellite-aap-conn',   'Satellite ↔ AAP connection and integration',        _is_satellite_aap_connection_strategy_query, ['connect satellite to aap', 'integrate satellite with ansible']),
    ('satellite-e2e-setup',  'Full Satellite end-to-end setup runbook',           _is_satellite_end_to_end_setup_query, ['set up satellite end to end', 'complete satellite installation']),
    ('satellite-pxe',        'Satellite PXE provisioning strategy',               _is_satellite_pxe_strategy_query,    ['pxe boot with satellite', 'provision hosts via pxe satellite']),
    ('aap-mcp-setup',        'MCP server setup in AAP',                           _is_aap_mcp_setup_query,             ['set up mcp in aap', 'configure mcp server for aap']),
    ('satellite-mcp-setup',  'MCP server setup in Satellite',                     _is_satellite_mcp_setup_query,       ['satellite mcp setup', 'configure mcp for satellite']),
    ('strategy',             'Consulting strategy generation for accounts',       _is_strategy_query,                  ['strategy for acme corp', 'account plan for centene']),
    ('stakeholder',          'Account stakeholder/contact lookup',                _is_stakeholder_query,               ['who are the contacts at acme', 'stakeholders for centene']),
    ('subscription-csv',     'Customer subscription CSV export',                  _is_subscription_csv_query,          ['subscription report csv', 'export subscription data']),
    ('stock-price',          'Account stock price lookup from intel',             _is_stock_price_query,               ['what is acme stock price', 'stock for centene']),
    ('operational-howto',    'Operational how-to runbooks (RHEL/Linux/Ansible)',  _is_operational_howto_query,         ['how do i configure ntp', 'how to add a user in rhel']),
]


def _show_list_intents() -> None:
    """Print all known HAL intent routes in a formatted table."""
    print('\n' + '='*100)
    print('HAL Intent Routes')
    print('='*100)
    print(f'\n  {"INTENT":<26} {"DESCRIPTION":<48} {"EXAMPLE TRIGGERS"}')
    print('  ' + '-'*96)
    for name, desc, _fn, examples in _INTENT_ROUTES:
        ex_str = '  |  '.join(examples[:2])
        print(f'  {name:<26} {desc:<48} {ex_str}')
    print('\n  Unmatched queries → LLM bridge (model auto-selected by task profile)')
    print('='*100 + '\n')


# ── Training data quality report ──────────────────────────────────────────────
def _generate_training_report() -> str:
    """Scan ~/.mcp-ai/training and produce a quality/stats report."""
    ensure_dirs()
    train_path = Path(TRAIN_DIR)
    if not train_path.exists():
        return 'Training directory not found: ' + str(train_path)

    files = sorted(train_path.glob('**/*'))
    jsonl_files = [f for f in files if f.suffix in ('.jsonl', '.json') and not f.name.endswith('.enc')]
    enc_files   = [f for f in files if f.suffix == '.enc']
    other_files = [f for f in files if f.is_file() and f not in jsonl_files and f not in enc_files]

    total_bytes = sum(f.stat().st_size for f in files if f.is_file())
    total_records = 0
    missing_request = 0
    missing_response = 0
    empty_response = 0
    dates = []
    record_types: dict[str, int] = {}
    hosts: dict[str, int] = {}
    models: dict[str, int] = {}

    for fpath in jsonl_files:
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as fh:
                content = fh.read().strip()
            if not content:
                continue
            # Support both single-object JSON and JSONL (one object per line)
            lines = content.splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                total_records += 1
                rtype = obj.get('type', 'unknown')
                record_types[rtype] = record_types.get(rtype, 0) + 1
                if not obj.get('request'):
                    missing_request += 1
                if 'ai_response_raw' not in obj and 'response' not in obj:
                    missing_response += 1
                elif not (obj.get('ai_response_raw') or obj.get('response') or '').strip():
                    empty_response += 1
                ts = obj.get('timestamp', '')
                if ts:
                    dates.append(ts)
                host = obj.get('host', '')
                if host:
                    hosts[host] = hosts.get(host, 0) + 1
                model = obj.get('model', '')
                if model:
                    models[model] = models.get(model, 0) + 1
        except Exception:
            continue

    lines_out = [
        f'\n{"="*70}',
        f'HAL Training Data Report',
        f'{"="*70}',
        f'',
        f'Directory     : {train_path}',
        f'JSONL files   : {len(jsonl_files)}',
        f'Encrypted     : {len(enc_files)}',
        f'Other files   : {len(other_files)}',
        f'Total size    : {total_bytes / (1024*1024):.2f} MB  ({total_bytes:,} bytes)',
        f'',
        f'Total records : {total_records}',
    ]
    if dates:
        lines_out += [
            f'Earliest      : {min(dates)}',
            f'Latest        : {max(dates)}',
        ]
    if record_types:
        lines_out.append(f'')
        lines_out.append(f'Record types:')
        for k, v in sorted(record_types.items(), key=lambda x: -x[1]):
            lines_out.append(f'  {k:<35} {v}')
    if hosts:
        lines_out.append(f'')
        lines_out.append(f'Hosts contributing:')
        for k, v in sorted(hosts.items(), key=lambda x: -x[1]):
            lines_out.append(f'  {k:<35} {v} records')
    if models:
        lines_out.append(f'')
        lines_out.append(f'Models recorded in training:')
        for k, v in sorted(models.items(), key=lambda x: -x[1]):
            lines_out.append(f'  {k:<35} {v} records')
    lines_out.append(f'')
    if total_records > 0:
        quality_score = 100
        if missing_request > 0:
            quality_score -= min(30, int(missing_request / total_records * 100))
        if empty_response > 0:
            quality_score -= min(30, int(empty_response / total_records * 100))
        if missing_response > 0:
            quality_score -= min(20, int(missing_response / total_records * 100))
        lines_out += [
            f'Data quality:',
            f'  Missing request fields  : {missing_request}',
            f'  Empty response fields   : {empty_response}',
            f'  Missing response fields : {missing_response}',
            f'  Quality score           : {quality_score}/100',
        ]
    lines_out += [f'', f'{"="*70}']
    return '\n'.join(lines_out)


# ── Eval / regression harness ─────────────────────────────────────────────────
_EVAL_SUITE = [
    ('hi there',                               'greeting',             _is_greeting),
    ('hello hal',                              'greeting',             _is_greeting),
    ('how are you doing',                      'wellbeing-check',      _is_well_query),
    ('write a playbook to install httpd',      'ansible-codegen',      _is_ansible_codegen_query),
    ('create a role for managing users',       'ansible-codegen',      _is_ansible_codegen_query),
    ('generate a jinja2 template for hosts',   'jinja2-codegen',       _is_jinja2_codegen_query),
    ('write a python script to parse csv',     'python-codegen',       _is_python_codegen_query),
    ('what do i need to install for ansible',  'dependency-advisor',   _is_dependency_advisor_query),
    ('zip up my training data for a usb key',  'training-bundle',      _is_training_bundle_export_query),
    ('what is the server update strategy',     'server-update-strat',  _is_server_update_strategy_query),
    ('how do i patch hosts with satellite',    'satellite-patch-strat',_is_satellite_patch_strategy_query),
    ('aap patching strategy plan',             'aap-patch-strat',      _is_aap_patch_strategy_query),
    ('set up idm on rhel',                     'idm-setup',            _is_idm_setup_query),
    ('migrate aap 2.5 to 2.6 strategy plan',  'aap-migration-strat',  _is_aap_migration_strategy_query),
    ('strategy to connect satellite to ansible aap', 'satellite-aap-conn', _is_satellite_aap_connection_strategy_query),
    ('satellite setup strategy ansible manifest activation keys dhcp pxe', 'satellite-e2e-setup', _is_satellite_end_to_end_setup_query),
    ('satellite pxe setup configure dhcp tftp provisioning', 'satellite-pxe', _is_satellite_pxe_strategy_query),
    ('set up mcp server in aap',               'aap-mcp-setup',        _is_aap_mcp_setup_query),
    ('configure mcp in satellite',             'satellite-mcp-setup',  _is_satellite_mcp_setup_query),
    ('what is the strategy for acme corp',     'strategy',             _is_strategy_query),
]


_OFFLINE_EVAL_SUITE = [
    ('can you give me a list of things users might use ansible eda for', 'ansible-eda-use-cases', _is_ansible_eda_use_case_query),
    ('how do i configure capsules with a load balancer in satellite 6.18', 'satellite-ops-doc-route', lambda q: _is_operational_howto_query(q) and bool(_preferred_doc_source_patterns(q))),
    ('how do i use event driven ansible automation decisions in aap 2.6', 'aap-eda-doc-route', lambda q: bool(_preferred_doc_source_patterns(q))),
    ('please go through all the documentation for redhat satellite 6.18, ansible automation platform 2.6, and idm 5.0', 'redhat-docs-ingest', _is_redhat_docs_ingest_query),
    ('sync red hat docs', 'redhat-docs-sync', _is_redhat_docs_sync_query),
    ('optimize training data', 'training-maintenance', _is_training_maintenance_query),
]


def _run_eval_suite() -> str:
    """Run all eval suite prompts and report intent routing accuracy."""
    passed = 0
    failed = 0
    results = []
    for prompt, expected_intent, detector_fn in _EVAL_SUITE:
        fired = bool(detector_fn(prompt))
        status = 'PASS' if fired else 'FAIL'
        if fired:
            passed += 1
        else:
            failed += 1
        results.append((status, expected_intent, prompt))

    # Write report
    ensure_dirs()
    report_path = os.path.join(REPORTS_DIR, f'hal-eval-{ts_now()}.json')
    report_data = {
        'timestamp': ts_now(),
        'total': len(_EVAL_SUITE),
        'passed': passed,
        'failed': failed,
        'accuracy_pct': round(passed / len(_EVAL_SUITE) * 100, 1),
        'results': [{'status': s, 'intent': i, 'prompt': p} for s, i, p in results],
    }
    try:
        with open(report_path, 'w', encoding='utf-8') as fh:
            json.dump(report_data, fh, indent=2)
    except Exception:
        report_path = '(write failed)'

    lines_out = [
        f'\n{"="*70}',
        f'HAL Intent Routing Eval Suite',
        f'{"="*70}',
        f'',
        f'{"STATUS":<8} {"INTENT":<28} PROMPT',
        '  ' + '-'*64,
    ]
    for status, intent, prompt in results:
        marker = '✓' if status == 'PASS' else '✗'
        lines_out.append(f'  {marker} {status:<6} {intent:<28} {prompt}')
    lines_out += [
        f'',
        f'Result: {passed}/{len(_EVAL_SUITE)} passed  ({report_data["accuracy_pct"]}% accuracy)',
        f'Report: {report_path}',
        f'{"="*70}',
    ]
    return '\n'.join(lines_out)


def _run_offline_eval_suite() -> str:
    """Run offline-focused routing eval prompts and report pass/fail."""
    passed = 0
    failed = 0
    results = []
    for prompt, expected_intent, detector_fn in _OFFLINE_EVAL_SUITE:
        fired = bool(detector_fn(prompt))
        status = 'PASS' if fired else 'FAIL'
        if fired:
            passed += 1
        else:
            failed += 1
        results.append((status, expected_intent, prompt))

    ensure_dirs()
    report_path = os.path.join(REPORTS_DIR, f'hal-offline-eval-{ts_now()}.json')
    report_data = {
        'timestamp': ts_now(),
        'total': len(_OFFLINE_EVAL_SUITE),
        'passed': passed,
        'failed': failed,
        'accuracy_pct': round(passed / len(_OFFLINE_EVAL_SUITE) * 100, 1),
        'results': [{'status': s, 'intent': i, 'prompt': p} for s, i, p in results],
    }
    try:
        with open(report_path, 'w', encoding='utf-8') as fh:
            json.dump(report_data, fh, indent=2)
    except Exception:
        report_path = '(write failed)'

    lines_out = [
        f'\n{"="*70}',
        'HAL Offline Routing Eval Suite',
        f'{"="*70}',
        '',
        f'{"STATUS":<8} {"INTENT":<28} PROMPT',
        '  ' + '-' * 64,
    ]
    for status, intent, prompt in results:
        marker = '✓' if status == 'PASS' else '✗'
        lines_out.append(f'  {marker} {status:<6} {intent:<28} {prompt}')
    lines_out += [
        '',
        f'Result: {passed}/{len(_OFFLINE_EVAL_SUITE)} passed  ({report_data["accuracy_pct"]}% accuracy)',
        f'Report: {report_path}',
        f'{"="*70}',
    ]
    return '\n'.join(lines_out)


# ── Ansible-lint integration ──────────────────────────────────────────────────
def _ansible_lint_check(playbook_content: str) -> str | None:
    """Run ansible-lint on playbook_content via a temp file. Returns lint output or None if not available."""
    ansible_lint = shutil.which('ansible-lint')
    if not ansible_lint:
        return None
    import tempfile
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False, encoding='utf-8') as tf:
            tf.write(playbook_content)
            tmp_path = tf.name
        proc = subprocess.run(
            [ansible_lint, '--nocolor', '--parseable', tmp_path],
            capture_output=True, text=True, timeout=30
        )
        output = (proc.stdout or '').strip() + ('\n' + (proc.stderr or '').strip() if proc.stderr else '')
        output = output.strip()
        return output if output else 'ansible-lint: no issues found'
    except Exception as e:
        return f'ansible-lint: could not run ({e})'
    finally:
        try:
            if tmp_path:
                os.unlink(tmp_path)
        except Exception:
            pass


# ── Git helpers ───────────────────────────────────────────────────────────────
def _is_git_helper_query(text: str) -> bool:
    """Detect `HAL git <subcommand>` style requests."""
    if not text:
        return False
    q = text.strip().lower()
    return bool(re.match(r'^git\s+(log|commit.?msg|commit-message|pr.?desc|pr-description|diff|status|summary)\b', q))


def _handle_git_helper(text: str) -> str:
    """Handle `HAL git <subcommand>` commands."""
    q = text.strip().lower()
    # Determine subcommand
    m = re.match(r'^git\s+(log|commit.?msg|commit-message|pr.?desc|pr-description|diff|status|summary)(.*)', q, re.IGNORECASE)
    if not m:
        return 'Usage: HAL git log | HAL git commit-msg | HAL git pr-desc | HAL git diff | HAL git status'
    subcmd = m.group(1).lower().replace(' ', '-').replace('_', '-')
    extra = m.group(2).strip()

    # Run the actual git command
    try:
        if subcmd in ('log',):
            result = subprocess.run(['git', 'log', '--oneline', '-20'], capture_output=True, text=True, timeout=10)
            git_out = result.stdout.strip() or result.stderr.strip() or '(no git log output)'
            return f'Recent git log:\n\n{git_out}'

        elif subcmd in ('status', 'summary'):
            result = subprocess.run(['git', 'status', '--short'], capture_output=True, text=True, timeout=10)
            git_out = result.stdout.strip() or result.stderr.strip() or '(working tree clean)'
            return f'Git status:\n\n{git_out}'

        elif subcmd in ('diff',):
            result = subprocess.run(['git', 'diff', '--stat', 'HEAD'], capture_output=True, text=True, timeout=10)
            git_out = result.stdout.strip() or result.stderr.strip() or '(no diff)'
            return f'Git diff stat:\n\n{git_out}'

        elif subcmd in ('commit-msg', 'commit-message'):
            # Get the staged diff and send to LLM to write a commit message
            result = subprocess.run(['git', 'diff', '--cached'], capture_output=True, text=True, timeout=15)
            diff_text = result.stdout.strip()
            if not diff_text:
                result2 = subprocess.run(['git', 'diff', 'HEAD'], capture_output=True, text=True, timeout=15)
                diff_text = result2.stdout.strip()
            if not diff_text:
                return 'No staged or uncommitted changes found. Stage changes with `git add` first.'
            # Trim very large diffs
            diff_snippet = diff_text[:3000]
            prompt = (
                'You are a Git commit message writer. '
                'Write a concise, conventional commit message (subject + body) for this diff. '
                'Follow Conventional Commits format (feat/fix/chore/docs/refactor). '
                'Output ONLY the commit message text, no explanation:\n\n'
                f'```diff\n{diff_snippet}\n```'
            )
            resp = call_bridge(prompt, task_profile='codegen')
            msg = extract_assistant_content(resp)
            return f'Suggested commit message:\n\n{msg or "(model offline — stage your diff and write: feat: <what changed>)"}'

        elif subcmd in ('pr-desc', 'pr-description'):
            # Get log since main/master and last diff summary
            result = subprocess.run(['git', 'log', 'main..HEAD', '--oneline'], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                result = subprocess.run(['git', 'log', 'master..HEAD', '--oneline'], capture_output=True, text=True, timeout=10)
            log_text = result.stdout.strip() or '(single branch — no base comparison available)'
            result2 = subprocess.run(['git', 'diff', '--stat', 'main'], capture_output=True, text=True, timeout=10)
            if result2.returncode != 0:
                result2 = subprocess.run(['git', 'diff', '--stat', 'master'], capture_output=True, text=True, timeout=10)
            stat_text = result2.stdout.strip() or ''
            prompt = (
                'You are a pull request description writer. '
                'Write a clear PR description with sections: Summary, Changes, Testing. '
                'Output ONLY the PR description, no preamble:\n\n'
                f'Commits:\n{log_text}\n\nChanged files:\n{stat_text}'
            )
            resp = call_bridge(prompt, task_profile='strategy')
            msg = extract_assistant_content(resp)
            return f'Suggested PR description:\n\n{msg or "(model offline)"}'

        else:
            return f'Unknown git subcommand: {subcmd}\nUsage: HAL git log | commit-msg | pr-desc | diff | status'

    except FileNotFoundError:
        return 'git is not installed or not in PATH.'
    except subprocess.TimeoutExpired:
        return 'git command timed out.'


# ── Interactive REPL ──────────────────────────────────────────────────────────
def _run_interactive_repl(user: str) -> None:
    """Run a multi-turn interactive HAL session. Type 'exit' or Ctrl-D to quit."""
    print(f'\n{"="*60}')
    print(f'  HAL Interactive Mode  (type exit or Ctrl-D to quit)')
    print(f'{"="*60}\n')
    print(f'Hello {HAL_DISPLAY_NAME}! I\'m ready. Ask me anything.\n')
    print('Commands: history, clear, stats, cache, suggestions, help\n')

    conversation_history: list[dict] = []

    while True:
        try:
            user_input = input(f'{HAL_DISPLAY_NAME}> ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nGoodbye!')
            break

        if not user_input:
            continue
        if user_input.lower() in ('exit', 'quit', 'bye', 'goodbye'):
            print('\nGoodbye!')
            break
        if user_input.lower() == 'help':
            print(f'\n📚 HAL Interactive Commands:\n')
            print('  history      - Show conversation history')
            print('  clear        - Clear conversation history')
            print('  stats        - 📊 Show session statistics (queries, cache hits, models)')
            print('  cache        - 💾 Show cache statistics')
            print('  suggestions  - 💡 Get smart suggestions for next queries')
            print('  help         - Show this help\n')
            continue
        if user_input.lower() == 'history':
            if not conversation_history:
                print('(no history yet)')
            for i, turn in enumerate(conversation_history, 1):
                print(f'[{i}] {turn["role"].upper()}: {turn["content"][:120]}')
            print()
            continue
        if user_input.lower() == 'clear':
            conversation_history.clear()
            print('Conversation history cleared.\n')
            continue
        if user_input.lower() == 'stats':
            total = _SESSION_CONTEXT['total_calls']
            hits = _SESSION_CONTEXT['cache_hits']
            print(f'\n📊 Session Stats:')
            print(f'  Queries: {total} | Cache Hits: {hits} ({100*hits//max(1, total)}%) | Models: {len(_SESSION_CONTEXT["models_used"])}')
            if _SESSION_CONTEXT['models_used']:
                for model, count in sorted(_SESSION_CONTEXT['models_used'].items(), key=lambda x: -x[1])[:3]:
                    print(f'    • {model}: {count}x')
            print()
            continue
        if user_input.lower() == 'cache':
            print(f'\n💾 Cache: {len(_RESPONSE_CACHE)} entries (~{len(json.dumps(_RESPONSE_CACHE))//1024}KB)')
            print(f'  TTL: {_RESPONSE_CACHE_TTL}s\n')
            continue
        if user_input.lower() == 'suggestions':
            sugg = _suggest_next_queries('\n'.join([q['query'] for q in _SESSION_CONTEXT['queries'][-3:]]))
            if sugg:
                print(f'\n💡 Suggestions:\n')
                for s in sugg:
                    print(f'  • {s}')
                print()
            continue

        conversation_history.append({'role': 'user', 'content': user_input})

        # Route through deterministic handlers first
        response = None

        if _is_greeting(user_input):
            variants = [
                f"Hi {HAL_DISPLAY_NAME}! What can I do for you?",
                f"Hello {HAL_DISPLAY_NAME}! Ready to help.",
                f"Hey! What would you like to do?"
            ]
            response = random.choice(variants)

        elif _is_git_helper_query(user_input):
            response = _handle_git_helper(user_input)

        elif _is_training_bundle_export_query(user_input):
            response = generate_training_bundle_export_response(user_input, user)

        elif _is_dependency_advisor_query(user_input):
            response = generate_dependency_advice(user_input)

        elif _is_ansible_codegen_query(user_input):
            raw = generate_ansible_codegen_response(user_input)
            # Extract playbook YAML for lint check
            lint_result = None
            yaml_m = re.search(r'```(?:yaml|yml)?\n(.*?)```', raw, re.DOTALL)
            if yaml_m:
                lint_result = _ansible_lint_check(yaml_m.group(1))
            response = raw
            if lint_result:
                response += f'\n\n── ansible-lint ─────────────────────\n{lint_result}\n'

        elif _is_jinja2_codegen_query(user_input):
            response = generate_jinja2_template_response(user_input)

        elif _is_python_codegen_query(user_input):
            response = generate_python_script_response(user_input)

        elif _is_server_update_strategy_query(user_input):
            response = generate_server_update_strategy_response()

        elif _is_satellite_patch_strategy_query(user_input):
            response = generate_satellite_patch_strategy_response(user_input)

        elif _is_aap_patch_strategy_query(user_input):
            response = generate_aap_patch_strategy_response(user_input)

        elif _is_idm_setup_query(user_input):
            response = generate_idm_setup_response(user_input)

        elif _is_aap_migration_strategy_query(user_input):
            response = generate_aap_migration_strategy_response(user_input)

        elif _is_satellite_aap_connection_strategy_query(user_input):
            response = generate_satellite_aap_connection_strategy_response(user_input)

        elif _is_satellite_end_to_end_setup_query(user_input):
            response = generate_satellite_end_to_end_setup_response(user_input)

        elif _is_satellite_pxe_strategy_query(user_input):
            response = generate_satellite_pxe_strategy_response(user_input)

        elif _is_aap_mcp_setup_query(user_input):
            response = generate_aap_mcp_setup_response()

        elif _is_satellite_mcp_setup_query(user_input):
            response = generate_satellite_mcp_setup_response()

        else:
            # LLM path with full conversation history injected as context
            rag_context = search_training_data_for_rag(user_input)
            if conversation_history and len(conversation_history) > 1:
                history_snippet = '\n'.join(
                    f'{t["role"].upper()}: {t["content"]}'
                    for t in conversation_history[-6:-1]  # last 3 exchanges
                )
                rag_context = (rag_context or '') + f'\n\nConversation history:\n{history_snippet}'
            raw = call_bridge(user_input, rag_context=rag_context)
            response = extract_assistant_content(raw) or raw

        print(f'\nHAL: {response}\n')
        conversation_history.append({'role': 'assistant', 'content': response or ''})
        write_interaction(user, user_input, response or '')


def main():
    global RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE
    ap = argparse.ArgumentParser()
    ap.add_argument('text', nargs='*')  # Capture all positional args
    ap.add_argument('--remediate', action='store_true', help='Invoke remediator on this interaction')
    ap.add_argument('--exec', action='store_true', help='Allow remediator to execute fixes (sets ALLOW_AUTO_FIX=1)')
    ap.add_argument('--diagnostics', action='store_true', help='Run full diagnostics via local server.full_diagnostics_json() and record result')
    ap.add_argument('--status', action='store_true', help='Quick one-line system health summary (Critical/Warning counts + top issue)')
    ap.add_argument('--feedback', nargs=2, metavar=('ENTRY', 'FEEDBACK'), help='Append feedback to an existing entry')
    ap.add_argument('--import-docs', nargs='+', metavar='PATH', help='Import local documents/spreadsheets into training data')
    ap.add_argument('--import-url', nargs='+', metavar='URL', help='Fetch and ingest one or more URLs into training data (default depth: 3)')
    ap.add_argument('--import-txt', nargs='+', metavar='FILE', help='Ingest one or more plain-text files into training data')
    # Numeric depth shortcuts for --import-url: --1 through --9 (default 3)
    _depth_group = ap.add_mutually_exclusive_group()
    for _d in range(1, 10):
        _depth_group.add_argument(f'--{_d}', dest='import_depth', action='store_const', const=_d,
                                  help=f'Set crawl depth to {_d} for --import-url')
    ap.set_defaults(import_depth=3)
    ap.add_argument('--import-redhat-docs', nargs='*', metavar='DOCSET', help='Import curated Red Hat docs (default: satellite-6.18 aap-2.6 idm-5.0)')
    ap.add_argument('--sync-redhat-docs', action='store_true', help='Sync default curated Red Hat docs (satellite-6.18 aap-2.6 idm-5.0)')
    ap.add_argument('--training-maintenance', action='store_true', help='Run training data maintenance report (dry-run duplicate analysis)')
    ap.add_argument('--training-maintenance-apply', action='store_true', help='Run training data maintenance and remove duplicate supplemental_document records')
    ap.add_argument('--enrich-companies', nargs='+', metavar='COMPANY', help='Enrich named companies from public web sources into training data')
    ap.add_argument('--enrich-from-training', action='store_true', help='Discover companies from training docs and enrich from public web sources')
    ap.add_argument('--max-enrich-companies', type=int, default=10, help='Limit company discovery count for --enrich-from-training')
    ap.add_argument('--encrypt-training', action='store_true', help='Encrypt local training files under ~/.mcp-ai/training')
    ap.add_argument('--decrypt-training', action='store_true', help='Decrypt local encrypted training files under ~/.mcp-ai/training')
    ap.add_argument('--export-training-bundle', action='store_true', help='Create a portable zip bundle of ~/.mcp-ai/training with README and deploy helper script')
    ap.add_argument('--bundle-output-dir', metavar='PATH', help='Output directory for --export-training-bundle (default: ~/Downloads or HAL_TRAIN_BUNDLE_DIR)')
    ap.add_argument('--import-business-intel', nargs='+', metavar='PATH', help='Import Business_Tools JSONL intel data into HAL training')
    ap.add_argument('--intel-report', metavar='ACCOUNT', help='Generate an account intel report from training data')
    ap.add_argument('--intel-report-all', nargs='+', metavar='ACCOUNT', help='Generate intel reports for multiple accounts (quote names with spaces)')
    ap.add_argument('--intel-report-file', metavar='PATH', help='Generate intel reports for account names in file (one account per line)')
    ap.add_argument('--auto-ingest', action='store_true', help='Auto-ingest new intelligence data from watch directories')
    ap.add_argument('--ingest-status', action='store_true', help='Show auto-ingest import history and status')
    ap.add_argument('--ingest-reset', action='store_true', help='Reset import tracker (re-import everything on next auto-ingest)')
    ap.add_argument('--bridge-check', action='store_true', help='Check bridge connectivity and available models')
    ap.add_argument('--session-stats', action='store_true', help='🆕 Show session statistics (cache hits, models used, timing)')
    ap.add_argument('--analytics', action='store_true', help='Show detailed query analytics dashboard')
    ap.add_argument('--suggestions', action='store_true', help='Show smart suggestions for next queries')
    ap.add_argument('--cache-clear', action='store_true', help='Clear all cached responses')
    ap.add_argument('--cache-stats', action='store_true', help='Show cache statistics and contents')
    ap.add_argument('--inventory', action='store_true', help='Run system inventory detection and first-run setup')
    ap.add_argument('--metrics', action='store_true', help='Display trending metrics dashboard')
    ap.add_argument('--predict', action='store_true', help='Show predictive alerts for next 7 days')
    ap.add_argument('--metric-days', type=int, default=7, help='Days of history for metrics (default: 7)')
    ap.add_argument('--studio', action='store_true', help='Launch HAL Studio (stocks, images, videos)')
    ap.add_argument('--voice', action='store_true', help='Speak HAL responses using local TTS (espeak/espeak-ng)')
    ap.add_argument('--voice-rate', type=int, default=170, help='Speech rate for --voice (default: 170)')
    ap.add_argument('--voice-name', metavar='VOICE', help='Optional voice name for local TTS (example: en-us)')
    ap.add_argument('--voice-chat', action='store_true', help='Interactive voice mode: type prompts, HAL speaks responses')
    ap.add_argument('--studio-pipeline', metavar='PROMPT', help='Run one-command HAL Studio pipeline: image + music + vocals + OBS assets')
    ap.add_argument('--studio-outdir', metavar='PATH', default='./hal-scenes', help='Output directory for --studio-pipeline (default: ./hal-scenes)')
    ap.add_argument('--studio-seconds', type=int, default=20, help='Audio length in seconds for --studio-pipeline (default: 20)')
    ap.add_argument('--studio-bpm', type=int, default=120, help='Music BPM for --studio-pipeline (default: 120)')
    ap.add_argument('--studio-image-backend', choices=['auto', 'diffusers', 'ollama-assisted', 'poster'], default='auto', help='Image backend for --studio-pipeline')
    ap.add_argument('--studio-voice', metavar='VOICE', help='Optional TTS voice for --studio-pipeline')
    ap.add_argument('--task-profile', choices=['general', 'codegen', 'strategy', 'business', 'diagnostics'], help='Force task profile for model selection for this command')
    ap.add_argument('--model', metavar='MODEL', help='Force exact Ollama model for this command (overrides --task-profile)')
    ap.add_argument('--account', metavar='ACCOUNT', help='Account name for business commands (e.g., HAL business account-brief --account centene)')
    ap.add_argument('--interactive', action='store_true', help='Launch multi-turn interactive HAL REPL session')
    ap.add_argument('--list-intents', action='store_true', help='List all known intent routes with examples')
    ap.add_argument('--training-report', action='store_true', help='Show training data quality and statistics report')
    ap.add_argument('--explain', action='store_true', help='Show routing decision from the last HAL invocation')
    ap.add_argument('--run-tests', action='store_true', help='Run intent routing eval/regression test suite')
    ap.add_argument('--run-offline-tests', action='store_true', help='Run offline routing eval/regression test suite')
    ap.add_argument('--bundle-encrypt', action='store_true', help='Encrypt the training bundle zip with a passphrase (use with --export-training-bundle)')
    ap.add_argument('--playbook-run', metavar='FILE', help='Run a generated or existing playbook with ansible-playbook --check')
    ap.add_argument('--setup-training-maintenance', action='store_true', help='Install scheduled training maintenance cron jobs (daily dry-run + weekly apply)')
    args = ap.parse_args()

    global VOICE_ENABLED, VOICE_RATE, VOICE_NAME
    VOICE_ENABLED = bool(args.voice or args.voice_chat or os.environ.get('HAL_VOICE', '0').lower() in ('1', 'true', 'yes', 'y'))
    VOICE_RATE = int(args.voice_rate or 170)
    VOICE_NAME = args.voice_name or os.environ.get('HAL_VOICE_NAME')

    if VOICE_ENABLED and not _speech_ready():
        print('Voice requested but no local TTS engine found (espeak/espeak-ng). Continuing in text mode.', file=sys.stderr)
        VOICE_ENABLED = False

    # Runtime one-command overrides used by choose_model_for_task().
    RUNTIME_FORCE_MODEL = args.model.strip() if args.model else None
    RUNTIME_FORCE_PROFILE = args.task_profile.strip().lower() if args.task_profile else None

    if args.feedback:
        entry_ref, fb = args.feedback
        entry = find_entry(entry_ref)
        if not entry:
            print('No matching entry found for', entry_ref, file=sys.stderr)
            sys.exit(2)
        append_feedback(entry, fb)
        sys.exit(0)

    if args.encrypt_training and args.decrypt_training:
        print('Use only one of --encrypt-training or --decrypt-training at a time.', file=sys.stderr)
        sys.exit(2)

    if args.bundle_output_dir and not args.export_training_bundle:
        print('--bundle-output-dir requires --export-training-bundle.', file=sys.stderr)
        sys.exit(2)

    if args.bundle_encrypt and not args.export_training_bundle:
        print('--bundle-encrypt requires --export-training-bundle.', file=sys.stderr)
        sys.exit(2)

    if args.import_docs:
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_documents.py')
        if not os.path.exists(ing):
            print('Document ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing, '--recursive', *args.import_docs]
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.import_url:
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_urls.py')
        if not os.path.exists(ing):
            print('URL ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        depth = str(getattr(args, 'import_depth', 3) or 3)
        print(f'Ingesting {len(args.import_url)} URL(s) at depth {depth}...')
        cmd = ['/usr/bin/env', 'python3', ing, '--depth', depth, '--same-host-only', *args.import_url]
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.import_txt:
        import hashlib as _hashlib
        ensure_dirs()
        saved = []
        errors = []
        for txt_path in args.import_txt:
            if not os.path.isfile(txt_path):
                errors.append(f'File not found: {txt_path}')
                continue
            try:
                with open(txt_path, 'r', encoding='utf-8', errors='replace') as _fh:
                    content = _fh.read()
            except Exception as _exc:
                errors.append(f'Cannot read {txt_path}: {_exc}')
                continue
            _h = _hashlib.sha256(os.path.abspath(txt_path).encode()).hexdigest()[:12]
            fname = os.path.join(TRAIN_DIR, f'txt-{_h}.json')
            entry = {
                'type': 'supplemental_document',
                'timestamp': ts_now(),
                'source': os.path.abspath(txt_path),
                'source_name': os.path.basename(txt_path),
                'parser': 'plaintext',
                'content_length': len(content),
                'text': content[:200000],
            }
            with open(fname, 'w', encoding='utf-8') as _fh:
                json.dump(entry, _fh, indent=2)
            saved.append(fname)
            print(f'Imported: {txt_path} -> {fname}')
        for e in errors:
            print('ERROR:', e, file=sys.stderr)
        print(f'\nDone. Imported {len(saved)} file(s).')
        sys.exit(1 if errors else 0)

    if args.training_maintenance and args.training_maintenance_apply:
        print('Use only one of --training-maintenance or --training-maintenance-apply.', file=sys.stderr)
        sys.exit(2)

    if args.training_maintenance or args.training_maintenance_apply:
        maint = os.path.join(BASE_DIR, 'mcp-ai', 'training_maintenance.py')
        if not os.path.exists(maint):
            print('Training maintenance script not found at', maint, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', maint]
        if args.training_maintenance_apply:
            cmd.append('--apply')
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.sync_redhat_docs:
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_redhat_docs.py')
        if not os.path.exists(ing):
            print('Red Hat docs ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing]
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.import_redhat_docs is not None:
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_redhat_docs.py')
        if not os.path.exists(ing):
            print('Red Hat docs ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing]
        if args.import_redhat_docs:
            cmd.extend(args.import_redhat_docs)
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.enrich_companies or args.enrich_from_training:
        enr = os.path.join(BASE_DIR, 'mcp-ai', 'enrich_companies.py')
        if not os.path.exists(enr):
            print('Company enrichment script not found at', enr, file=sys.stderr)
            sys.exit(2)

        cmd = ['/usr/bin/env', 'python3', enr]
        if args.enrich_from_training:
            cmd.extend(['--from-training', '--max-companies', str(max(1, args.max_enrich_companies))])
        if args.enrich_companies:
            cmd.extend(args.enrich_companies)

        proc = subprocess.run(cmd, text=True, capture_output=True)
        out = (proc.stdout or '') + (proc.stderr or '')
        print(out)

        user = os.environ.get('USER') or os.environ.get('LOGNAME') or os.getlogin()
        req = json.dumps({
            'enrich_companies': args.enrich_companies or [],
            'enrich_from_training': bool(args.enrich_from_training),
            'max_enrich_companies': int(args.max_enrich_companies),
        })
        entry_path = write_interaction(user, 'HAL_COMPANY_ENRICHMENT', json.dumps({'request': req, 'result': out}))
        print('\nInteraction recorded ->', entry_path)
        sys.exit(proc.returncode)

    if args.encrypt_training or args.decrypt_training:
        tool = os.path.join(BASE_DIR, 'mcp-ai', 'training_crypto.py')
        if not os.path.exists(tool):
            print('Training crypto script not found at', tool, file=sys.stderr)
            sys.exit(2)
        mode = 'encrypt' if args.encrypt_training else 'decrypt'
        cmd = ['/usr/bin/env', 'python3', tool, mode, '--recursive']
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.export_training_bundle:
        if args.bundle_output_dir:
            os.environ['HAL_TRAIN_BUNDLE_DIR'] = args.bundle_output_dir
        user = os.environ.get('USER') or os.environ.get('LOGNAME') or os.getlogin()
        bundle_resp = generate_training_bundle_export_response('CLI export training bundle', user)
        print('\nHAL response:\n')
        print(bundle_resp)
        # Optional encryption of the zip bundle
        if args.bundle_encrypt:
            # Extract zip path from response (line starting with "Bundle saved to:")
            zip_path_m = re.search(r'Bundle saved to:\s*(.+\.zip)', bundle_resp)
            if zip_path_m:
                zip_path = zip_path_m.group(1).strip()
                if os.path.exists(zip_path):
                    try:
                        import getpass as _gp
                        passphrase = _gp.getpass('Enter encryption passphrase: ')
                        confirm = _gp.getpass('Confirm passphrase: ')
                        if passphrase != confirm:
                            print('Passphrases do not match — bundle NOT encrypted.', file=sys.stderr)
                        else:
                            enc_path = zip_path + '.enc'
                            # Use GPG symmetric if available, else warn
                            gpg = shutil.which('gpg') or shutil.which('gpg2')
                            if gpg:
                                proc_enc = subprocess.run(
                                    [gpg, '--batch', '--yes', '--symmetric', '--cipher-algo', 'AES256',
                                     '--passphrase-fd', '0', '--output', enc_path, zip_path],
                                    input=passphrase, text=True, capture_output=True
                                )
                                if proc_enc.returncode == 0:
                                    os.unlink(zip_path)
                                    print(f'Encrypted bundle: {enc_path}')
                                    print('Decrypt with: gpg --decrypt ' + enc_path + ' > bundle.zip')
                                else:
                                    print('GPG encryption failed:', proc_enc.stderr, file=sys.stderr)
                            else:
                                print('gpg not found — install GnuPG to enable encryption.', file=sys.stderr)
                    except Exception as e:
                        print(f'Encryption error: {e}', file=sys.stderr)
        entry_path = write_interaction(user, 'HAL_EXPORT_TRAINING_BUNDLE', bundle_resp)
        print('\nInteraction recorded ->', entry_path)
        sys.exit(0)

    if args.import_business_intel:
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_business_intel.py')
        if not os.path.exists(ing):
            print('Business intel ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing, *args.import_business_intel]
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.intel_report:
        report = _generate_intel_report_live(args.intel_report, allow_public_enrich=True)
        if report:
            print(report)
            user = os.environ.get('USER') or os.environ.get('LOGNAME') or os.getlogin()
            entry_path = write_interaction(user, f'HAL_INTEL_REPORT:{args.intel_report}', report)
            print(f'\nInteraction recorded -> {entry_path}')
        else:
            print(f'No intel records found for: {args.intel_report}')
            print('Import account data first with: HAL --import-business-intel /path/to/Training_Data/')
        sys.exit(0)

    if args.intel_report_all or args.intel_report_file:
        accounts = []
        if args.intel_report_all:
            accounts.extend(args.intel_report_all)

        if args.intel_report_file:
            try:
                with open(args.intel_report_file, 'r', encoding='utf-8') as fh:
                    for line in fh:
                        val = line.strip()
                        if not val or val.startswith('#'):
                            continue
                        accounts.append(val)
            except Exception as exc:
                print(f'Could not read --intel-report-file: {exc}', file=sys.stderr)
                sys.exit(2)

        # Preserve order, remove dupes.
        ordered_accounts = []
        seen = set()
        for a in accounts:
            key = (a or '').strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            ordered_accounts.append((a or '').strip())

        if not ordered_accounts:
            print('No accounts provided for bulk intel report generation.', file=sys.stderr)
            sys.exit(2)

        ensure_dirs()
        out_dir = os.path.join(REPORTS_DIR, 'intel-reports')
        os.makedirs(out_dir, exist_ok=True)

        print(f'Generating intel reports for {len(ordered_accounts)} account(s)...')
        generated = []
        missing = []
        for account in ordered_accounts:
            report = _generate_intel_report_live(account, allow_public_enrich=True)
            if not report:
                missing.append(account)
                continue
            out_name = f'{_slugify(account)}-{ts_now()}.md'
            out_path = os.path.join(out_dir, out_name)
            with open(out_path, 'w', encoding='utf-8') as fh:
                fh.write(report)
            generated.append((account, out_path))
            print(f'✓ {account} -> {out_path}')

        if missing:
            print('\nCould not generate reports for:')
            for m in missing:
                print(f'  - {m}')

        summary = {
            'requested': ordered_accounts,
            'generated': [{'account': a, 'path': p} for a, p in generated],
            'missing': missing,
            'output_dir': out_dir,
        }
        user = os.environ.get('USER') or os.environ.get('LOGNAME') or os.getlogin()
        entry_path = write_interaction(user, 'HAL_INTEL_REPORT_BULK', json.dumps(summary, indent=2))
        print(f'\nInteraction recorded -> {entry_path}')
        sys.exit(0)

    if args.auto_ingest:
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'auto_ingest_training.py')
        if not os.path.exists(ing):
            print('Auto-ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing, '-v']
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.ingest_status:
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'auto_ingest_training.py')
        if not os.path.exists(ing):
            print('Auto-ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing, '--show-tracker']
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.ingest_reset:
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'auto_ingest_training.py')
        if not os.path.exists(ing):
            print('Auto-ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing, '--track-reset']
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    if args.bridge_check:
        print('\n' + '='*80)
        print('HAL Bridge & Model Diagnostics')
        print('='*80 + '\n')
        
        # Check Ollama
        print('Ollama (port 11434):')
        try:
            import urllib.request
            with urllib.request.urlopen('http://localhost:11434/api/tags', timeout=5) as resp:
                print('  ✓ Ollama responding')
                data = json.loads(resp.read().decode('utf-8'))
                models = data.get('models', [])
                print(f'  ✓ {len(models)} model(s) available:')
                for m in models:
                    name = m['name']
                    size_gb = m['size'] / (1024**3)
                    print(f'    • {name:30} ({size_gb:.1f}GB)')
        except Exception as e:
            print(f'  ✗ Ollama not responding: {e}')
        
        # Check Bridge
        print('\nBridge (port 1776):')
        try:
            with urllib.request.urlopen('http://localhost:1776/health', timeout=5) as resp:
                print('  ✓ Bridge responding')
        except Exception as e:
            print(f'  ✗ Bridge not running: {e}')
            print('    → Start with: bash mcp-ai/start-bridge.sh')
        
        # Check model selection
        print('\nModel Selection:')
        if RUNTIME_FORCE_MODEL:
            print(f'  • Runtime forced model  : {RUNTIME_FORCE_MODEL}')
        elif RUNTIME_FORCE_PROFILE:
            print(f'  • Runtime forced profile: {RUNTIME_FORCE_PROFILE}')

        models = get_available_models()
        if models:
            best = choose_model_for_task(task_profile='general', available=models)
            print(f'  ✓ Default model: {best}')
            for profile in ('codegen', 'strategy', 'business', 'diagnostics'):
                chosen = choose_model_for_task(task_profile=profile, available=models)
                print(f'  ✓ {profile:11}: {chosen}')
        else:
            print('  ✗ No models available')
            print('    → Run: ollama pull qwen2.5-coder:7b')
        
        print('\n' + '='*80 + '\n')
        sys.exit(0)

    # Handle help command
    if args.text and len(args.text) > 0 and args.text[0].lower() == 'help':
        # Extract help topic if provided (e.g., "help import" or just "help")
        topic = args.text[1].lower() if len(args.text) > 1 else None
        show_help(topic)
        sys.exit(0)

    # --list-intents: show all intent routes
    if args.list_intents:
        _show_list_intents()
        sys.exit(0)

    # --explain: show last routing decision
    if args.explain:
        if os.path.exists(_LAST_ROUTE_FILE):
            try:
                with open(_LAST_ROUTE_FILE, 'r', encoding='utf-8') as fh:
                    data = json.load(fh)
                print('\n' + '='*60)
                print('HAL Last Route Decision')
                print('='*60)
                print(f'  Timestamp    : {data.get("timestamp", "?")}')
                print(f'  Intent       : {data.get("intent", "?")}')
                print(f'  Handler      : {data.get("handler", "?")}')
                print(f'  Model        : {data.get("model") or "(auto)"}')
                print(f'  Profile      : {data.get("profile") or "(auto)"}')
                print(f'  Query        : {data.get("query_preview", "?")}')
                print('='*60 + '\n')
            except Exception as e:
                print(f'Could not read last route file: {e}', file=sys.stderr)
        else:
            print('No route decision on record yet. Run a HAL query first.')
        sys.exit(0)

    # --training-report: show training data quality stats
    if args.training_report:
        report = _generate_training_report()
        print(report)
        sys.exit(0)

    # 🆕 COOL: Session statistics dashboard
    if args.session_stats:
        print('\n' + '='*80)
        print('📊 HAL Session Statistics')
        print('='*80)
        total = _SESSION_CONTEXT['total_calls']
        hits = _SESSION_CONTEXT['cache_hits']
        print(f'\nTotal Queries      : {total}')
        print(f'Cache Hits         : {hits} ({100*hits//max(1, total)}%)')
        print(f'Cache Miss         : {total - hits}')
        
        if _SESSION_CONTEXT['models_used']:
            print(f'\nModels Used        :')
            for model, count in sorted(_SESSION_CONTEXT['models_used'].items(), key=lambda x: -x[1]):
                print(f'  • {model:30} {count:3} time(s)')
        
        print(f'\nCache Size         : {len(_RESPONSE_CACHE)} entries')
        print(f'Cache Memory       : ~{len(json.dumps(_RESPONSE_CACHE))//1024}KB')
        print('='*80 + '\n')
        sys.exit(0)

    # 🆕 COOL: Analytics dashboard
    if args.analytics:
        # Load cross-session query history from persisted log
        log_path = os.path.join(AI_HOME, 'hal-query-log.jsonl')
        persisted = []
        try:
            if os.path.isfile(log_path):
                with open(log_path, 'r', encoding='utf-8') as fh:
                    for line in fh:
                        try:
                            persisted.append(json.loads(line.strip()))
                        except Exception:
                            pass
        except Exception:
            pass
        all_queries = persisted or _SESSION_CONTEXT['queries']
        print('\n' + '='*80)
        print('📈 HAL Query Analytics')
        print('='*80)
        if all_queries:
            # Last 20 queries
            recent = all_queries[-20:]
            print(f'\nRecent Queries ({len(all_queries)} total in log):')
            for i, q in enumerate(recent[-10:], start=1):
                query_str = q['query'][:60] + '...' if len(q['query']) > 60 else q['query']
                ts = datetime.fromtimestamp(q['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
                print(f'  {i:2}. [{ts}] {query_str}')
                print(f'       Model: {q.get("model", "unknown")}')
            # Model breakdown
            model_counts: dict = {}
            for q in all_queries:
                m = q.get('model', 'unknown')
                model_counts[m] = model_counts.get(m, 0) + 1
            print(f'\nModels used across {len(all_queries)} queries:')
            for m, c in sorted(model_counts.items(), key=lambda x: -x[1]):
                bar = '█' * min(c, 40)
                print(f'  {m[:30]:30}  {bar} {c}')
        else:
            print('\nNo query history yet. Run `hal "your question"` first.')
        print('='*80 + '\n')
        sys.exit(0)

    # 🆕 COOL: Suggestions engine
    if args.suggestions:
        # Pull recent queries from persisted log for better suggestions
        log_path = os.path.join(AI_HOME, 'hal-query-log.jsonl')
        recent_texts = []
        try:
            if os.path.isfile(log_path):
                with open(log_path, 'r', encoding='utf-8') as fh:
                    lines = fh.readlines()
                for line in lines[-20:]:
                    try:
                        recent_texts.append(json.loads(line.strip()).get('query', ''))
                    except Exception:
                        pass
        except Exception:
            pass
        if not recent_texts:
            recent_texts = [q['query'] for q in _SESSION_CONTEXT['queries'][-3:]]
        sugg = _suggest_next_queries('\n'.join(recent_texts[-3:]))
        print('\n' + '='*80)
        print('💡 Smart Suggestions')
        print('='*80)
        if sugg:
            for i, s in enumerate(sugg, start=1):
                print(f'\n{i}. {s}')
                print(f'   Try: hal "{s}"')
        else:
            # Always surface a few useful starters if the engine has no suggestions
            starters = [
                'how are you today?',
                'what are the current system issues?',
                'show me recommended actions',
                'run a security diagnostic',
            ]
            print('\nSuggested starting points:')
            for i, s in enumerate(starters, start=1):
                print(f'\n{i}. {s}')
                print(f'   Try: hal "{s}"')
        print('='*80 + '\n')
        sys.exit(0)

    # 🆕 COOL: Cache management
    if args.cache_clear:
        _RESPONSE_CACHE.clear()
        print('✓ Response cache cleared')
        sys.exit(0)

    if args.cache_stats:
        print('\n' + '='*80)
        print('💾 Cache Statistics')
        print('='*80)
        print(f'\nCache Entries      : {len(_RESPONSE_CACHE)}')
        print(f'Cache TTL          : {_RESPONSE_CACHE_TTL}s ({_RESPONSE_CACHE_TTL//3600}h)')
        print(f'Memory Usage       : ~{len(json.dumps(_RESPONSE_CACHE))//1024}KB')
        
        if _RESPONSE_CACHE:
            print(f'\nRecent Cache Entries:')
            for i, (h, entry) in enumerate(list(_RESPONSE_CACHE.items())[-5:], start=1):
                query = entry['query'][:50] + '...' if len(entry['query']) > 50 else entry['query']
                age_sec = int(time.time() - entry['cached_at'])
                ttl_remain = max(0, _RESPONSE_CACHE_TTL - age_sec)
                print(f'  {i}. {query}')
                print(f'     Age: {age_sec}s | TTL: {ttl_remain}s remaining')
        print('='*80 + '\n')
        sys.exit(0)

    # --run-tests: eval/regression harness
    if args.run_tests:
        result = _run_eval_suite()
        print(result)
        sys.exit(0)

    if args.run_offline_tests:
        result = _run_offline_eval_suite()
        print(result)
        sys.exit(0)

    if args.setup_training_maintenance:
        setup = os.path.join(BASE_DIR, 'mcp-ai', 'setup_training_maintenance.sh')
        if not os.path.exists(setup):
            print('Training maintenance setup script not found at', setup, file=sys.stderr)
            sys.exit(2)
        proc = subprocess.run(['/usr/bin/env', 'bash', setup])
        sys.exit(proc.returncode)

    # --playbook-run FILE: run an Ansible playbook with --check
    if args.playbook_run:
        pb_path = args.playbook_run
        if not os.path.exists(pb_path):
            print(f'Playbook file not found: {pb_path}', file=sys.stderr)
            sys.exit(2)
        ansible_cmd = shutil.which('ansible-playbook') or 'ansible-playbook'
        print(f'\nRunning: {ansible_cmd} --check {pb_path}\n')
        proc = subprocess.run([ansible_cmd, '--check', pb_path])
        sys.exit(proc.returncode)

    # --inventory: System inventory detection and setup
    if args.inventory:
        inv_script = os.path.join(BASE_DIR, 'hal-inventory.py')
        if not os.path.exists(inv_script):
            print(f'Inventory script not found: {inv_script}', file=sys.stderr)
            sys.exit(1)
        proc = subprocess.run([sys.executable, inv_script])
        sys.exit(proc.returncode)

    # --metrics: Display trending metrics dashboard
    if args.metrics:
        dashboard_script = os.path.join(BASE_DIR, 'hal-dashboard.py')
        if not os.path.exists(dashboard_script):
            print(f'Dashboard script not found: {dashboard_script}', file=sys.stderr)
            sys.exit(1)
        cmd = [sys.executable, dashboard_script, '--days', str(args.metric_days)]
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    # --predict: Show predictive alerts
    if args.predict:
        try:
            from importlib.util import spec_from_file_location, module_from_spec
            metrics_path = os.path.join(BASE_DIR, 'hal-metrics.py')
            spec = spec_from_file_location("hal_metrics", metrics_path)
            hal_metrics = module_from_spec(spec)
            spec.loader.exec_module(hal_metrics)
            
            predictions = hal_metrics.get_all_predictions(days_ahead=7)
            
            print('\n' + '='*70)
            print('PREDICTIVE ALERTS (Next 7 Days)')
            print('='*70 + '\n')
            
            if not predictions:
                print('No active predictions. System is stable.\n')
            else:
                for i, pred in enumerate(predictions, 1):
                    metric = pred['metric_type'].upper()
                    days = pred['days_to_breach']
                    current = pred['current_value']
                    threshold = pred['threshold']
                    severity = pred['severity'].upper()
                    date = pred['projected_date']
                    rate = pred['rate_of_change']
                    
                    print(f'{i}. {metric} Threshold Breach')
                    print(f'   Severity:  {severity}')
                    print(f'   Current:   {current:.1f}% of {threshold:.1f}% threshold')
                    print(f'   Timeframe: {days:.1f} days until {date}')
                    print(f'   Rate:      {rate:.2f}%/day\n')
            
            print('='*70)
            sys.exit(0)
        except Exception as e:
            print(f'Error displaying predictions: {e}', file=sys.stderr)
            sys.exit(1)

    # --studio: launch stocks, image, and video tools
    if args.studio_pipeline:
        studio_script = os.path.join(BASE_DIR, 'hal-studio.py')
        if not os.path.exists(studio_script):
            print(f'Studio script not found: {studio_script}', file=sys.stderr)
            sys.exit(1)
        cmd = [
            sys.executable,
            studio_script,
            'pipeline',
            args.studio_pipeline,
            '--outdir', str(args.studio_outdir),
            '--seconds', str(args.studio_seconds),
            '--bpm', str(args.studio_bpm),
            '--image-backend', str(args.studio_image_backend),
        ]
        if args.studio_voice:
            cmd.extend(['--voice', str(args.studio_voice)])
        proc = subprocess.run(cmd)
        sys.exit(proc.returncode)

    # --studio: launch stocks, image, and video tools
    if args.studio:
        studio_script = os.path.join(BASE_DIR, 'hal-studio.py')
        if not os.path.exists(studio_script):
            print(f'Studio script not found: {studio_script}', file=sys.stderr)
            sys.exit(1)
        proc = subprocess.run([sys.executable, studio_script])
        sys.exit(proc.returncode)

    if not args.text and not args.diagnostics and not args.status and not args.interactive and not args.inventory and not args.metrics and not args.predict and not args.studio and not args.studio_pipeline:
        ap.print_help()
        sys.exit(0)

    user = os.environ.get('USER') or os.environ.get('LOGNAME') or os.getlogin()

    if args.voice_chat:
        _run_voice_chat(user)
        sys.exit(0)

    # --interactive: multi-turn REPL
    if args.interactive:
        _run_interactive_repl(user)
        sys.exit(0)

    # --status: quick one-line health summary (scriptable; exits 0=ok, 1=warn, 2=critical)
    if args.status:
        try:
            import importlib
            import server as _server
            importlib.reload(_server)
            resp_json = _server.full_diagnostics_json()
            data = json.loads(resp_json)
            hw_issues = data.get('hardware', [])
            sec_issues = data.get('security', [])
            all_issues = hw_issues + sec_issues
            crit = sum(1 for i in all_issues if str(i.get('severity', '')).lower() in ('critical', 'high', 'crit'))
            warn = sum(1 for i in all_issues if str(i.get('severity', '')).lower() in ('warning', 'medium', 'warn'))
            info = len(all_issues) - crit - warn
            hostname = os.uname().nodename
            if crit:
                state = 'CRITICAL'
                exit_code = 2
            elif warn:
                state = 'WARNING'
                exit_code = 1
            else:
                state = 'OK'
                exit_code = 0
            top = all_issues[0].get('title', 'No details') if all_issues else 'No issues detected'
            total = len(all_issues)
            print(f'{hostname} [{state}] {total} finding(s): {crit} critical, {warn} warnings, {info} info — {top}')
            sys.exit(exit_code)
        except Exception as e:
            hostname = os.uname().nodename
            print(f'{hostname} [ERROR] Could not run diagnostics: {e}')
            sys.exit(3)

    # Diagnostics path: call server.full_diagnostics_json() locally for structured output
    if args.diagnostics:
        try:
            import importlib
            import server as _server
            importlib.reload(_server)
            resp = _server.full_diagnostics_json()
        except Exception as e:
            resp = f'ERR: failed to run local diagnostics: {e}'

        print('\nHAL diagnostics:\n')
        print(resp)

        entry_path = write_interaction(user, 'FULL_DIAGNOSTICS', resp)
        print('\nInteraction recorded ->', entry_path)

        if args.remediate:
            invoke_remediator(entry_path, args.exec)

        sys.exit(0)

    # Default chat path
    text = ' '.join(args.text) if args.text else None

    # Company/account intel requests should prefer live enrichment + report generation
    # instead of generic supplemental snippets.
    if text and _is_company_intel_query(text):
        extracted_company = _extract_company_name_from_query(text) or _detect_account_in_query(text)
        if extracted_company:
            live_report = _generate_intel_report_live(extracted_company, allow_public_enrich=True)
            if live_report:
                print('\nHAL response:\n')
                print(live_report)
                entry_path = write_interaction(user, text, live_report)
                print(f'\nInteraction recorded -> {entry_path}')
                if args.remediate:
                    invoke_remediator(entry_path, args.exec)
                sys.exit(0)

    # Business command namespace (v1)
    biz_cmd, biz_arg = _parse_business_command(text)
    if biz_cmd == 'account-brief':
        account_target = (biz_arg or args.account or '').strip()
        if not account_target:
            print('Missing account name for business account-brief.')
            print('Usage: HAL business account-brief --account <name>')
            print('   or: HAL business account-brief <name>')
            sys.exit(2)

        brief = generate_business_account_brief(account_target)
        if brief:
            print('\nHAL Business Account Brief:\n')
            print(brief)
            entry_path = write_interaction(user, f'HAL_BUSINESS_ACCOUNT_BRIEF:{account_target}', brief)
            print('\nInteraction recorded ->', entry_path)
        else:
            print(f"No business intel found for: {account_target}")
            print('Import account data first with: HAL --import-business-intel /path/to/Training_Data/')
        sys.exit(0)
    elif biz_cmd == 'help':
        print('\nBusiness commands (v1):')
        print('  HAL business account-brief --account <name>')
        print('  HAL business account-brief <name>')
        sys.exit(0)
    elif biz_cmd == 'unknown':
        print(f"Unknown business command: {biz_arg}")
        print('Run: HAL business help')
        sys.exit(2)

    # Handle help command (check after converting to string)
    if text and text.strip().lower() in ('add training data', 'add training', 'add urls', 'add training urls'):
        prompt = f"Sure {HAL_DISPLAY_NAME}, please paste the url or list of urls to be added (one per line). Finish with an empty line or Ctrl-D:\n"
        print(prompt)
        urls = []
        try:
            # read lines until blank line or EOF
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line == '':
                    break
                urls.append(line)
        except KeyboardInterrupt:
            pass

        if not urls:
            print('No URLs provided; aborting.')
            sys.exit(0)

        ing = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_urls.py')
        if os.path.exists(ing):
            cmd = ['/usr/bin/env', 'python3', ing, '--depth', '3']
            print(f'Ingesting {len(urls)} URL(s) into training data...')
            try:
                proc = _run_subprocess_with_spinner('Ingesting URLs', cmd, input='\n'.join(urls), text=True, capture_output=True)
                out = (proc.stdout or '') + (proc.stderr or '')
                print(out)
            except Exception as e:
                out = f'ERR: {e}'
                print(out)

            entry_path = write_interaction(user, 'HAL_ADD_TRAINING_URLS', json.dumps({'urls': urls, 'result': out}))
            print('\nInteraction recorded ->', entry_path)
        else:
            print('Ingest script not found at', ing)
        sys.exit(0)

    # Special interactive command to import local docs/spreadsheets into training.
    if text and text.strip().lower() in ('import training files', 'import documents', 'add local training data'):
        print(f"Sure {HAL_DISPLAY_NAME}, please provide file or directory paths (one per line). Finish with an empty line or Ctrl-D:\n")
        doc_inputs = []
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line == '':
                    break
                doc_inputs.append(line)
        except KeyboardInterrupt:
            pass

        if not doc_inputs:
            print('No file paths provided; aborting.')
            sys.exit(0)

        ing_docs = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_documents.py')
        if os.path.exists(ing_docs):
            cmd = ['/usr/bin/env', 'python3', ing_docs, '--recursive', *doc_inputs]
            print(f'Importing {len(doc_inputs)} path(s) into training data...')
            try:
                proc = _run_subprocess_with_spinner('Importing training documents', cmd, text=True, capture_output=True)
                out = (proc.stdout or '') + (proc.stderr or '')
                print(out)
            except Exception as e:
                out = f'ERR: {e}'
                print(out)

            entry_path = write_interaction(user, 'HAL_IMPORT_TRAINING_DOCS', json.dumps({'inputs': doc_inputs, 'result': out}))
            print('\nInteraction recorded ->', entry_path)
        else:
            print('Document ingest script not found at', ing_docs)
        sys.exit(0)

    # Special interactive command to import Business_Tools intel data
    if text and text.strip().lower() in ('import intel', 'import business intel', 'import account intel', 'load intel data'):
        print(f"Sure {HAL_DISPLAY_NAME}, please provide the path to your Business_Tools Training_Data directory or JSONL files (one per line). Finish with an empty line:\n")
        intel_inputs = []
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line == '':
                    break
                intel_inputs.append(line)
        except KeyboardInterrupt:
            pass

        if not intel_inputs:
            print('No paths provided; aborting.')
            sys.exit(0)

        ing_intel = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_business_intel.py')
        if os.path.exists(ing_intel):
            cmd = ['/usr/bin/env', 'python3', ing_intel, *intel_inputs]
            print(f'Importing intel from {len(intel_inputs)} path(s)...')
            try:
                proc = _run_subprocess_with_spinner('Importing business intel', cmd, text=True, capture_output=True)
                out = (proc.stdout or '') + (proc.stderr or '')
                print(out)
            except Exception as e:
                out = f'ERR: {e}'
                print(out)
            entry_path = write_interaction(user, 'HAL_IMPORT_BUSINESS_INTEL', json.dumps({'inputs': intel_inputs, 'result': out}))
            print('\nInteraction recorded ->', entry_path)
        else:
            print('Business intel ingest script not found at', ing_intel)
        sys.exit(0)

    # Detect "intel report for <account>" phrasing in the query
    intel_report_match = re.match(r'\s*(?:intel\s+report|account\s+report|account\s+brief|report|brief)\s+(?:for|on|about)?\s+(.+)', text.strip(), re.IGNORECASE)
    if intel_report_match:
        account_query = intel_report_match.group(1).strip()
        report = _generate_intel_report_live(account_query, allow_public_enrich=True)
        if report:
            print(f'\nHAL Intel Report:\n')
            print(report)
            entry_path = write_interaction(user, f'HAL_INTEL_REPORT:{account_query}', report)
            print(f'\nInteraction recorded -> {entry_path}')
            sys.exit(0)
        # No record found — fall through to normal chat to try the LLM

    # Also support account-first report phrasing, e.g.:
    # "centene intel report", "centene report", "centene inteligence report".
    if not _is_strategy_query(text):
        report_keyword_match = bool(re.search(r'\b(report|brief|intel|intelligence|inteligence)\b', text, re.IGNORECASE))
        account_in_text = _detect_account_in_query(text)
        if report_keyword_match and account_in_text:
            report = _generate_intel_report_live(account_in_text, allow_public_enrich=True)
            if report:
                print(f'\nHAL Intel Report:\n')
                print(report)
                entry_path = write_interaction(user, f'HAL_INTEL_REPORT:{account_in_text}', report)
                print(f'\nInteraction recorded -> {entry_path}')
                sys.exit(0)

    # Strategy queries should use the strategy pipeline directly, not generic chat.
    # This avoids terse model greetings like "Hello" for strategy requests.
    if _is_strategy_query(text):
        strategy_account = _detect_account_in_query(text)
        if strategy_account:
            strategy = generate_consulting_strategy(strategy_account, text)
            if strategy:
                print('\nHAL response:\n')
                print(strategy)
                entry_path = write_interaction(user, text, strategy)
                print('\nInteraction recorded ->', entry_path)
                if args.remediate:
                    invoke_remediator(entry_path, args.exec)
                sys.exit(0)

    # Stakeholder/contact queries for known accounts should bypass diagnostics flow.
    if _is_stakeholder_query(text):
        stakeholder_account = _detect_account_in_query(text)
        if stakeholder_account:
            stakeholder_resp = generate_account_stakeholder_response(stakeholder_account)
            if stakeholder_resp:
                print('\nHAL response:\n')
                print(stakeholder_resp)
                entry_path = write_interaction(user, text, stakeholder_resp)
                print('\nInteraction recorded ->', entry_path)
                if args.remediate:
                    invoke_remediator(entry_path, args.exec)
                sys.exit(0)

    # CSV export for customer subscription overview should bypass generic LLM chat.
    if _is_subscription_csv_query(text):
        csv_text, out_path = generate_subscription_csv_report()
        if csv_text and out_path:
            print('\nHAL response (CSV):\n')
            print(csv_text)
            print(f'CSV saved to: {out_path}')
            entry_path = write_interaction(user, text, json.dumps({'csv_path': out_path, 'rows_preview': csv_text[:2000]}))
            print('\nInteraction recorded ->', entry_path)
        else:
            msg = 'No business intel account records found to build subscription CSV. Import intel first with: HAL --import-business-intel /path/to/Training_Data/'
            print('\nHAL response:\n')
            print(msg)
            entry_path = write_interaction(user, text, msg)
            print('\nInteraction recorded ->', entry_path)
        sys.exit(0)

    # Stock price lookups should use deterministic business-intel snapshot responses.
    if _is_stock_price_query(text):
        stock_account = _detect_account_in_query(text)
        if stock_account:
            stock_resp = generate_stock_price_response(stock_account)
            if stock_resp:
                print('\nHAL response:\n')
                print(stock_resp)
                entry_path = write_interaction(user, text, stock_resp)
                print('\nInteraction recorded ->', entry_path)
                sys.exit(0)

    # Server update strategy should bypass generic LLM short replies.
    if _is_server_update_strategy_query(text):
        upd_resp = generate_server_update_strategy_response()
        print('\nHAL response:\n')
        print(upd_resp)
        entry_path = write_interaction(user, text, upd_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Satellite patching prompts should return deterministic runbooks,
    # not short or placeholder model text.
    if _is_satellite_patch_strategy_query(text):
        sat_patch_resp = generate_satellite_patch_strategy_response(text)
        print('\nHAL response:\n')
        print(sat_patch_resp)
        entry_path = write_interaction(user, text, sat_patch_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # AAP patching prompts should return deterministic runbooks.
    if _is_aap_patch_strategy_query(text):
        aap_patch_resp = generate_aap_patch_strategy_response(text)
        print('\nHAL response:\n')
        print(aap_patch_resp)
        entry_path = write_interaction(user, text, aap_patch_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # IdM setup prompts should return deterministic runbooks.
    if _is_idm_setup_query(text):
        idm_resp = generate_idm_setup_response(text)
        print('\nHAL response:\n')
        print(idm_resp)
        entry_path = write_interaction(user, text, idm_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # AAP/Ansible migration strategy prompts should return a deterministic plan,
    # not short generic model replies.
    if _is_aap_migration_strategy_query(text):
        mig_resp = generate_aap_migration_strategy_response(text)
        print('\nHAL response:\n')
        print(mig_resp)
        entry_path = write_interaction(user, text, mig_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Satellite to AAP connection strategy prompts should return a deterministic plan.
    if _is_satellite_aap_connection_strategy_query(text):
        sat_resp = generate_satellite_aap_connection_strategy_response(text)
        print('\nHAL response:\n')
        print(sat_resp)
        entry_path = write_interaction(user, text, sat_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Broad Satellite setup + integration strategy should use an end-to-end runbook.
    if _is_satellite_end_to_end_setup_query(text):
        sat_e2e_resp = generate_satellite_end_to_end_setup_response(text)
        print('\nHAL response:\n')
        print(sat_e2e_resp)
        entry_path = write_interaction(user, text, sat_e2e_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Satellite PXE provisioning strategy prompts should return deterministic guidance.
    if _is_satellite_pxe_strategy_query(text):
        pxe_resp = generate_satellite_pxe_strategy_response(text)
        print('\nHAL response:\n')
        print(pxe_resp)
        entry_path = write_interaction(user, text, pxe_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # MCP server setup in AAP should return deterministic setup guidance.
    if _is_aap_mcp_setup_query(text):
        mcp_resp = generate_aap_mcp_setup_response()
        print('\nHAL response:\n')
        print(mcp_resp)
        entry_path = write_interaction(user, text, mcp_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # MCP setup in Satellite should return deterministic setup guidance.
    if _is_satellite_mcp_setup_query(text):
        sat_mcp_resp = generate_satellite_mcp_setup_response()
        print('\nHAL response:\n')
        print(sat_mcp_resp)
        entry_path = write_interaction(user, text, sat_mcp_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # ── Code generation routing ───────────────────────────────────────────────
    # Git helper subcommands: HAL git log | commit-msg | pr-desc | diff | status
    if _is_git_helper_query(text):
        git_resp = _handle_git_helper(text)
        _log_route_decision('git-helper', '_handle_git_helper', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(git_resp)
        entry_path = write_interaction(user, text, git_resp)
        print('\nInteraction recorded ->', entry_path)
        sys.exit(0)

    # Training-data export bundle (zip + README + deploy helper script)
    if _is_training_bundle_export_query(text):
        bundle_resp = generate_training_bundle_export_response(text, user)
        _log_route_decision('training-bundle', 'generate_training_bundle_export_response', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(bundle_resp)
        entry_path = write_interaction(user, text, bundle_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Natural-language alias: import training data from URL(s)
    if _is_training_import_url_execute_query(text):
        urls = _extract_import_urls_from_query(text)
        depth = _extract_import_depth_from_query(text, default_depth=3)
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_urls.py')
        if not os.path.exists(ing):
            print('URL ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing, '--depth', str(depth), '--same-host-only', *urls]
        try:
            proc = _run_subprocess_with_spinner('Importing training URLs', cmd, text=True, capture_output=True)
            imp_resp = (proc.stdout or '') + (proc.stderr or '')
        except Exception as e:
            imp_resp = f'ERR: {e}'
        _log_route_decision('training-import-url-exec', 'ingest_urls.py', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(imp_resp)
        entry_path = write_interaction(user, text, imp_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Natural-language alias: import training data from txt file(s)
    if _is_training_import_txt_execute_query(text):
        import hashlib as _hashlib
        ensure_dirs()
        paths = _extract_import_txt_paths_from_query(text)
        saved = []
        errors = []
        for txt_path in paths:
            if not os.path.isfile(txt_path):
                errors.append(f'File not found: {txt_path}')
                continue
            try:
                with open(txt_path, 'r', encoding='utf-8', errors='replace') as _fh:
                    content = _fh.read()
            except Exception as _exc:
                errors.append(f'Cannot read {txt_path}: {_exc}')
                continue
            _h = _hashlib.sha256(os.path.abspath(txt_path).encode()).hexdigest()[:12]
            fname = os.path.join(TRAIN_DIR, f'txt-{_h}.json')
            entry = {
                'type': 'supplemental_document',
                'timestamp': ts_now(),
                'source': os.path.abspath(txt_path),
                'source_name': os.path.basename(txt_path),
                'parser': 'plaintext',
                'content_length': len(content),
                'text': content[:200000],
            }
            with open(fname, 'w', encoding='utf-8') as _fh:
                json.dump(entry, _fh, indent=2)
            saved.append(fname)
        lines = []
        for f in saved:
            lines.append(f'Imported: {f}')
        for e in errors:
            lines.append(f'ERROR: {e}')
        lines.append(f'Done. Imported {len(saved)} file(s).')
        imp_txt_resp = '\n'.join(lines)
        _log_route_decision('training-import-txt-exec', 'inline_txt_import', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(imp_txt_resp)
        entry_path = write_interaction(user, text, imp_txt_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Training data import guidance (URL/TXT) should be deterministic.
    if _is_training_data_import_query(text):
        imp_resp = generate_training_import_help_response()
        _log_route_decision('training-import-help', 'generate_training_import_help_response', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(imp_resp)
        entry_path = write_interaction(user, text, imp_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Curated Red Hat product documentation ingestion
    if _is_redhat_docs_ingest_query(text):
        docsets = _extract_redhat_docsets_from_query(text)
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_redhat_docs.py')
        if not os.path.exists(ing):
            print('Red Hat docs ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing, *docsets]
        try:
            proc = _run_subprocess_with_spinner('Ingesting Red Hat docs', cmd, text=True, capture_output=True)
            docs_resp = (proc.stdout or '') + (proc.stderr or '')
        except Exception as e:
            docs_resp = f'ERR: {e}'
        _log_route_decision('redhat-docs-ingest', 'ingest_redhat_docs.py', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(docs_resp)
        entry_path = write_interaction(user, text, docs_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Curated Red Hat documentation sync (all default docsets)
    if _is_redhat_docs_sync_query(text):
        ing = os.path.join(BASE_DIR, 'mcp-ai', 'ingest_redhat_docs.py')
        if not os.path.exists(ing):
            print('Red Hat docs ingest script not found at', ing, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', ing]
        try:
            proc = _run_subprocess_with_spinner('Syncing Red Hat docs', cmd, text=True, capture_output=True)
            docs_resp = (proc.stdout or '') + (proc.stderr or '')
        except Exception as e:
            docs_resp = f'ERR: {e}'
        _log_route_decision('redhat-docs-sync', 'ingest_redhat_docs.py', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(docs_resp)
        entry_path = write_interaction(user, text, docs_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Training data optimization/maintenance
    if _is_training_maintenance_query(text):
        maint = os.path.join(BASE_DIR, 'mcp-ai', 'training_maintenance.py')
        if not os.path.exists(maint):
            print('Training maintenance script not found at', maint, file=sys.stderr)
            sys.exit(2)
        cmd = ['/usr/bin/env', 'python3', maint]
        try:
            proc = _run_subprocess_with_spinner('Running training maintenance', cmd, text=True, capture_output=True)
            maint_resp = (proc.stdout or '') + (proc.stderr or '')
        except Exception as e:
            maint_resp = f'ERR: {e}'
        _log_route_decision('training-maintenance', 'training_maintenance.py', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(maint_resp)
        entry_path = write_interaction(user, text, maint_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Dependency / import advisor — checked first (most specific)
    if _is_dependency_advisor_query(text):
        dep_resp = generate_dependency_advice(text)
        _log_route_decision('dependency-advisor', 'generate_dependency_advice', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(dep_resp)
        entry_path = write_interaction(user, text, dep_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Ansible Event-Driven Ansible use cases / examples
    if _is_ansible_eda_use_case_query(text):
        eda_resp = generate_ansible_eda_use_cases_response()
        _log_route_decision('ansible-eda-use-cases', 'generate_ansible_eda_use_cases_response', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(eda_resp)
        entry_path = write_interaction(user, text, eda_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Ansible playbook / role / collection / task generation
    if _is_ansible_codegen_query(text):
        ansible_resp = generate_ansible_codegen_response(text)
        _log_route_decision('ansible-codegen', 'generate_ansible_codegen_response', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        # Ansible-lint check on generated YAML
        yaml_m = re.search(r'```(?:yaml|yml)?\n(.*?)```', ansible_resp, re.DOTALL)
        if yaml_m:
            lint_out = _ansible_lint_check(yaml_m.group(1))
            if lint_out:
                ansible_resp += f'\n\n── ansible-lint ─────────────────────\n{lint_out}\n'
        print('\nHAL response:\n')
        print(ansible_resp)
        entry_path = write_interaction(user, text, ansible_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Jinja2 template generation
    if _is_jinja2_codegen_query(text):
        j2_resp = generate_jinja2_template_response(text)
        _log_route_decision('jinja2-codegen', 'generate_jinja2_template_response', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(j2_resp)
        entry_path = write_interaction(user, text, j2_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # Python script generation
    if _is_python_codegen_query(text):
        py_resp = generate_python_script_response(text)
        _log_route_decision('python-codegen', 'generate_python_script_response', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
        print('\nHAL response:\n')
        print(py_resp)
        entry_path = write_interaction(user, text, py_resp)
        print('\nInteraction recorded ->', entry_path)
        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)
    # ── End code generation routing ───────────────────────────────────────────

    print(f"HAL request: {text}")

    # Short-circuit trivial greetings so HAL replies with a concise human greeting
    # rather than a model-generated disclaimer. This returns the exact phrasing
    # requested by policy: "Hello Dave, How can I help you today" (name is
    # configurable via HAL_DISPLAY_NAME).
    try:
        if _is_greeting(text):
            if CONVERSATIONAL:
                variants = [
                    f"Hi {HAL_DISPLAY_NAME}! I'm doing well — thanks for asking. What can I do for you today?",
                    f"Hello {HAL_DISPLAY_NAME}, nice to hear from you. How can I help?",
                    f"Hey {HAL_DISPLAY_NAME}! I'm here and ready. What would you like to do?"
                ]
                greeting = random.choice(variants)
            else:
                greeting = f"Hello {HAL_DISPLAY_NAME}, How can I help you today"

            print('\nHAL response:\n')
            print(greeting)
            entry_path = write_interaction(user, text, greeting)
            print('\nInteraction recorded ->', entry_path)
            if args.remediate:
                invoke_remediator(entry_path, args.exec)
            sys.exit(0)
    except Exception:
        pass

    # If this is a wellbeing question, run local diagnostics first and reply accordingly
    try:
        if _is_well_query(text):
            # Conversational mode: respond with a friendly acknowledgement and offer a health check
            if CONVERSATIONAL:
                friendly_variants = [
                    f"Thanks for asking, {HAL_DISPLAY_NAME}! I'm operating normally. Would you like me to run a quick system health check? (y/N)",
                    f"I appreciate you checking in, {HAL_DISPLAY_NAME}. I'm up and ready — should I run a status check now? (y/N)",
                    f"Doing fine here, {HAL_DISPLAY_NAME}. If you'd like, I can run a health check and report back. Run it now? (y/N)"
                ]
                reply = random.choice(friendly_variants)
                print('\nHAL response:\n')
                print(reply)
                entry_path = write_interaction(user, text, reply)
                print('\nInteraction recorded ->', entry_path)

                # Interactive confirmation to run diagnostics
                try:
                    if sys.stdin and sys.stdin.isatty():
                        ans = input('\nRun system health check now? [y/N] ').strip().lower()
                    else:
                        ans = 'n'
                except Exception:
                    ans = 'n'

                if ans.startswith('y'):
                    # perform the same diagnostics+LLM report flow as before
                    try:
                        full_diag = _run_with_spinner(
                            'Running local diagnostics',
                            _exec_local_tool,
                            'architect.full_diagnostics_json',
                            {}
                        )
                    except Exception:
                        full_diag = 'ERR: failed to run diagnostics'

                    if isinstance(full_diag, str) and full_diag.startswith('ERR:'):
                        # fallback to LLM if diagnostics failed
                        resp = call_bridge(text)
                        assistant_text = extract_assistant_content(resp)
                        print('\nHAL response:\n')
                        print(assistant_text if assistant_text else resp)
                        entry_path = write_interaction(user, text, resp)
                        print('\nInteraction recorded ->', entry_path)
                        if args.remediate:
                            invoke_remediator(entry_path, args.exec)
                        sys.exit(0)

                    try:
                        diag_json = json.loads(full_diag)
                    except Exception:
                        diag_json = None

                    hw = diag_json.get('hardware', []) if isinstance(diag_json, dict) else []
                    sec = diag_json.get('security', []) if isinstance(diag_json, dict) else []

                    if not hw and not sec:
                        well = os.environ.get('HAL_WELL_PHRASE', WELL_PHRASE)
                        print('\nHAL response:\n')
                        print(well)
                        entry_path = write_interaction(user, text, well)
                        print('\nInteraction recorded ->', entry_path)
                        if args.remediate:
                            invoke_remediator(entry_path, args.exec)
                        sys.exit(0)

                    print('\nHAL final report:\n')
                    print(_render_health_summary_menu(diag_json))

                    mode = _choose_health_output_mode()
                    llm_raw = ''
                    if mode == '2':
                        final_text = _render_health_actions(diag_json)
                    elif mode == '3':
                        final_text = f"Local diagnostics (raw):\n{json.dumps(diag_json, indent=2)}"
                    elif mode == '4':
                        followup = (
                            f"User asked: {text}\n"
                            f"Local diagnostics (JSON):\n{json.dumps(diag_json, indent=2)[:4000]}\n\n"
                            "Return only a short, plain-text summary (4-8 lines) with the most important findings and immediate actions. "
                            "Do not include JSON."
                        )
                        llm_raw = _run_with_spinner('Generating AI summary', call_bridge, followup)
                        final_text = extract_assistant_content(llm_raw) or _render_health_actions(diag_json)
                    elif mode == '5':
                        final_text = _render_health_actions(diag_json)
                    else:
                        final_text = _render_health_summary_menu(diag_json)

                    if mode != '1':
                        print('\nSelected output:\n')
                        print(final_text)

                    combined = json.dumps({'llm_response_raw': llm_raw, 'diagnostics': diag_json, 'final_report': final_text, 'mode': mode}, indent=2)
                    entry_path = write_interaction(user, text, combined)
                    print('\nInteraction recorded ->', entry_path)

                    if mode == '5':
                        print('\nFix all selected. Starting full auto-remediation...')
                        remediation_mode = '3'
                    else:
                        remediation_mode = _choose_health_remediation_mode()
                        if args.remediate and args.exec:
                            remediation_mode = '3'
                        elif args.remediate:
                            remediation_mode = '2'
                    _execute_health_remediation(text, diag_json, entry_path, remediation_mode)

                    sys.exit(0)
                else:
                    # user declined; provide guidance to request diagnostics later
                    print('\nIf you want a full system check later, run: hal --diagnostics')
                    sys.exit(0)
            else:
                # non-conversational (legacy) behavior: run diagnostics and ask LLM for a report
                try:
                    full_diag = _run_with_spinner(
                        'Running local diagnostics',
                        _exec_local_tool,
                        'architect.full_diagnostics_json',
                        {}
                    )
                except Exception:
                    full_diag = 'ERR: failed to run diagnostics'

                if isinstance(full_diag, str) and full_diag.startswith('ERR:'):
                    resp = call_bridge(text)
                    assistant_text = extract_assistant_content(resp)
                    print('\nHAL response:\n')
                    print(assistant_text if assistant_text else resp)
                    entry_path = write_interaction(user, text, resp)
                    print('\nInteraction recorded ->', entry_path)
                    if args.remediate:
                        invoke_remediator(entry_path, args.exec)
                    sys.exit(0)

                try:
                    diag_json = json.loads(full_diag)
                except Exception:
                    diag_json = None

                hw = diag_json.get('hardware', []) if isinstance(diag_json, dict) else []
                sec = diag_json.get('security', []) if isinstance(diag_json, dict) else []

                if not hw and not sec:
                    well = os.environ.get('HAL_WELL_PHRASE', WELL_PHRASE)
                    print('\nHAL response:\n')
                    print(well)
                    entry_path = write_interaction(user, text, well)
                    print('\nInteraction recorded ->', entry_path)
                    if args.remediate:
                        invoke_remediator(entry_path, args.exec)
                    sys.exit(0)

                print('\nHAL final report:\n')
                print(_render_health_summary_menu(diag_json))

                mode = _choose_health_output_mode()
                llm_raw = ''
                if mode == '2':
                    final_text = _render_health_actions(diag_json)
                elif mode == '3':
                    final_text = f"Local diagnostics (raw):\n{json.dumps(diag_json, indent=2)}"
                elif mode == '4':
                    followup = (
                        f"User asked: {text}\n"
                        f"Local diagnostics (JSON):\n{json.dumps(diag_json, indent=2)[:4000]}\n\n"
                        "Return only a short, plain-text summary (4-8 lines) with the most important findings and immediate actions. "
                        "Do not include JSON."
                    )
                    llm_raw = _run_with_spinner('Generating AI summary', call_bridge, followup)
                    final_text = extract_assistant_content(llm_raw) or _render_health_actions(diag_json)
                elif mode == '5':
                    final_text = _render_health_actions(diag_json)
                else:
                    final_text = _render_health_summary_menu(diag_json)

                if mode != '1':
                    print('\nSelected output:\n')
                    print(final_text)

                combined = json.dumps({'llm_response_raw': llm_raw, 'diagnostics': diag_json, 'final_report': final_text, 'mode': mode}, indent=2)
                entry_path = write_interaction(user, text, combined)
                print('\nInteraction recorded ->', entry_path)

                if mode == '5':
                    print('\nFix all selected. Starting full auto-remediation...')
                    remediation_mode = '3'
                else:
                    remediation_mode = _choose_health_remediation_mode()
                    if args.remediate and args.exec:
                        remediation_mode = '3'
                    elif args.remediate:
                        remediation_mode = '2'
                _execute_health_remediation(text, diag_json, entry_path, remediation_mode)

                sys.exit(0)
    except Exception:
        pass

    # RAG: search training data and inject as context into the LLM prompt
    rag_context = search_training_data_for_rag(text)

    resp = call_bridge(text, rag_context=rag_context)
    assistant_text = extract_assistant_content(resp)

    # Global quality gate for operational asks: retry once with a strict prompt,
    # then deterministic fallback if output is still low quality.
    quality_retried = False
    fallback_used = False
    rag_confidence = _estimate_rag_confidence(text, rag_context)
    if _is_operational_howto_query(text) and _is_low_quality_assistant_text(assistant_text or ''):
        quality_retried = True
        strict_prompt = (
            f"User request: {text}\n\n"
            "Provide a practical runbook with exactly these sections: Preconditions, Steps, Validation, Rollback, Next Actions. "
            "Do not answer with a greeting or one-word placeholder."
        )
        retry_resp = call_bridge(strict_prompt, rag_context=rag_context)
        retry_text = extract_assistant_content(retry_resp)
        if retry_text and not _is_low_quality_assistant_text(retry_text):
            resp = retry_resp
            assistant_text = retry_text
        else:
            fallback_used = True
            # Prefer doc-grounded summary over generic runbook when local docs match.
            if rag_context and _preferred_doc_source_patterns(text):
                doc_summary = _build_offline_doc_summary(text, rag_context)
                assistant_text = doc_summary if doc_summary else _build_generic_operational_runbook(text, rag_context)
            else:
                assistant_text = _build_generic_operational_runbook(text, rag_context)
            resp = json.dumps({'fallback': 'doc_summary' if (rag_context and _preferred_doc_source_patterns(text)) else 'generic_operational_runbook', 'query': text}, indent=2)

    # Add concise grounding metadata for operational answers.
    if _is_operational_howto_query(text):
        sources = _extract_rag_sources(rag_context)
        grounding = ["", "Grounding:", f"- Confidence: {rag_confidence}"]
        if sources:
            grounding.append(f"- Sources: {', '.join(sources[:5])}")
        else:
            grounding.append('- Sources: none matched in local knowledge base')
        if assistant_text and 'Grounding:' not in assistant_text:
            assistant_text = assistant_text.rstrip() + '\n' + '\n'.join(grounding)

    print('\nHAL response:\n')
    print(assistant_text if assistant_text else resp)
    speak_if_enabled(assistant_text if assistant_text else str(resp))

    # Attempt to detect a tool invocation from the LLM response
    tool_name, tool_args = (None, None) if fallback_used else _parse_tool_call(resp)

    # If the assistant returned only a brief greeting but the user's query
    # clearly asks about problems/fixes, run local diagnostics automatically
    # and ask the model to produce a human-readable report.
    try:
        keywords_match = bool(re.search(r"\b(problem|problems|fix|fixes|issue|issues|error|errors|fail|failed|disk|remed|remediation)\b", text or '', re.IGNORECASE))
        # Treat non-trivial questions as requests for a report (e.g. "what needs fixing", "how is security...")
        question_match = bool(re.search(r"\b(what|how|do|does|is|are|should|could|would|did|where|when|why)\b", text or '', re.IGNORECASE) or ('?' in (text or '')))
        user_word_count = len((text or '').split())
        user_asks_question = question_match and user_word_count >= 2
        user_needs_report = keywords_match or user_asks_question
    except Exception:
        user_needs_report = False

    assistant_is_greeting = False
    if assistant_text:
        at = assistant_text.strip().lower()
        if _is_greeting(at) or len(at.split()) < 10 or at.startswith('hello') or 'how can i' in at:
            assistant_is_greeting = True

    resp_is_err = isinstance(resp, str) and resp.startswith('ERR:')

    # ── OFFLINE FALLBACK ─────────────────────────────────────────────────────
    # When the bridge is unavailable, surface the best available local answer.
    # Priority: 1) structured intel report for known accounts
    #           2) training data RAG context (already searched above)
    #           3) raw training-data search results
    if resp_is_err:
        # Strategy-style requests should return a strategy, not a raw intel brief.
        if _is_strategy_query(text):
            detected_account = _detect_account_in_query(text)
            if detected_account:
                strategy = generate_consulting_strategy(detected_account, text)
                if strategy:
                    print('\nHAL response (offline — from knowledge base):\n')
                    print(strategy)
                    entry_path = write_interaction(user, text, strategy)
                    print('\nInteraction recorded ->', entry_path)
                    if args.remediate:
                        invoke_remediator(entry_path, args.exec)
                    sys.exit(0)

        # 1. Known-account/company intel report with public-web refresh
        detected_account = _detect_account_in_query(text) or _extract_company_name_from_query(text)
        if detected_account:
            report = _generate_intel_report_live(detected_account, allow_public_enrich=True)
            if report:
                print('\nHAL response (offline — from knowledge base):\n')
                print(report)
                entry_path = write_interaction(user, text, report)
                print(f'\nInteraction recorded -> {entry_path}')
                if args.remediate:
                    invoke_remediator(entry_path, args.exec)
                sys.exit(0)

        # 2. If we already have RAG context from the search above, display it cleanly
        if rag_context:
            summary = _build_offline_doc_summary(text, rag_context)
            if summary and (_is_operational_howto_query(text) or bool(_preferred_doc_source_patterns(text))):
                print('\nHAL response (offline — from knowledge base):\n')
                print(summary)
                entry_path = write_interaction(user, text, summary)
                print('\nInteraction recorded ->', entry_path)
                if args.remediate:
                    invoke_remediator(entry_path, args.exec)
                sys.exit(0)

            # Strip the internal header and render cleanly; skip empty render.
            display = rag_context.replace('[Relevant context from your personal knowledge base:]', '').strip()
            if display:
                print('\nHAL response (offline — from knowledge base):\n')
                print(display)
                entry_path = write_interaction(user, text, display)
                print('\nInteraction recorded ->', entry_path)
                if args.remediate:
                    invoke_remediator(entry_path, args.exec)
                sys.exit(0)

        # 3. Fall back to keyword-matched training-data search
        training_results = search_training_data(text)
        if training_results:
            print('\nHAL response (offline — from knowledge base):\n')
            print(training_results)
            entry_path = write_interaction(user, text, training_results)
            print('\nInteraction recorded ->', entry_path)
            if args.remediate:
                invoke_remediator(entry_path, args.exec)
            sys.exit(0)

        # 4. Nothing found in training data at all — give an honest message
        print('\nHAL response (offline):\n')
        print('Bridge is offline and I didn\'t find relevant information in your local knowledge base for that query.')
        print('Start the bridge with: bash mcp-ai/start-bridge.sh')
        entry_path = write_interaction(user, text, 'Bridge offline, no local data found.')
        print('\nInteraction recorded ->', entry_path)
        sys.exit(0)
    # ── END OFFLINE FALLBACK ──────────────────────────────────────────────────

    if not fallback_used and not tool_name and user_needs_report and (assistant_is_greeting or not assistant_text):
        # If bridge returned a short/greeting reply, fetch diagnostics for a better report
        # (training context already injected via RAG)
        if CONVERSATIONAL:
            follow_msg = random.choice([
                "That was a short reply — I'll gather more details and prepare a clear summary for you.",
                "I'll pull a fuller report now so you get a concise, user-friendly list of problems and fixes.",
                "Let me fetch system details and turn them into a short, actionable report."
            ])
        else:
            follow_msg = 'Assistant reply was brief; running local diagnostics and requesting a report...'
        print('\n' + follow_msg)
        # run local diagnostics
        try:
            full_diag = _exec_local_tool('architect.full_diagnostics_json', {})
            if full_diag and full_diag.startswith('ERR:'):
                diag_snippet = ''
            else:
                diag_snippet = (full_diag or '')[:4000]
        except Exception:
            diag_snippet = ''

        followup = (
            f"User asked: {text}\n"
            f"The assistant responded briefly: {assistant_text}\n"
            f"Local diagnostics (truncated):\n{diag_snippet}\n\n"
            "Please produce TWO outputs: (1) a short plain-text summary (2-6 sentences) listing detected problems and immediate remediations; "
            "and (2) a JSON object with keys `problems` (array) and `remediations` (array). Return the plain-text first, then the JSON on its own."
        )

        final = call_bridge(followup)
        final_text = extract_assistant_content(final)

        # If the bridge/LLM did not produce a usable final text, fall back
        # to presenting the raw local diagnostics so the user still gets a report.
        if not final_text or (isinstance(final_text, str) and final_text.strip() == '') or (isinstance(final_text, str) and final_text.lower().startswith('err:')):
            final_text = f"Local diagnostics (raw):\n{diag_snippet or full_diag or 'No diagnostics available'}"

        print('\nHAL final report:\n')
        print(final_text)
        speak_if_enabled(final_text)

        combined = json.dumps({'llm_response_raw': resp, 'assistant_text': assistant_text, 'diagnostics': diag_snippet, 'final_report': final_text}, indent=2)
        entry_path = write_interaction(user, text, combined)
        print('\nInteraction recorded ->', entry_path)

        if args.remediate:
            invoke_remediator(entry_path, args.exec)

        sys.exit(0)
    if tool_name:
        print('\nLLM requested tool call ->', tool_name)
        tool_out = _exec_local_tool(tool_name, tool_args)
        print('\nTool result:\n')
        print(tool_out)

        # Also fetch full local diagnostics to provide richer context to the model
        try:
            full_diag = _exec_local_tool('architect.full_diagnostics_json', {})
            if full_diag and full_diag.startswith('ERR:'):
                full_diag = ''
        except Exception:
            full_diag = ''

        # Truncate diagnostics to keep prompt size reasonable
        diag_snippet = (full_diag or '')[:4000]

        # Ask the model to convert the tool output + diagnostics into a user-friendly report
        followup = (
            f"User asked: {text}\n"
            f"The model requested the tool `{tool_name}` which returned:\n{tool_out}\n\n"
            f"Full local diagnostics (truncated):\n{diag_snippet}\n\n"
            "Please produce TWO outputs: (1) a short plain-text summary for the user (2-6 sentences) listing detected problems and immediate remediations; "
            "and (2) a JSON object with keys `problems` (array) and `remediations` (array). Return the plain-text first, then the JSON on its own."
        )
        final = call_bridge(followup)
        final_text = extract_assistant_content(final)
        print('\nHAL final report:\n')
        print(final_text if final_text else final)
        speak_if_enabled(final_text if final_text else str(final))

        # Record combined interaction (raw LLM, tool output, diagnostics, final summary)
        combined = json.dumps({'llm_response_raw': resp, 'tool_invoked': tool_name, 'tool_arguments': tool_args, 'tool_result': tool_out, 'diagnostics': diag_snippet, 'final_report': final}, indent=2)
        entry_path = write_interaction(user, text, combined)
        print('\nInteraction recorded ->', entry_path)

        if args.remediate:
            invoke_remediator(entry_path, args.exec)
        sys.exit(0)

    # No tool call: record the raw response
    _log_route_decision('llm-bridge', 'call_bridge', RUNTIME_FORCE_MODEL, RUNTIME_FORCE_PROFILE, text)
    _log_quality_event(user, text, quality_retried, fallback_used, rag_confidence)
    entry_path = write_interaction(user, text, resp)
    print('\nInteraction recorded ->', entry_path)

    if args.remediate:
        invoke_remediator(entry_path, args.exec)

if __name__ == '__main__':
    main()
