#!/usr/bin/env python3
"""Minimal MoE router prototype for HAL.

This is a light-weight Mixture-of-Experts orchestration layer prototype.
It selects one or more "experts" for a query, invokes them (via the
local bridge / Ollama endpoint), and aggregates their outputs.

This implementation is intentionally small and pragmatic: rule-based
routing with an LLM-backed aggregator. It is designed for on-prem
deployments using the existing bridge at $OLLAMA_URL.
"""
from __future__ import annotations

import json
import os
import time
import typing as t
import logging
import math

_EMBED_MODEL = None
_PROFILE_EMBEDS: dict[str, object] = {}
_EMBEDDING_BACKEND = None
# Optional index structures for fast profile lookup
_INDEX = None
_INDEX_TYPE: str | None = None
_INDEX_NAMES: list[str] = []

# Prometheus integration (optional)
try:
    from prometheus_client import start_http_server, Counter, Summary
    _PROM_AVAILABLE = True
except Exception:
    _PROM_AVAILABLE = False

_PROM_ENABLED = False
_PROM_METRICS: dict = {}

try:
    import requests
except Exception:
    requests = None
import mcp_ai_config as config

# Determine defaults from centralized config
_MCP_AI_HOME = config.get_config('ai_home', None)
OLLAMA_URL = config.get_config('ollama_url', config.get_ollama_url())
if not OLLAMA_URL.endswith('/api/chat'):
    OLLAMA_URL = OLLAMA_URL.rstrip('/') + '/api/chat'

logger = logging.getLogger('moe_router')


EXPERTS: dict[str, dict] = {
    'business': {
        'system': 'You are a Business Intelligence expert. Use available RAG context to produce concise, factual account briefs, list primary objectives, notable news and source URLs. Provide concise answers with provenance lines when possible.',
        'model': None,
    },
    'summarizer': {
        'system': 'You are a Summarizer. Produce a short summary and a 2-3 sentence executive summary. Prefer extremely concise phrasing.',
        'model': None,
    },
    'general': {
        'system': 'You are a helpful general-purpose assistant. Answer the user query clearly and directly.',
        'model': None,
    },
    'aggregator': {
        'system': 'You are an Aggregator that synthesizes multiple expert responses. Given labeled expert outputs and their stated sources, produce a single coherent answer, indicate which expert is most reliable, and include provenance lines.',
        'model': None,
    }
}


MODEL_MAP: dict | None = None


def _model_map_paths() -> list[str]:
    paths: list[str] = []
    # explicit path via env
    envp = os.environ.get('MOE_MODEL_MAP_PATH') or os.environ.get('MOE_MODEL_MAP_FILE')
    if envp:
        paths.append(envp)
    # AI_HOME inside config if set
    if _MCP_AI_HOME:
        paths.append(os.path.join(_MCP_AI_HOME, 'model_map.json'))
    paths.append(os.path.expanduser('~/.mcp-ai/model_map.json'))
    paths.append('/etc/mcp-ai/model_map.json')
    # packaged example fallback
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        paths.append(os.path.join(repo_root, 'packaging', 'llm', 'model_map.example.json'))
    except Exception:
        pass
    return paths


def load_model_map() -> dict:
    """Load per-expert model mapping from env or config files.

    Supports env JSON string `MOE_MODEL_MAP` or files from
    `MOE_MODEL_MAP_PATH`, `~/.mcp-ai/model_map.json`, `/etc/mcp-ai/model_map.json`,
    or the packaged example.
    """
    global MODEL_MAP
    if MODEL_MAP is not None:
        return MODEL_MAP
    # inline JSON via env
    env_json = os.environ.get('MOE_MODEL_MAP')
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, dict):
                MODEL_MAP = data
                return MODEL_MAP
        except Exception:
            pass

    for p in _model_map_paths():
        try:
            if p and os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        MODEL_MAP = data
                        return MODEL_MAP
        except Exception:
            continue

    MODEL_MAP = {}
    return MODEL_MAP


