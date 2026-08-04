"""Centralized LLM HTTP client with connection pooling, retries and failover.

Provides a lightweight wrapper around `requests.Session` to call one or more
LLM endpoints (local Ollama bridge, remote bridges). Features:
- connection pooling and keep-alive
- configurable retry/backoff via urllib3 Retry
- endpoint failover (try candidates in order)
- optional streaming support (generator yielding bytes)

This module is intended to replace ad-hoc urllib usage across the codebase
and improve latency and reliability by reusing TCP connections and having
robust retries.
"""
from __future__ import annotations

import json
import os
import time
import logging
import threading
from typing import Iterable, List, Optional
import mcp_ai_config as config

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util import Retry
except Exception:
    requests = None

try:
    from prometheus_client import Counter, Summary
    _PROM_AVAILABLE = True
except Exception:
    _PROM_AVAILABLE = False
try:
    import ingest_common
    _HAS_INGEST_COMMON = True
except Exception:
    _HAS_INGEST_COMMON = False

logger = logging.getLogger('llm_client')


class LLMClient:
    def __init__(self, endpoints: Optional[Iterable[str]] = None, timeout: int = 60, max_retries: int = 3, backoff_factor: float = 0.5, pool_maxsize: int = 10):
        self.timeout = int(os.environ.get('MCP_AI_TIMEOUT_SEC', str(timeout)))
        self.max_retries = int(os.environ.get('MCP_AI_CLIENT_RETRIES', str(max_retries)))
        self.endpoints = list(endpoints or [])
        # Fallback to env OLLAMA_URL(S)
        if not self.endpoints:
            # Allow environment or config-file driven list first
            env_list = os.environ.get('OLLAMA_URLS') or config.get_config('OLLAMA_URLS')
            if env_list:
                self.endpoints = [u.strip() for u in env_list.split(',') if u.strip()]
            else:
                # Use configured ollama base URL and normalize to /api/chat
                primary = config.get_config('ollama_url') or config.get_ollama_url()
                if primary:
                    if primary.endswith('/api/chat'):
                        self.endpoints = [primary]
                    else:
                        self.endpoints = [f"{primary.rstrip('/')}/api/chat"]

        # Ensure requests available
        if requests is None:
            raise RuntimeError('requests library not available')

        self.session = requests.Session()
        # Configure retries and connection pool
        retries = Retry(total=max_retries, read=max_retries, connect=max_retries, backoff_factor=backoff_factor, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(['GET', 'POST']))
        adapter = HTTPAdapter(pool_connections=pool_maxsize, pool_maxsize=pool_maxsize, max_retries=retries)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

        # Endpoint circuit-breaker settings
        self._cb_fail_threshold = int(os.environ.get('MCP_AI_CB_FAIL_THRESHOLD', '3'))
        self._cb_open_sec = float(os.environ.get('MCP_AI_CB_OPEN_SEC', '30'))
        self._endpoint_state = {
            ep: {'failures': 0, 'open_until': 0.0, 'last_error': ''} for ep in self.endpoints
        }

        # Basic runtime stats for health/observability
        self._stats_lock = threading.Lock()
        self._stats = {
            'calls_total': 0,
            'calls_success': 0,
            'calls_error': 0,
            'failover_hops': 0,
            'last_latency_sec': 0.0,
            'last_error': '',
            'last_success_model': '',
        }

        # Metrics (Prometheus optional + local JSONL)
        self._prom = {}
        if _PROM_AVAILABLE and os.environ.get('LLM_PROMETHEUS', '').lower() in ('1', 'true', 'yes'):
            try:
                self._prom['calls'] = Counter('llm_client_calls_total', 'LLM client calls', ['endpoint', 'status'])
                self._prom['latency'] = Summary('llm_client_latency_seconds', 'LLM client latency', ['endpoint'])
            except Exception:
                self._prom = {}

        # JSONL metrics file (always available as fallback)
        try:
            home = os.path.expanduser('~')
            logdir = os.path.join(home, '.mcp-ai', 'logs')
            os.makedirs(logdir, exist_ok=True)
            self._metrics_path = os.path.join(logdir, 'llm_client_metrics.jsonl')
        except Exception:
            self._metrics_path = None

    def _record_metric(self, endpoint: str, status: str, latency: float, error: Optional[str] = None) -> None:
        # Prometheus
        try:
            if self._prom.get('calls'):
                try:
                    self._prom['calls'].labels(endpoint=endpoint or 'unknown', status=status).inc()
                except Exception:
                    pass
            if self._prom.get('latency') and status == 'success':
                try:
                    self._prom['latency'].labels(endpoint=endpoint or 'unknown').observe(latency)
                except Exception:
                    pass
        except Exception:
            pass

        # JSONL log
        try:
            if self._metrics_path:
                entry = {'ts': time.time(), 'endpoint': endpoint, 'status': status, 'latency': latency}
                if error:
                    entry['error'] = str(error)
                if _HAS_INGEST_COMMON:
                    try:
                        ingest_common.append_jsonl(self._metrics_path, entry)
                    except Exception:
                        with open(self._metrics_path, 'a', encoding='utf-8') as fh:
                            fh.write(json.dumps(entry) + '\n')
                else:
                    with open(self._metrics_path, 'a', encoding='utf-8') as fh:
                        fh.write(json.dumps(entry) + '\n')
        except Exception:
            pass

    def _iter_endpoints(self) -> Iterable[str]:
        # yield endpoints in order, skipping open-circuit entries
        now = time.time()
        for e in self.endpoints:
            state = self._endpoint_state.get(e, {})
            if float(state.get('open_until', 0.0) or 0.0) > now:
                continue
            yield e

    def _mark_endpoint_success(self, endpoint: str, model: str = '') -> None:
        st = self._endpoint_state.setdefault(endpoint, {'failures': 0, 'open_until': 0.0, 'last_error': ''})
        st['failures'] = 0
        st['open_until'] = 0.0
        st['last_error'] = ''
        with self._stats_lock:
            self._stats['calls_total'] += 1
            self._stats['calls_success'] += 1
            if model:
                self._stats['last_success_model'] = model

    def _mark_endpoint_failure(self, endpoint: str, err: Exception) -> None:
        st = self._endpoint_state.setdefault(endpoint, {'failures': 0, 'open_until': 0.0, 'last_error': ''})
        st['failures'] = int(st.get('failures', 0) or 0) + 1
        st['last_error'] = str(err)
        if st['failures'] >= self._cb_fail_threshold:
            st['open_until'] = time.time() + self._cb_open_sec
        with self._stats_lock:
            self._stats['calls_total'] += 1
            self._stats['calls_error'] += 1
            self._stats['last_error'] = str(err)

    def route_model(self, task: str = 'default', payload: Optional[dict] = None) -> str:
        """Pick an LLM model using centralized task routing and request hints."""
        chosen_task = (task or 'default').strip().lower() or 'default'
        try:
            if payload and isinstance(payload, dict):
                msgs = payload.get('messages')
                if isinstance(msgs, list):
                    text = ' '.join(str(m.get('content', '')) for m in msgs if isinstance(m, dict)).lower()
                    if any(k in text for k in ('traceback', 'exception', 'kernel', 'diagnostic', 'failed', 'error')):
                        chosen_task = 'diagnostics'
                    elif any(k in text for k in ('refactor', 'python', 'code', 'function', 'class')):
                        chosen_task = 'code'
        except Exception:
            pass
        return config.pick_model_for_task(chosen_task)

    def get_stats_snapshot(self) -> dict:
        """Return a serializable health/stats snapshot for dashboards/health checks."""
        with self._stats_lock:
            stats = dict(self._stats)
        endpoints = []
        now = time.time()
        for ep in self.endpoints:
            st = self._endpoint_state.get(ep, {})
            endpoints.append({
                'endpoint': ep,
                'failures': int(st.get('failures', 0) or 0),
                'circuit_open': float(st.get('open_until', 0.0) or 0.0) > now,
                'open_for_sec': max(0.0, float(st.get('open_until', 0.0) or 0.0) - now),
                'last_error': st.get('last_error', ''),
            })
        stats['endpoints'] = endpoints
        stats['model_routing'] = config.get_model_routing_map()
        return stats

    def call(self, payload: dict, stream: bool = False, timeout: Optional[int] = None, headers: Optional[dict] = None, endpoint: Optional[str] = None) -> Optional[object]:
        """Call a single endpoint (or the first configured) and return text or a streaming iterator.

        If `stream=True` returns an iterator yielding bytes chunks from the response.
        Otherwise returns the full response text on success.
        """
        if requests is None:
            raise RuntimeError('requests required')
        ep = endpoint or (self.endpoints[0] if self.endpoints else None)
        if not ep:
            raise RuntimeError('No LLM endpoint configured')
        to = timeout or self.timeout
        hdrs = {'Content-Type': 'application/json'}
        if headers:
            hdrs.update(headers)
        t0 = time.time()
        try:
            resp = self.session.post(ep, json=payload, timeout=to, stream=stream, headers=hdrs)
            resp.raise_for_status()
            latency = time.time() - t0
            self._mark_endpoint_success(ep, model=str(payload.get('model', '') if isinstance(payload, dict) else ''))
            with self._stats_lock:
                self._stats['last_latency_sec'] = latency
            try:
                self._record_metric(ep, 'success', latency)
            except Exception:
                pass
            if stream:
                # Return the Response object so callers can stream content and access headers/status
                return resp
            return resp.text
        except Exception as e:
            latency = time.time() - t0
            self._mark_endpoint_failure(ep, e)
            with self._stats_lock:
                self._stats['last_latency_sec'] = latency
            try:
                self._record_metric(ep, 'error', latency, error=str(e))
            except Exception:
                pass
            raise

    def call_task(self, task: str, payload: dict, stream: bool = False, timeout: Optional[int] = None, headers: Optional[dict] = None) -> Optional[object]:
        """Route model by task and execute with failover/circuit-breaker semantics."""
        if isinstance(payload, dict) and not payload.get('model'):
            payload = dict(payload)
            payload['model'] = self.route_model(task=task, payload=payload)
        return self.call_with_failover(payload, stream=stream, timeout=timeout, headers=headers)

    def call_with_failover(self, payload: dict, stream: bool = False, timeout: Optional[int] = None, headers: Optional[dict] = None) -> Optional[object]:
        """Try each configured endpoint in order until one succeeds.

        Returns response text (or iterator if stream=True), or raises the last exception.
        """
        last_err = None
        endpoints = list(self._iter_endpoints())
        if not endpoints:
            # All endpoints are open in circuit-breaker mode; force a trial on first endpoint.
            endpoints = [self.endpoints[0]] if self.endpoints else []
        for idx, ep in enumerate(endpoints):
            for attempt in range(1, self.max_retries + 1):
                try:
                    return self.call(payload, stream=stream, timeout=timeout, headers=headers, endpoint=ep)
                except Exception as e:
                    last_err = e
                    logger.warning('LLM endpoint %s failed (attempt %d/%d): %s', ep, attempt, self.max_retries, e)
                    if attempt < self.max_retries:
                        try:
                            time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
                        except Exception:
                            pass
                        continue
                    break
            if idx < len(endpoints) - 1:
                with self._stats_lock:
                    self._stats['failover_hops'] += 1
                try:
                    time.sleep(0.2)
                except Exception:
                    pass
        if last_err:
            # record a global failure metric (no endpoint)
            try:
                self._record_metric('', 'failover_error', 0.0, error=str(last_err))
            except Exception:
                pass
            raise last_err
        return None

    def health(self) -> List[dict]:
        """Return health info for each endpoint (best-effort)."""
        out = []
        for ep in self._iter_endpoints():
            try:
                # prefer /health on bridge if available
                hurl = ep
                if hurl.endswith('/api/chat'):
                    # bridge exposes /health on same host/port
                    hurl = hurl.replace('/api/chat', '/health')
                r = self.session.get(hurl, timeout=3)
                out.append({'endpoint': ep, 'status': r.status_code, 'ok': r.ok})
            except Exception as e:
                out.append({'endpoint': ep, 'status': None, 'ok': False, 'error': str(e)})
        return out


# module-level default client (lazy init)
_DEFAULT_CLIENT: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = LLMClient()
    return _DEFAULT_CLIENT
