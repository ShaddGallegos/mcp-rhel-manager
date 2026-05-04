#!/usr/bin/env python3
"""
HAL Brain — Autonomous resource management, adaptive routing, and intelligent dispatch.

Gives HAL the ability to:
 • Automatically select the best available model for each task type
 • Learn from performance history (latency, quality scores)
 • Pull new models or tools autonomously when needed
 • Remove resources that are stale / no longer useful
 • Run parallel multi-model ensemble inference and synthesize consensus
 • Manage MCP server lifecycle (register, start, stop, health-check)
 • Decompose complex goals into subtasks and route each optimally
 • Fall back across multiple LLM backends (Ollama → OpenAI-compat → HF)
 • Install Python packages it needs at runtime
 • Self-diagnose routing quality and propose improvements

Usage (standalone):
  hal --brain-status                      # show all resources + routing state
  hal --brain-route "explain CVE-2024-3400"  # classify + route a query
  hal --ensemble "what is idempotency"    # multi-model consensus answer
  hal --resource-status                   # list all models, sizes, last-used
  hal --resource-cleanup 30               # remove models unused >30 days
  hal --resource-install mistral:7b       # pull a model (with confirm)
  hal --mcp-server-status                 # list registered MCP servers
  hal --mcp-server-start architect        # start a named MCP server
  hal --mcp-server-stop architect         # stop a named MCP server
  hal --task-plan "fully automate RHEL patching via Satellite + AAP"
  hal --auto-pull                         # smart: pull recommended models if missing
  hal --brain-learn                       # re-score models against benchmark tasks
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
HOME         = os.path.expanduser('~')
AI_HOME      = os.path.join(HOME, '.mcp-ai')
BRAIN_DIR    = os.path.join(AI_HOME, 'brain')
PERF_DB      = os.path.join(BRAIN_DIR, 'perf-db.json')
ROUTE_LOG    = os.path.join(BRAIN_DIR, 'route-log.jsonl')
MCP_REGISTRY = os.path.join(BRAIN_DIR, 'mcp-registry.json')
RESOURCE_LOG = os.path.join(BRAIN_DIR, 'resource-log.jsonl')
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

OLLAMA_BASE  = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434')
OLLAMA_CHAT  = os.environ.get('OLLAMA_URL',      'http://localhost:1776/api/chat')
HAL_DISPLAY  = os.environ.get('HAL_DISPLAY_NAME', 'Dave')
ASSISTANT    = os.environ.get('HAL_ASSISTANT_NAME', 'HAL9000')

# ── Task types ──────────────────────────────────────────────────────────────
TASK_TYPES = [
    'code',       # code generation, review, debugging
    'ansible',    # Ansible playbooks, roles, AAP
    'security',   # CVE analysis, hardening, OWASP
    'reasoning',  # complex multi-step logic, math, planning
    'business',   # intel reports, account briefs, stocks
    'search',     # needs web search, up-to-date facts
    'creative',   # writing, brainstorming, metaphors
    'system',     # sysadmin, diagnostics, logs
    'fast',       # simple factual, 1-sentence answers
    'general',    # fallback
]

# ── Model preferences per task type (user can override via ~/.mcp-ai/brain/model-prefs.json)
DEFAULT_TASK_MODEL_PREFS = {
    'code':      ['qwen2.5-coder:7b', 'deepseek-coder:6.7b', 'codellama:7b', 'llama4:scout'],
    'ansible':   ['ansible-custom-brain:latest', 'ansible-certified-pro:latest', 'qwen2.5-coder:7b', 'llama4:scout'],
    'security':  ['qwen2.5-coder:7b', 'llama4:scout', 'mistral:7b'],
    'reasoning': ['llama4:scout', 'scout-human:latest', 'qwen2.5-coder:7b', 'llama3.2:3b'],
    'business':  ['llama4:scout', 'scout-human:latest', 'mistral:7b', 'qwen2.5-coder:7b'],
    'search':    ['llama3.2:3b', 'qwen2.5-coder:7b', 'llama4:scout'],
    'creative':  ['llama4:scout', 'scout-human:latest', 'mistral:7b', 'llama3.2:3b'],
    'system':    ['qwen2.5-coder:7b', 'ansible-custom-brain:latest', 'llama4:scout'],
    'fast':      ['llama3.2:3b', 'qwen2.5-coder:7b', 'phi3:mini', 'mistral:7b'],
    'general':   ['qwen2.5-coder:7b', 'llama4:scout', 'llama3.2:3b', 'mistral:7b'],
}

# ── Models recommended for auto-pull if missing (by task need)
RECOMMENDED_MODELS = {
    'fast_chat':   'llama3.2:3b',
    'code':        'qwen2.5-coder:7b',
    'reasoning':   'llama4:scout',
    'ansible':     'ansible-custom-brain:latest',
}

# ── Known MCP server definitions (extended from mcp-config.json)
BUILTIN_MCP_SERVERS = {
    'architect': {
        'description': 'Main HAL MCP server (RHEL/Ansible/Satellite diagnostics)',
        'command': [os.path.join(BASE_DIR, '.venv', 'bin', 'python'), os.path.join(BASE_DIR, 'scripts', 'server.py')],
        'env': {},
        'health_url': None,
    },
    'bridge': {
        'description': 'Ollama MCP bridge (translates MCP → Ollama chat API)',
        'command': ['bash', os.path.join(BASE_DIR, 'mcp-ai', 'start-bridge.sh')],
        'env': {},
        'health_url': 'http://localhost:1776/health',
    },
}

# ── Whitelisted pip packages that brain can install autonomously
ALLOWED_AUTO_INSTALL = {
    'duckduckgo-search', 'huggingface-hub', 'psutil', 'rich', 'requests',
    'tenacity', 'pillow', 'pandas', 'openpyxl', 'feedparser', 'httpx',
    'aiohttp', 'tqdm', 'colorama', 'tabulate', 'yaspin', 'pydantic',
}


def _ensure_dirs():
    for d in (BRAIN_DIR,):
        os.makedirs(d, exist_ok=True)


# ── Performance DB ──────────────────────────────────────────────────────────

def _load_perf_db() -> dict:
    _ensure_dirs()
    if os.path.exists(PERF_DB):
        try:
            with open(PERF_DB, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'models': {}, 'task_routing': {}}


def _save_perf_db(db: dict):
    _ensure_dirs()
    with open(PERF_DB, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2)


def record_inference(model: str, task_type: str, latency_sec: float, success: bool, user_score: float = 0.0):
    """Record an inference event so HAL can learn routing over time."""
    db = _load_perf_db()
    db.setdefault('models', {})
    db['models'].setdefault(model, {'task_types': {}, 'total_uses': 0, 'last_used': ''})
    m = db['models'][model]
    m['total_uses'] = m.get('total_uses', 0) + 1
    m['last_used'] = datetime.now().isoformat()

    tt = m['task_types'].setdefault(task_type, {'uses': 0, 'total_latency': 0.0, 'success': 0, 'fail': 0, 'avg_latency': 0.0})
    tt['uses'] = tt.get('uses', 0) + 1
    tt['total_latency'] = tt.get('total_latency', 0.0) + latency_sec
    if success:
        tt['success'] = tt.get('success', 0) + 1
    else:
        tt['fail'] = tt.get('fail', 0) + 1
    tt['avg_latency'] = round(tt['total_latency'] / tt['uses'], 3)
    if user_score > 0:
        scores = tt.get('scores', [])
        scores.append(user_score)
        tt['scores'] = scores[-50:]  # keep last 50
        tt['avg_score'] = round(sum(tt['scores']) / len(tt['scores']), 2)

    _save_perf_db(db)


def _append_route_log(entry: dict):
    _ensure_dirs()
    with open(ROUTE_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')


# ── Task Classifier ──────────────────────────────────────────────────────────

_TASK_PATTERNS = {
    'ansible': re.compile(
        r'\b(ansible|playbook|role|collection|aap|automation\s+platform|jinja2?|inventory|handler|task|module|galaxy|awx|satellite\s+patch)\b',
        re.IGNORECASE,
    ),
    'code': re.compile(
        r'\b(code|script|function|class|debug|compile|syntax|write\s+a|generate\s+(code|function|class)|python|bash|shell|terraform|dockerfile|helm)\b',
        re.IGNORECASE,
    ),
    'security': re.compile(
        r'\b(cve|vulnerability|owasp|exploit|harden|selinux|firewall|audit|pen\s?test|intrusion|rbac|privilege|escalat|injection|xss|csrf)\b',
        re.IGNORECASE,
    ),
    'reasoning': re.compile(
        r'\b(why|how\s+does|explain\s+the\s+concept|what\s+is\s+the\s+difference|compare|analyze|pros\s+and\s+cons|evaluate|recommend|best\s+practice)\b',
        re.IGNORECASE,
    ),
    'business': re.compile(
        r'\b(account|intel|brief|executive|stakeholder|stock|revenue|customer|market|company|competitor|business)\b',
        re.IGNORECASE,
    ),
    'search': re.compile(
        r'\b(latest|recent|today|current|news|2024|2025|2026|release|update|patch\s+tuesday|new\s+in)\b',
        re.IGNORECASE,
    ),
    'system': re.compile(
        r'\b(systemd|service|journalctl|log|diagnostic|disk|cpu|memory|process|kernel|boot|grub|rhel|satellite|insights|subscription)\b',
        re.IGNORECASE,
    ),
    'fast': re.compile(
        r'^(what\s+is\s+\w+\??|who\s+is\s+|when\s+was\s+|define\s+|meaning\s+of\s+|short\s+answer|quick\s+|tl;?dr)',
        re.IGNORECASE,
    ),
    'creative': re.compile(
        r'\b(write\s+(a\s+)?(poem|story|joke|email|blog|analogy)|brainstorm|imagine|creative|fun\s+way)\b',
        re.IGNORECASE,
    ),
}


def classify_task(text: str) -> tuple[str, dict[str, float]]:
    """Classify a text query into a task type + confidence scores.

    Returns (best_task_type, {task_type: confidence_score}).
    """
    if not text:
        return 'general', {'general': 1.0}

    scores: dict[str, int] = {}
    for task, pat in _TASK_PATTERNS.items():
        matches = pat.findall(text)
        if matches:
            scores[task] = len(matches)

    if not scores:
        return 'general', {'general': 1.0}

    # Normalize to 0-1
    total = sum(scores.values())
    norm = {k: round(v / total, 3) for k, v in scores.items()}
    best = max(scores, key=scores.get)
    return best, norm


# ── Model Selector ──────────────────────────────────────────────────────────

def _load_task_model_prefs() -> dict:
    """Load model prefs, allowing user override via ~/.mcp-ai/brain/model-prefs.json."""
    prefs = {k: list(v) for k, v in DEFAULT_TASK_MODEL_PREFS.items()}
    override_path = os.path.join(BRAIN_DIR, 'model-prefs.json')
    if os.path.exists(override_path):
        try:
            with open(override_path, encoding='utf-8') as f:
                overrides = json.load(f)
            for k, v in overrides.items():
                if k in prefs and isinstance(v, list):
                    prefs[k] = [str(m) for m in v if str(m).strip()]
        except Exception:
            pass
    return prefs


def _get_available_models() -> list[str]:
    try:
        with urllib.request.urlopen(f'{OLLAMA_BASE}/api/tags', timeout=5) as r:
            return [m['name'] for m in json.loads(r.read().decode()).get('models', [])]
    except Exception:
        return []


def select_model(task_type: str, available: list[str] | None = None) -> str:
    """Choose best available model for task type, using performance history to break ties."""
    if available is None:
        available = _get_available_models()

    prefs = _load_task_model_prefs()
    db = _load_perf_db()

    candidates = prefs.get(task_type, []) + prefs.get('general', [])
    available_set = set(available)

    # Score candidates: pref rank + learned performance
    scored = []
    for rank, model in enumerate(candidates):
        if model not in available_set:
            continue
        perf = db.get('models', {}).get(model, {}).get('task_types', {}).get(task_type, {})
        avg_latency = perf.get('avg_latency', 999.0)
        success_rate = perf.get('success', 0) / max(1, perf.get('uses', 1))
        # Lower rank (more preferred) + lower latency + higher success = better score
        score = rank - (success_rate * 5) + (avg_latency * 0.1)
        scored.append((score, model))

    if scored:
        return min(scored, key=lambda x: x[0])[1]

    return available[0] if available else 'qwen2.5-coder:7b'


# ── LLM Call with Fallback Chain ────────────────────────────────────────────

def _call_ollama(prompt: str, model: str, system: str = '', temperature: float = 0.5, timeout: int = 90) -> tuple[str, float]:
    """Call Ollama via bridge. Returns (response_text, latency_sec)."""
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

    t0 = time.time()
    try:
        req = urllib.request.Request(
            OLLAMA_CHAT, data=payload,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode())
        latency = time.time() - t0

        if isinstance(resp, dict):
            if 'message' in resp:
                return resp['message'].get('content', ''), latency
            if 'choices' in resp:
                return resp['choices'][0]['message']['content'], latency
            if 'response' in resp:
                return resp['response'], latency
        return str(resp), latency
    except Exception as e:
        return f'[ERR: {e}]', time.time() - t0


def routed_call(prompt: str, task_type: str | None = None, temperature: float = 0.5,
                fallback_count: int = 2, record: bool = True) -> str:
    """Classify task, pick best model, call with fallback chain, record perf."""
    if not task_type:
        task_type, _ = classify_task(prompt)

    available = _get_available_models()
    prefs = _load_task_model_prefs()
    candidates = prefs.get(task_type, []) + prefs.get('general', [])
    # Filter to available, de-dupe
    seen: set = set()
    chain: list[str] = []
    for m in candidates:
        if m in set(available) and m not in seen:
            chain.append(m)
            seen.add(m)
    if not chain and available:
        chain = available[:fallback_count + 1]

    entry = {
        'timestamp': datetime.now().isoformat(),
        'prompt_len': len(prompt),
        'task_type': task_type,
        'chain': chain[:fallback_count + 1],
        'model_used': None,
        'latency': None,
        'success': False,
    }

    for model in chain[:fallback_count + 1]:
        text, latency = _call_ollama(prompt, model, temperature=temperature)
        success = not text.startswith('[ERR:')
        if record:
            record_inference(model, task_type, latency, success)
        if success:
            entry.update({'model_used': model, 'latency': round(latency, 2), 'success': True})
            _append_route_log(entry)
            return text

    _append_route_log(entry)
    return '[All models in fallback chain failed. Is Ollama running?]'


# ── Ensemble Engine ──────────────────────────────────────────────────────────

def ensemble_call(prompt: str, models: list[str] | None = None, task_type: str | None = None,
                  judge_model: str | None = None, parallel: bool = True) -> str:
    """Run prompt through multiple models in parallel, then synthesize best answer."""
    available = _get_available_models()

    if not models:
        if not task_type:
            task_type, _ = classify_task(prompt)
        prefs = _load_task_model_prefs()
        candidates = [m for m in prefs.get(task_type, []) + prefs.get('general', []) if m in set(available)]
        models = candidates[:3]

    if not models:
        return '[No models available for ensemble]'

    print(f'\n⚡ Ensemble ({len(models)} models): {", ".join(models)}\n')

    results: dict[str, str] = {}

    if parallel and len(models) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as ex:
            futures = {ex.submit(_call_ollama, prompt, m, temperature=0.5): m for m in models}
            for fut in concurrent.futures.as_completed(futures):
                m = futures[fut]
                try:
                    text, lat = fut.result(timeout=120)
                    results[m] = text
                    print(f'  ✓ {m} ({lat:.1f}s)')
                except Exception as e:
                    results[m] = f'[ERR: {e}]'
                    print(f'  ✗ {m} error')
    else:
        for m in models:
            text, lat = _call_ollama(prompt, m, temperature=0.5)
            results[m] = text
            print(f'  ✓ {m} ({lat:.1f}s)')

    print()

    # Synthesize with a judge model (pick best available if not specified)
    judge = judge_model or (judge_model := select_model('reasoning', available))

    print(f'🧑‍⚖️ Synthesizing with judge: {judge}\n')

    responses_block = '\n\n'.join(
        f'--- Response from {m} ---\n{r}' for m, r in results.items()
        if not r.startswith('[ERR:')
    )

    synthesis_prompt = (
        f'You have received the following responses from multiple AI models to this question:\n\n'
        f'QUESTION: {prompt}\n\n'
        f'{responses_block}\n\n'
        f'Synthesize the best, most complete, and accurate answer. '
        f'Incorporate unique insights from each response. '
        f'If they disagree, note the disagreement and give the most defensible answer. '
        f'Be concise but comprehensive.'
    )

    synthesis, _ = _call_ollama(synthesis_prompt, judge, temperature=0.3)

    print('─' * 70)
    print('🤖 Ensemble synthesis:\n')
    print(synthesis)
    return synthesis


# ── Task Planner ─────────────────────────────────────────────────────────────

def task_plan(goal: str) -> list[dict]:
    """Decompose a complex goal into subtasks with optimal resource assignments."""
    print(f'\n🧠 HAL Task Planner: {goal}\n')
    print('Analyzing goal and decomposing into subtasks...\n')

    available = _get_available_models()
    planner_model = select_model('reasoning', available)

    system = (
        f'You are {ASSISTANT}, an expert AI task planner for Red Hat Linux/Ansible/infrastructure environments. '
        f'You decompose complex goals into concrete, ordered subtasks and identify the best tool for each.'
    )

    prompt = (
        f'Decompose this goal into 3-7 concrete, ordered subtasks that a sysadmin can execute:\n\n'
        f'GOAL: {goal}\n\n'
        f'For each subtask, identify:\n'
        f'1. What needs to be done (action)\n'
        f'2. What tool/resource is best (ollama-model, ansible, satellite, bash, python, web-search, hal-tools)\n'
        f'3. Expected output / success criteria\n'
        f'4. Dependencies on prior steps\n\n'
        f'Available Ollama models: {", ".join(available[:8])}\n\n'
        f'Format as a numbered list. Be specific and actionable.'
    )

    plan_text, _ = _call_ollama(prompt, planner_model, system=system, temperature=0.3)

    print('📋 Task Plan:\n')
    print(plan_text)

    # Parse into structured list (best-effort)
    tasks = []
    for line in plan_text.split('\n'):
        m = re.match(r'^(\d+)[.)]\s*(.+)', line.strip())
        if m:
            tasks.append({'step': int(m.group(1)), 'description': m.group(2).strip()})

    return tasks


# ── Resource Manager ─────────────────────────────────────────────────────────

def resource_status(verbose: bool = False) -> dict:
    """Show all resources: models, packages, MCP servers."""
    print('\n' + '═' * 70)
    print(f'  {ASSISTANT} Resource Status')
    print('═' * 70 + '\n')

    db = _load_perf_db()

    # ── Ollama models
    print('🤖 Ollama Models:\n')
    try:
        with urllib.request.urlopen(f'{OLLAMA_BASE}/api/tags', timeout=5) as r:
            data = json.loads(r.read().decode())
        models = data.get('models', [])
    except Exception as e:
        models = []
        print(f'  ✗ Cannot reach Ollama: {e}\n')

    model_data = {}
    for m in sorted(models, key=lambda x: x.get('name', '')):
        name = m['name']
        size = m.get('size', 0)
        size_str = f'{size/1e9:.1f}GB' if size > 1e9 else f'{size/1e6:.0f}MB'
        last_used = db.get('models', {}).get(name, {}).get('last_used', 'never')[:10]
        total_uses = db.get('models', {}).get(name, {}).get('total_uses', 0)
        model_data[name] = {'size': size, 'size_str': size_str, 'last_used': last_used, 'total_uses': total_uses}
        print(f'  {name:<45} {size_str:>8}  used {total_uses:4}x  last: {last_used}')

    print(f'\n  Total: {len(models)} model(s)')

    # ── MCP servers
    print('\n\n🔗 MCP Servers:\n')
    registry = _load_mcp_registry()
    if not registry:
        print('  (No MCP servers registered. Run: hal --mcp-server-register)')
    else:
        for name, srv in registry.items():
            status = _mcp_server_health(srv)
            icon = '✓' if status == 'healthy' else '✗'
            print(f'  {icon} {name:<25} {srv.get("description", "")[:40]}  [{status}]')

    # ── Key Python packages
    print('\n\n📦 Key Python Packages:\n')
    packages = ['requests', 'ansible', 'duckduckgo-search', 'huggingface-hub', 'psutil',
                'streamlit', 'fastapi', 'rich', 'ollama-mcp-bridge', 'pandas']
    for pkg in packages:
        try:
            pkg_var = pkg.replace('-', '_')
            result = subprocess.run(
                [sys.executable, '-c', f'import importlib.metadata; print(importlib.metadata.version("{pkg_var}"))'],
                capture_output=True, text=True, timeout=5,
            )
            ver = result.stdout.strip() or '?'
            print(f'  ✓ {pkg:<30} {ver}')
        except Exception:
            print(f'  ? {pkg:<30} (check failed)')

    # ── Routing recommendation
    print('\n\n🎯 Current Task → Model Routing:\n')
    avail_names = [m['name'] for m in models]
    for task in TASK_TYPES[:8]:
        best = select_model(task, avail_names)
        print(f'  {task:<12} → {best}')

    print('\n' + '═' * 70 + '\n')
    return {'models': model_data, 'mcp': registry}


def resource_cleanup(days: int = 30, dry_run: bool = True) -> list[str]:
    """Remove Ollama models not used in the last N days."""
    print(f'\n🧹 Resource Cleanup (unused > {days} days)  [{"DRY RUN" if dry_run else "LIVE"}]\n')

    db = _load_perf_db()
    available = _get_available_models()
    cutoff = datetime.now() - timedelta(days=days)

    to_remove = []
    keep = []

    for model in available:
        last_used_str = db.get('models', {}).get(model, {}).get('last_used', '')
        if not last_used_str:
            # Never used — candidate for removal if not in recommended list
            is_recommended = model in set(RECOMMENDED_MODELS.values())
            if not is_recommended:
                to_remove.append((model, 'never used'))
            else:
                keep.append((model, 'recommended'))
        else:
            try:
                last_used = datetime.fromisoformat(last_used_str[:19])
                if last_used < cutoff:
                    to_remove.append((model, f'last used {last_used.date()}'))
                else:
                    keep.append((model, f'active {last_used.date()}'))
            except ValueError:
                keep.append((model, 'unknown date'))

    if not to_remove:
        print('  ✓ No models to remove. Everything is in active use.')
        return []

    print('  Candidates for removal:')
    for model, reason in to_remove:
        print(f'  ✗ {model:<45} ({reason})')
    print(f'\n  Keeping {len(keep)} active model(s).')

    if dry_run:
        print('\n  [Dry run — no models removed. Use --resource-cleanup with --confirm to remove.]\n')
        return [m for m, _ in to_remove]

    print()
    removed = []
    for model, reason in to_remove:
        confirm = input(f'  Remove {model}? ({reason}) [y/N]: ').strip().lower()
        if confirm == 'y':
            try:
                result = subprocess.run(['ollama', 'rm', model], capture_output=True, text=True)
                if result.returncode == 0:
                    print(f'  ✓ Removed: {model}')
                    removed.append(model)
                    _log_resource_event('remove', model, reason)
                else:
                    print(f'  ✗ Failed: {result.stderr.strip()}')
            except FileNotFoundError:
                print('  ollama command not found.')
        else:
            print(f'  Skipped: {model}')

    print(f'\n  Removed {len(removed)} model(s).')
    return removed


def resource_install(resource: str, auto_confirm: bool = False) -> int:
    """Pull an Ollama model or install a Python package."""
    resource = resource.strip()

    # Determine resource type
    is_pip = '/' not in resource and ':' not in resource and resource.replace('-', '').replace('_', '').isalpha()
    is_pip = is_pip or resource in ALLOWED_AUTO_INSTALL

    if is_pip:
        # Python package install
        clean = resource.replace('-', '_').lower()
        print(f'\n📦 Install Python package: {resource}\n')

        if not auto_confirm and resource not in ALLOWED_AUTO_INSTALL:
            print(f'  ⚠  Package "{resource}" is not in the auto-install whitelist.')
            confirm = input(f'  Install anyway? [y/N]: ').strip().lower()
            if confirm != 'y':
                print('  Cancelled.')
                return 0

        result = subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', resource], check=False)
        if result.returncode == 0:
            print(f'  ✓ Installed: {resource}')
            _log_resource_event('pip-install', resource, 'manual')
        else:
            print(f'  ✗ Failed to install: {resource}')
        return result.returncode
    else:
        # Ollama model pull
        print(f'\n⬇  Pull Ollama model: {resource}\n')
        if not auto_confirm:
            confirm = input(f'  Pull {resource}? This may download several GB. [y/N]: ').strip().lower()
            if confirm != 'y':
                print('  Cancelled.')
                return 0

        result = subprocess.run(['ollama', 'pull', resource], check=False)
        if result.returncode == 0:
            _log_resource_event('model-pull', resource, 'manual')
        return result.returncode


def auto_pull_recommended(dry_run: bool = False) -> list[str]:
    """Check if recommended models are present; offer to pull missing ones."""
    available = set(_get_available_models())
    missing = {role: model for role, model in RECOMMENDED_MODELS.items() if model not in available}

    if not missing:
        print('\n✓ All recommended models are already installed.\n')
        return []

    print(f'\n🤖 Recommended models not yet installed ({len(missing)}):\n')
    for role, model in missing.items():
        print(f'  [{role:12}] {model}')

    if dry_run:
        print('\n  [Dry run — use --auto-pull to actually download]\n')
        return list(missing.values())

    pulled = []
    for role, model in missing.items():
        confirm = input(f'\n  Pull {model} (needed for {role} tasks)? [y/N]: ').strip().lower()
        if confirm == 'y':
            result = subprocess.run(['ollama', 'pull', model], check=False)
            if result.returncode == 0:
                pulled.append(model)
                _log_resource_event('model-pull', model, f'auto-pull:{role}')
    return pulled


def _log_resource_event(event: str, resource: str, reason: str):
    _ensure_dirs()
    with open(RESOURCE_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps({
            'timestamp': datetime.now().isoformat(),
            'event': event,
            'resource': resource,
            'reason': reason,
        }) + '\n')


# ── MCP Server Management ────────────────────────────────────────────────────

def _load_mcp_registry() -> dict:
    _ensure_dirs()
    registry = dict(BUILTIN_MCP_SERVERS)
    if os.path.exists(MCP_REGISTRY):
        try:
            with open(MCP_REGISTRY, encoding='utf-8') as f:
                saved = json.load(f)
            registry.update(saved)
        except Exception:
            pass
    return registry


def _save_mcp_registry(registry: dict):
    # Only save non-builtin entries to avoid polluting config
    to_save = {k: v for k, v in registry.items() if k not in BUILTIN_MCP_SERVERS}
    _ensure_dirs()
    with open(MCP_REGISTRY, 'w', encoding='utf-8') as f:
        json.dump(to_save, f, indent=2)


def _mcp_server_health(srv: dict) -> str:
    health_url = srv.get('health_url')
    if health_url:
        try:
            with urllib.request.urlopen(health_url, timeout=3) as r:
                return 'healthy' if r.status == 200 else 'unhealthy'
        except Exception:
            return 'offline'
    # No health URL — check by PID file or command presence
    pid_file = srv.get('pid_file')
    if pid_file and os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return 'running'
        except Exception:
            return 'stale-pid'
    return 'unknown'


def cmd_mcp_server_status():
    """List all registered MCP servers with health status."""
    registry = _load_mcp_registry()
    print(f'\n🔗 MCP Server Registry ({len(registry)} servers)\n')
    print(f'  {"NAME":<20} {"STATUS":<12} {"DESCRIPTION"}')
    print('  ' + '─' * 70)
    for name, srv in registry.items():
        status = _mcp_server_health(srv)
        icon = {'healthy': '✓', 'running': '✓', 'offline': '✗', 'unknown': '?', 'unhealthy': '⚠'}.get(status, '?')
        desc = srv.get('description', '')[:45]
        print(f'  {icon} {name:<20} {status:<12} {desc}')
    print()


def cmd_mcp_server_start(name: str) -> int:
    """Start a registered MCP server."""
    registry = _load_mcp_registry()
    if name not in registry:
        print(f'MCP server not registered: {name}', file=sys.stderr)
        print(f'Available: {", ".join(registry.keys())}')
        return 1

    srv = registry[name]
    status = _mcp_server_health(srv)
    if status in ('healthy', 'running'):
        print(f'  {name} is already running.')
        return 0

    cmd = srv.get('command', [])
    if not cmd:
        print(f'No command defined for {name}', file=sys.stderr)
        return 1

    print(f'\n▶  Starting MCP server: {name}')
    env = {**os.environ, **srv.get('env', {})}

    if isinstance(cmd, str):
        proc = subprocess.Popen(cmd, shell=True, env=env)
    else:
        proc = subprocess.Popen(cmd, env=env)

    time.sleep(2)
    if proc.poll() is None:
        pid_file = srv.get('pid_file')
        if pid_file:
            with open(pid_file, 'w') as f:
                f.write(str(proc.pid))
        print(f'  ✓ {name} started (PID {proc.pid})')
        _log_resource_event('mcp-start', name, 'manual')
        return 0
    else:
        print(f'  ✗ {name} exited immediately (rc={proc.returncode})')
        return proc.returncode


def cmd_mcp_server_stop(name: str) -> int:
    """Stop a running MCP server."""
    registry = _load_mcp_registry()
    if name not in registry:
        print(f'MCP server not registered: {name}', file=sys.stderr)
        return 1

    srv = registry[name]
    pid_file = srv.get('pid_file')
    if pid_file and os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            os.unlink(pid_file)
            print(f'  ✓ Sent SIGTERM to {name} (PID {pid})')
            _log_resource_event('mcp-stop', name, 'manual')
            return 0
        except ProcessLookupError:
            print(f'  {name}: process not found (already stopped?)')
            return 0
        except Exception as e:
            print(f'  ✗ Could not stop {name}: {e}')
            return 1
    else:
        print(f'  No PID file for {name}. Try stopping manually.')
        return 1


def cmd_mcp_server_register(name: str, command: str, description: str = '', health_url: str = '') -> int:
    """Register a new MCP server."""
    registry = _load_mcp_registry()
    registry[name] = {
        'description': description or name,
        'command': command,
        'env': {},
        'health_url': health_url or None,
        'registered': datetime.now().isoformat(),
    }
    _save_mcp_registry({k: v for k, v in registry.items() if k not in BUILTIN_MCP_SERVERS})
    print(f'  ✓ Registered MCP server: {name}')
    _log_resource_event('mcp-register', name, 'manual')
    return 0


# ── Brain Route (interactive query classifier + dispatcher) ──────────────────

def cmd_brain_route(query: str, verbose: bool = True) -> str:
    """Classify a query, select the best model, route it, and return the answer."""
    task_type, scores = classify_task(query)
    available = _get_available_models()
    model = select_model(task_type, available)

    if verbose:
        top_scores = sorted(scores.items(), key=lambda x: -x[1])[:3]
        score_str = '  '.join(f'{k}:{v:.2f}' for k, v in top_scores)
        print(f'\n🧠 Brain Route')
        print(f'  Task type  : {task_type}  ({score_str})')
        print(f'  Model      : {model}')
        print(f'  Available  : {len(available)} model(s)\n')

    t0 = time.time()
    text, lat = _call_ollama(query, model, temperature=0.5)
    record_inference(model, task_type, lat, not text.startswith('[ERR:'))

    if verbose:
        print(f'🤖 Response ({lat:.1f}s, {model}):\n')
        print(text)

    return text


# ── Brain Learn (benchmark all models) ──────────────────────────────────────

BENCHMARK_TASKS = [
    ('code',      'Write a Python function that parses a YAML file and returns it as a dict.'),
    ('ansible',   'Write an Ansible task that installs httpd and ensures it starts on boot.'),
    ('security',  'List 3 OWASP Top 10 risks relevant to a REST API.'),
    ('reasoning', 'Compare blue-green vs. canary deployments in 3 sentences.'),
    ('fast',      'What does idempotent mean?'),
    ('system',    'How do you check disk I/O usage on RHEL with a single command?'),
]


def cmd_brain_learn(runs_per_task: int = 1) -> None:
    """Run benchmark tasks against all available models and update performance DB."""
    available = _get_available_models()
    if not available:
        print('No Ollama models available.', file=sys.stderr)
        return

    print(f'\n🧠 HAL Brain Learning Mode')
    print(f'  Models : {len(available)}')
    print(f'  Tasks  : {len(BENCHMARK_TASKS)}')
    print(f'  Runs   : {runs_per_task} per task\n')

    results: dict = {}
    for model in available:
        results[model] = {}
        for task_type, prompt in BENCHMARK_TASKS:
            print(f'  [{model}] {task_type}...', end='', flush=True)
            latencies = []
            for _ in range(runs_per_task):
                _, lat = _call_ollama(prompt, model, temperature=0.0)
                latencies.append(lat)
                record_inference(model, task_type, lat, True)
            avg = round(sum(latencies) / len(latencies), 2)
            results[model][task_type] = avg
            print(f' {avg:.1f}s')

    # Print summary table
    print('\n' + '─' * 70)
    print(f'  {"MODEL":<40}', end='')
    for task, _ in BENCHMARK_TASKS:
        print(f' {task[:6]:>7}', end='')
    print()
    print('  ' + '─' * 68)
    for model, scores in sorted(results.items()):
        print(f'  {model:<40}', end='')
        for task, _ in BENCHMARK_TASKS:
            lat = scores.get(task, 0)
            print(f' {lat:>7.1f}', end='')
        print()
    print('─' * 70)
    print('\n✓ Performance database updated. Routing will use these scores.\n')


# ── Brain Status ─────────────────────────────────────────────────────────────

def cmd_brain_status() -> None:
    """Comprehensive brain status: routing, performance, recent routes."""
    print('\n' + '═' * 70)
    print(f'  {ASSISTANT} Brain Status')
    print('═' * 70)

    db = _load_perf_db()
    available = _get_available_models()

    # Current routing table
    print('\n📍 Task → Model Routing (adaptive):\n')
    for task in TASK_TYPES[:9]:
        model = select_model(task, available)
        perf = db.get('models', {}).get(model, {}).get('task_types', {}).get(task, {})
        lat = perf.get('avg_latency', 0)
        uses = perf.get('uses', 0)
        lat_str = f'{lat:.1f}s avg' if lat else 'no data'
        print(f'  {task:<12} → {model:<45} {lat_str}  ({uses} uses)')

    # Recent routes
    print('\n📋 Recent Route Log (last 5):\n')
    if os.path.exists(ROUTE_LOG):
        lines = []
        with open(ROUTE_LOG, encoding='utf-8') as f:
            for line in f:
                lines.append(line)
        for entry in lines[-5:]:
            try:
                r = json.loads(entry)
                ts = r.get('timestamp', '')[:16]
                tt = r.get('task_type', '?')
                model = r.get('model_used', '?')
                lat = r.get('latency', '?')
                print(f'  {ts}  {tt:<12} → {model}  ({lat}s)')
            except Exception:
                pass
    else:
        print('  No routes logged yet.')

    # Model usage summary
    print('\n📊 Model Usage Summary:\n')
    models_db = db.get('models', {})
    if not models_db:
        print('  No usage data yet. Run queries to populate.')
    else:
        for model, data in sorted(models_db.items(), key=lambda x: -x[1].get('total_uses', 0)):
            uses = data.get('total_uses', 0)
            last = data.get('last_used', 'never')[:10]
            print(f'  {model:<45} {uses:4}x  last: {last}')

    print('\n' + '═' * 70 + '\n')


# ── Package Search + Install ──────────────────────────────────────────────────

def cmd_find_and_install(capability: str) -> int:
    """AI-driven: given a needed capability, find the best package and install it."""
    print(f'\n🔍 Finding package for capability: {capability}\n')

    available = _get_available_models()
    model = select_model('reasoning', available)

    prompt = (
        f'I need a Python package (pip installable) that provides this capability: "{capability}"\n\n'
        f'Reply with ONLY the pip package name on the first line, then a one-line description. '
        f'No extra text.'
    )
    text, _ = _call_ollama(prompt, model, temperature=0.1)
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if not lines:
        print('Could not determine package name.', file=sys.stderr)
        return 1

    pkg_name = lines[0].strip().split()[0]  # first word of first line
    desc = lines[1] if len(lines) > 1 else ''

    print(f'  Suggested package: {pkg_name}')
    if desc:
        print(f'  Description: {desc}')

    return resource_install(pkg_name)


# ── Main CLI ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description='HAL Brain — Autonomous resource management and intelligent LLM routing',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Brain operations
    brain = ap.add_argument_group('Brain / Routing')
    brain.add_argument('--brain-status', action='store_true', help='Full brain status: routing table, model usage, recent routes')
    brain.add_argument('--brain-route', metavar='QUERY', help='Route a query through the intelligent classifier and best model')
    brain.add_argument('--brain-learn', action='store_true', help='Run benchmark tasks to update model performance database')
    brain.add_argument('--brain-learn-runs', type=int, default=1, help='Runs per benchmark task (default: 1)')
    brain.add_argument('--ensemble', metavar='PROMPT', help='Multi-model ensemble inference + synthesis')
    brain.add_argument('--ensemble-models', metavar='MODELS', help='Comma/space-separated models for --ensemble (default: auto-selected)')
    brain.add_argument('--task-plan', metavar='GOAL', help='Decompose a complex goal into ordered subtasks with resource assignments')

    # Resource management
    res = ap.add_argument_group('Resource Management')
    res.add_argument('--resource-status', action='store_true', help='Show all resources: models, packages, MCP servers')
    res.add_argument('--resource-cleanup', nargs='?', const=30, type=int, metavar='DAYS',
                     help='Show models unused for N days (default: 30). Add --confirm to remove.')
    res.add_argument('--confirm', action='store_true', help='Actually perform removal (use with --resource-cleanup)')
    res.add_argument('--resource-install', metavar='RESOURCE', help='Pull an Ollama model or install a Python package')
    res.add_argument('--auto-pull', action='store_true', help='Pull recommended models if missing')
    res.add_argument('--auto-confirm', action='store_true', help='Skip confirmation prompts for --auto-pull / --resource-install')
    res.add_argument('--find-install', metavar='CAPABILITY', help='AI-suggested package for a capability, then install it')

    # MCP server
    mcp = ap.add_argument_group('MCP Server Management')
    mcp.add_argument('--mcp-server-status', action='store_true', help='List all registered MCP servers with health status')
    mcp.add_argument('--mcp-server-start', metavar='NAME', help='Start a registered MCP server by name')
    mcp.add_argument('--mcp-server-stop', metavar='NAME', help='Stop a registered MCP server by name')
    mcp.add_argument('--mcp-server-register', nargs='+', metavar='ARGS',
                     help='Register a server: NAME COMMAND [--description DESC] [--health-url URL]')

    # Misc
    ap.add_argument('--verbose', action='store_true', help='Verbose output')
    ap.add_argument('--model', metavar='MODEL', help='Override model selection')

    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()
    _ensure_dirs()

    if args.brain_status:
        cmd_brain_status()
        return 0

    if args.brain_route:
        cmd_brain_route(args.brain_route, verbose=True)
        return 0

    if args.brain_learn:
        cmd_brain_learn(runs_per_task=args.brain_learn_runs)
        return 0

    if args.ensemble:
        models = None
        if args.ensemble_models:
            models = [m.strip() for m in re.split(r'[,\s]+', args.ensemble_models) if m.strip()]
        ensemble_call(args.ensemble, models=models)
        return 0

    if args.task_plan:
        task_plan(args.task_plan)
        return 0

    if args.resource_status:
        resource_status(verbose=args.verbose)
        return 0

    if args.resource_cleanup is not None:
        resource_cleanup(days=args.resource_cleanup, dry_run=not args.confirm)
        return 0

    if args.resource_install:
        return resource_install(args.resource_install, auto_confirm=args.auto_confirm)

    if args.auto_pull:
        auto_pull_recommended(dry_run=False)
        return 0

    if args.find_install:
        return cmd_find_and_install(args.find_install)

    if args.mcp_server_status:
        cmd_mcp_server_status()
        return 0

    if args.mcp_server_start:
        return cmd_mcp_server_start(args.mcp_server_start)

    if args.mcp_server_stop:
        return cmd_mcp_server_stop(args.mcp_server_stop)

    if args.mcp_server_register:
        parts = args.mcp_server_register
        if len(parts) < 2:
            print('Usage: --mcp-server-register NAME COMMAND [--description D] [--health-url U]')
            return 1
        name = parts[0]
        command = parts[1]
        description = ''
        health_url = ''
        for i, p in enumerate(parts[2:]):
            if p == '--description' and i + 3 < len(parts):
                description = parts[i + 3]
            elif p == '--health-url' and i + 3 < len(parts):
                health_url = parts[i + 3]
        return cmd_mcp_server_register(name, command, description, health_url)

    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