def _call_bridge(messages: list[dict], model: str | None = None, timeout: int = 60, endpoint: str | None = None) -> str:
    # Ensure a model is chosen; try to reuse hal-tools picker when available
    if not model:
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
            import importlib.util
            spec = importlib.util.spec_from_file_location('hal_tools', os.path.join(base_dir, 'scripts', 'hal-tools.py'))
            hal_tools = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(hal_tools)
            model = hal_tools._pick_default_model(task_hint='general')
        except Exception:
            model = os.environ.get('MOE_DEFAULT_MODEL', 'qwen2.5-coder:7b')

    payload = {'model': model or '', 'messages': messages, 'stream': False}
    # Try to use centralized llm_client when available (provides pooling and failover)
    client = None
    try:
        import llm_client
        client = llm_client.get_client()
    except Exception:
        client = None

    retries = int(os.environ.get('MOE_OLLAMA_RETRIES', '2'))
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            # If a specific endpoint was provided, prefer a direct call to that endpoint.
            if client:
                if endpoint:
                    # call a specific endpoint via client.call
                    resp = client.call(payload, stream=False, timeout=timeout, endpoint=endpoint)
                else:
                    # try failover across configured endpoints
                    resp = client.call_with_failover(payload, stream=False, timeout=timeout)
                return resp
            target = endpoint or OLLAMA_URL
            if requests:
                r = requests.post(target, json=payload, timeout=timeout)
                r.raise_for_status()
                return r.text
            # fallback to urllib
            import urllib.request

            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(target, data=data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8')
        except Exception as e:
            last_err = e
            # exponential backoff
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1) * 0.5, 10))
                continue
    return f'ERR: moe_bridge_call_failed: {last_err}'


def _ensure_embedding_model() -> bool:
    """Try to load a sentence-transformers model for embeddings. Returns True on success."""
    global _EMBED_MODEL, _PROFILE_EMBEDS, _EMBEDDING_BACKEND
    if _EMBED_MODEL is not None and _PROFILE_EMBEDS:
        return True
    try:
        from sentence_transformers import SentenceTransformer
        model_name = os.environ.get('MOE_SENT_TRANS_MODEL', 'all-MiniLM-L6-v2')
        _EMBED_MODEL = SentenceTransformer(model_name)
        _EMBEDDING_BACKEND = 'sentence_transformers'
        # precompute profile embeddings (store numpy arrays)
        try:
            import numpy as _np
            for name, kws in EXPERT_PROFILES.items():
                ptext = ' '.join(kws)
                vec = _EMBED_MODEL.encode(ptext, convert_to_numpy=True, normalize_embeddings=True)
                # ensure float32 for downstream indexers
                _PROFILE_EMBEDS[name] = _np.array(vec, dtype='float32')
        except Exception:
            # best-effort: store raw vector
            for name, kws in EXPERT_PROFILES.items():
                ptext = ' '.join(kws)
                vec = _EMBED_MODEL.encode(ptext, convert_to_numpy=True, normalize_embeddings=True)
                _PROFILE_EMBEDS[name] = vec
        logger.info('MoE embeddings initialized with %s', model_name)
        return True
    except Exception:
        _EMBED_MODEL = None
        _EMBEDDING_BACKEND = None
        return False


def _get_embedding(text: str):
    global _EMBED_MODEL
    if not _ensure_embedding_model():
        return None
    try:
        vec = _EMBED_MODEL.encode(text, convert_to_numpy=True, normalize_embeddings=True)
        return vec
    except Exception:
        return None


def _embeds_dir() -> str:
    if _MCP_AI_HOME:
        path = os.path.join(_MCP_AI_HOME, 'embeds')
    else:
        home = os.path.expanduser('~')
        path = os.path.join(home, '.mcp-ai', 'embeds')
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def _index_disk_paths() -> dict:
    d = _embeds_dir()
    return {
        'faiss': os.path.join(d, 'profiles.faiss'),
        'npz': os.path.join(d, 'profiles.npz'),
        'names': os.path.join(d, 'profiles_names.json'),
    }


def _save_profile_index(mat, names, index_obj=None, index_type: str | None = None) -> None:
    """Persist profile matrix and optional faiss index to disk."""
    paths = _index_disk_paths()
    try:
        import numpy as _np
        # save numpy matrix and names
        try:
            _np.savez_compressed(paths['npz'], mat=mat)
            with open(paths['names'], 'w', encoding='utf-8') as fh:
                json.dump(names, fh)
        except Exception:
            # best-effort: save plain npy per-row
            for i, row in enumerate(mat):
                _np.save(os.path.join(_embeds_dir(), f'profile_{i}.npy'), row)
        # save faiss index if provided
        if index_type == 'faiss' and index_obj is not None:
            try:
                import faiss
                faiss.write_index(index_obj, paths['faiss'])
            except Exception:
                pass
    except Exception:
        pass


def _load_profile_index() -> bool:
    """Attempt to load a persisted profile index. Returns True on success."""
    global _INDEX, _INDEX_TYPE, _INDEX_NAMES
    paths = _index_disk_paths()
    # try faiss file first
    try:
        if os.path.exists(paths['faiss']):
            try:
                import faiss
                idx = faiss.read_index(paths['faiss'])
                # load names
                names = []
                if os.path.exists(paths['names']):
                    try:
                        with open(paths['names'], 'r', encoding='utf-8') as fh:
                            names = json.load(fh)
                    except Exception:
                        names = []
                _INDEX = idx
                _INDEX_TYPE = 'faiss'
                _INDEX_NAMES = names
                return True
            except Exception:
                pass
        # try npz
        if os.path.exists(paths['npz']):
            try:
                import numpy as _np
                with _np.load(paths['npz']) as data:
                    mat = data['mat']
                names = []
                if os.path.exists(paths['names']):
                    try:
                        with open(paths['names'], 'r', encoding='utf-8') as fh:
                            names = json.load(fh)
                    except Exception:
                        names = []
                _INDEX = mat
                _INDEX_TYPE = 'brute'
                _INDEX_NAMES = names if names else [f'profile_{i}' for i in range(mat.shape[0])]
                return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _build_profile_index() -> bool:
    """Build a small in-memory index over EXPERT_PROFILES embeddings.

    Tries FAISS first, then sklearn NearestNeighbors, then falls back to brute-force numpy.
    Returns True when an index is available.
    """
    global _INDEX, _INDEX_TYPE, _INDEX_NAMES
    try:
        # try to load a persisted index first
        if _load_profile_index():
            return True
        # Ensure embeddings exist
        if not _ensure_embedding_model():
            return False
        import numpy as _np
        names = []
        mats = []
        for name, vec in _PROFILE_EMBEDS.items():
            names.append(name)
            mats.append(_np.asarray(vec, dtype='float32'))
        if not mats:
            return False
        mat = _np.vstack(mats).astype('float32')
        # try faiss
        try:
            import faiss
            d = mat.shape[1]
            index = faiss.IndexFlatIP(d)
            index.add(mat)
            _INDEX = index
            _INDEX_TYPE = 'faiss'
            _INDEX_NAMES = names
            # persist
            try:
                _save_profile_index(mat, names, index_obj=index, index_type='faiss')
            except Exception:
                pass
            return True
        except Exception:
            pass
        # try sklearn (or fallback to brute force matrix)
        try:
            from sklearn.neighbors import NearestNeighbors
            nn = NearestNeighbors(n_neighbors=min(3, mat.shape[0]), metric='cosine')
            nn.fit(mat)
            _INDEX = mat
            _INDEX_TYPE = 'sklearn'
            _INDEX_NAMES = names
            try:
                _save_profile_index(mat, names)
            except Exception:
                pass
            return True
        except Exception:
            pass
        # brute-force fallback: keep matrix and names
        _INDEX = mat
        _INDEX_TYPE = 'brute'
        _INDEX_NAMES = names
        try:
            _save_profile_index(mat, names)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _profile_search(query_emb, top_n=2):
    """Return top_n profile names ordered by similarity to query_emb."""
    try:
        import numpy as _np
        if _INDEX_TYPE == 'faiss':
            q = _np.asarray(query_emb, dtype='float32').reshape(1, -1)
            D, I = _INDEX.search(q, top_n)
            names = []
            for idx in I[0]:
                if 0 <= idx < len(_INDEX_NAMES):
                    names.append(_INDEX_NAMES[idx])
            return names
        if _INDEX_TYPE == 'sklearn' or _INDEX_TYPE == 'brute':
            mat = _INDEX
            q = _np.asarray(query_emb, dtype='float32')
            sims = mat.dot(q)
            # since embeddings are normalized, dot product ~ cosine similarity
            idxs = list(reversed(sorted(range(len(sims)), key=lambda i: float(sims[i]))))[:top_n]
            return [_INDEX_NAMES[i] for i in idxs]
    except Exception:
        pass
    return []


def _cosine_sim(a, b) -> float:
    try:
        # both may be numpy arrays
        import numpy as _np
        a = _np.array(a)
        b = _np.array(b)
        if a.size == 0 or b.size == 0:
            return 0.0
        denom = (_np.linalg.norm(a) * _np.linalg.norm(b))
        if denom == 0:
            return 0.0
        return float((_np.dot(a, b) / denom).item())
    except Exception:
        # pure-python fallback
        dot = 0.0
        for i in range(min(len(a), len(b))):
            dot += float(a[i]) * float(b[i])
        norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
        norm_b = math.sqrt(sum(float(x) * float(x) for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


def _choose_experts_rule(text: str) -> list[str]:
    t = (text or '').lower()
    keywords = ('account', 'company', 'customer', 'intel', 'intel-report', 'report', 'partner', 'sales', 'revenue')
    for k in keywords:
        if k in t:
            return ['business', 'general']
    # default: generalist + summarizer
    return ['general', 'summarizer']


def _text_tokens(text: str) -> dict[str, int]:
    """Return a simple token frequency dict for the text."""
    import re
    txt = (text or '').lower()
    toks = re.findall(r"\b[a-z0-9]+\b", txt)
    stop = set(('the', 'and', 'for', 'with', 'that', 'this', 'from', 'are', 'was', 'were', 'will', 'have', 'has', 'had', 'but', 'not', 'you', 'your', 'their', 'they', 'them'))
    out: dict[str, int] = {}
    for tkn in toks:
        if len(tkn) < 3 or tkn in stop:
            continue
        out[tkn] = out.get(tkn, 0) + 1
    return out


def _vector_similarity(a: dict[str, int], b: dict[str, int]) -> float:
    """Compute cosine similarity between two sparse token-frequency dicts."""
    import math
    # dot product
    dot = 0.0
    for k, v in a.items():
        if k in b:
            dot += v * b[k]
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


EXPERT_PROFILES: dict[str, list[str]] = {
    'business': ['account', 'company', 'customer', 'partner', 'sales', 'revenue', 'market', 'intel', 'report'],
    'summarizer': ['summarize', 'summary', 'short', 'concise', 'brief', 'high-level'],
    'general': ['how', 'what', 'why', 'help', 'explain', 'guide'],
}


def _choose_experts_embedding(text: str, top_n: int = 2, threshold: float = 0.05) -> list[str]:
    # Prefer real embeddings when available (sentence-transformers/FAISS). Fall back to token-frequency heuristic.
    try:
        if _ensure_embedding_model():
            # Build index lazily
            if _INDEX_TYPE is None:
                _build_profile_index()
            q_emb = _get_embedding(text)
            if q_emb is not None and _INDEX_TYPE is not None:
                names = _profile_search(q_emb, top_n=top_n)
                if names:
                    return names
    except Exception:
        pass

    # Fallback: token-frequency similarity
    qvec = _text_tokens(text)
    scores: list[tuple[str, float]] = []
    for name, kws in EXPERT_PROFILES.items():
        ptext = ' '.join(kws)
        pvec = _text_tokens(ptext)
        sim = _vector_similarity(qvec, pvec)
        scores.append((name, sim))
    scores.sort(key=lambda x: x[1], reverse=True)
    chosen = [n for n, s in scores[:top_n] if s >= threshold]
    if not chosen:
        # fallback to rule
        return _choose_experts_rule(text)
    return chosen


def _run_expert(expert: str, user_text: str, rag_context: str | None = None, timeout: int = 60, model_map: dict | None = None) -> str:
    cfg = EXPERTS.get(expert, EXPERTS['general'])
    system = cfg.get('system', '')
    messages = [{'role': 'system', 'content': system}]
    if rag_context:
        messages.append({'role': 'system', 'content': 'RAG CONTEXT:\n' + rag_context})
    messages.append({'role': 'user', 'content': user_text})
    model = None
    endpoint = None
    if isinstance(model_map, dict):
        mval = model_map.get(expert) or model_map.get('default')
        if isinstance(mval, dict):
            model = mval.get('model') or None
            endpoint = mval.get('endpoint') or None
        elif isinstance(mval, str):
            model = mval
    if not model:
        model = cfg.get('model')
    # Expert call with retries and metrics
    retries = int(os.environ.get('MOE_EXPERT_RETRIES', '2'))
    backoff_base = float(os.environ.get('MOE_EXPERT_BACKOFF_BASE', '0.5'))
    last_err = None
    t0 = time.time()
    for attempt in range(1, retries + 1):
        try:
            resp = _call_bridge(messages, model=model, timeout=timeout, endpoint=endpoint)
            latency = time.time() - t0
            status = 'ok' if not (isinstance(resp, str) and resp.startswith('ERR')) else 'error'
            _emit_moe_metric({'event': 'expert_call', 'expert': expert, 'attempt': attempt, 'latency': latency, 'status': status})
            if status == 'ok':
                return resp
            last_err = resp
        except Exception as e:
            last_err = e
        if attempt < retries:
            time.sleep(min(backoff_base * (2 ** (attempt - 1)), 10))
    return f'ERR: expert_{expert}_failed_after_retries: {last_err}'


def _aggregate(results: dict[str, str], user_text: str, timeout: int = 60, model_map: dict | None = None) -> str:
    # Compose an aggregator prompt that includes all expert outputs labeled
    parts = ['You are an aggregator. Synthesize the following expert outputs. Be concise and include provenance where available.']
    labeled = []
    for name, out in results.items():
        labeled.append(f'--- EXPERT: {name} ---\n{out}\n')
    body = '\n\n'.join(labeled)
    messages = [{'role': 'system', 'content': EXPERTS['aggregator']['system']}, {'role': 'user', 'content': user_text + '\n\n' + body}]
    model = None
    endpoint = None
    if isinstance(model_map, dict):
        mval = model_map.get('aggregator') or model_map.get('default')
        if isinstance(mval, dict):
            model = mval.get('model') or None
            endpoint = mval.get('endpoint') or None
        elif isinstance(mval, str):
            model = mval
    if not model:
        model = EXPERTS['aggregator'].get('model')
    t0 = time.time()
    out = _call_bridge(messages, model=model, timeout=timeout, endpoint=endpoint)
    latency = time.time() - t0
    _emit_moe_metric({'event': 'aggregate', 'latency': latency, 'num_experts': len(results)})
    return out


def _emit_moe_metric(data: dict) -> None:
    """Append a JSON-line metric to ~/.mcp-ai/logs/moe_metrics.jsonl"""
    try:
        home = os.path.expanduser('~')
        logdir = os.path.join(home, '.mcp-ai', 'logs')
        os.makedirs(logdir, exist_ok=True)
        path = os.path.join(logdir, 'moe_metrics.jsonl')
        entry = {'ts': time.time()}
        entry.update(data)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry) + '\n')
    except Exception:
        pass

    # Prometheus metrics (optional) + optional pushgateway support
    try:
        global _PROM_ENABLED, _PROM_METRICS
        if not _PROM_ENABLED:
            if _PROM_AVAILABLE and os.environ.get('MOE_PROMETHEUS', '').lower() in ('1', 'true', 'yes'):
                try:
                    port = int(os.environ.get('MOE_PROMETHEUS_PORT', '9321'))
                    start_http_server(port)
                    _PROM_METRICS['expert_calls'] = Counter('moe_expert_calls_total', 'MoE expert calls', ['expert', 'status'])
                    _PROM_METRICS['expert_latency'] = Summary('moe_expert_latency_seconds', 'MoE expert latency', ['expert'])
                    _PROM_METRICS['aggregate_latency'] = Summary('moe_aggregate_latency_seconds', 'MoE aggregation latency')
                    _PROM_ENABLED = True
                except Exception:
                    _PROM_ENABLED = False
        if _PROM_ENABLED:
            evt = data.get('event')
            if evt == 'expert_call':
                expert = str(data.get('expert', 'unknown'))
                status = str(data.get('status', ''))
                latency = float(data.get('latency') or 0.0)
                try:
                    _PROM_METRICS['expert_calls'].labels(expert=expert, status=status).inc()
                    _PROM_METRICS['expert_latency'].labels(expert=expert).observe(latency)
                except Exception:
                    pass
            if evt == 'aggregate':
                latency = float(data.get('latency') or 0.0)
                try:
                    _PROM_METRICS['aggregate_latency'].observe(latency)
                except Exception:
                    pass

        # Pushgateway support: push current registry to configured gateway URL
        push_url = os.environ.get('MOE_PUSHGATEWAY_URL') or os.environ.get('MOE_PUSHGATEWAY')
        if _PROM_ENABLED and push_url:
            try:
                from prometheus_client import push_to_gateway, pushadd_to_gateway, REGISTRY
                import socket as _socket
                job = os.environ.get('MOE_PUSHGATEWAY_JOB', 'mcp_moe')
                mode = os.environ.get('MOE_PUSHGATEWAY_MODE', 'pushadd').lower()
                grouping = {'instance': _socket.gethostname()}
                if mode == 'push':
                    push_to_gateway(push_url, job=job, registry=REGISTRY, grouping_key=grouping)
                else:
                    pushadd_to_gateway(push_url, job=job, registry=REGISTRY, grouping_key=grouping)
            except Exception:
                # best-effort: do not break main flow on push errors
                pass
    except Exception:
        pass


def _add_profile(name: str, kws: str | list[str]) -> bool:
    """Add or update a profile (name -> keywords). Rebuilds index and persists it.

    `kws` may be a space-separated string or a list of keywords.
    Returns True on success.
    """
    try:
        global EXPERT_PROFILES, _PROFILE_EMBEDS, _INDEX_TYPE
        if isinstance(kws, str):
            kws_list = [t for t in kws.split() if t]
        else:
            kws_list = list(kws)
        EXPERT_PROFILES[name] = kws_list
        # ensure embedding model and compute embedding
        if not _ensure_embedding_model():
            # still update profiles; index build may fail later
            _PROFILE_EMBEDS[name] = None
        else:
            ptext = ' '.join(kws_list)
            try:
                import numpy as _np
                vec = _EMBED_MODEL.encode(ptext, convert_to_numpy=True, normalize_embeddings=True)
                _PROFILE_EMBEDS[name] = _np.array(vec, dtype='float32')
            except Exception:
                vec = _EMBED_MODEL.encode(ptext, convert_to_numpy=True, normalize_embeddings=True)
                _PROFILE_EMBEDS[name] = vec

        # Rebuild index and persist
        ok = _build_profile_index()
        return bool(ok)
    except Exception:
        return False


def _remove_profile(name: str) -> bool:
    """Remove a profile by name and rebuild/persist index. Returns True on success."""
    try:
        global EXPERT_PROFILES, _PROFILE_EMBEDS
        if name in EXPERT_PROFILES:
            EXPERT_PROFILES.pop(name, None)
        if name in _PROFILE_EMBEDS:
            _PROFILE_EMBEDS.pop(name, None)
        # Rebuild index and persist
        ok = _build_profile_index()
        return bool(ok)
    except Exception:
        return False


def moe_call(text: str, rag_context: str | None = None, debug: bool = False, task_profile: str | None = None, model_map: dict | None = None, mode: str = 'auto') -> str | None:
    """Main entrypoint used by HAL.

    Returns aggregated string on success, or None to signal HAL should fallback to standard call.
    """
    try:
        # Choose experts according to mode (auto/rule/embedding/llm)
        chosen_mode = (mode or 'auto').lower()
        if chosen_mode == 'embedding':
            experts = _choose_experts_embedding(text)
        elif chosen_mode == 'rule':
            experts = _choose_experts_rule(text)
        else:
            # auto: prefer embedding for longer queries, otherwise rule
            if len((text or '').split()) >= 5:
                experts = _choose_experts_embedding(text)
            else:
                experts = _choose_experts_rule(text)

        if debug:
            logger.info('[moe_router] chosen experts: %s', experts)

        results: dict[str, str] = {}
        # Run experts with bounded concurrency and per-expert timeouts
        import concurrent.futures
        max_workers = int(os.environ.get('MOE_MAX_WORKERS', '2'))
        per_expert_timeout = int(os.environ.get('MOE_EXPERT_TIMEOUT', '20'))
        master_timeout = per_expert_timeout * max(1, len(experts)) + 5
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures_map: dict[concurrent.futures.Future, str] = {}
            for e in experts:
                fut = ex.submit(_run_expert, e, text, rag_context, per_expert_timeout, model_map)
                futures_map[fut] = e

            try:
                for fut in concurrent.futures.as_completed(futures_map.keys(), timeout=master_timeout):
                    name = futures_map.get(fut)
                    try:
                        out = fut.result()
                    except Exception as exc:
                        out = f'ERR: expert_{name}_failed: {exc}'
                        logger.warning('Expert %s failed: %s', name, exc)
                    results[name] = out
                    if debug:
                        logger.info('[moe_router] expert=%s len=%d', name, len(out) if isinstance(out, str) else 0)
            except concurrent.futures.TimeoutError:
                # Cancel remaining futures and record timeout errors
                logger.warning('MoE experts timed out after %ds', master_timeout)
                for fut, name in list(futures_map.items()):
                    if not fut.done():
                        fut.cancel()
                        results[name] = f'ERR: expert_{name}_timed_out'

        # Also get a generalist fallback if not included
        if 'general' not in results:
            try:
                general_fut = None
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex2:
                    general_fut = ex2.submit(_run_expert, 'general', text, rag_context, per_expert_timeout, model_map)
                    results['general'] = general_fut.result(timeout=per_expert_timeout)
            except Exception as exc:
                results['general'] = f'ERR: general_failed: {exc}'
                logger.warning('General expert failed: %s', exc)

        agg = _aggregate(results, text, model_map=model_map)
        return agg
    except Exception as exc:
        logger.exception('Unexpected MoE error: %s', exc)
        return None


if __name__ == '__main__':
    # simple CLI for local testing
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('text', nargs='+')
    p.add_argument('--debug', action='store_true')
    args = p.parse_args()
    print(moe_call(' '.join(args.text), debug=args.debug))
