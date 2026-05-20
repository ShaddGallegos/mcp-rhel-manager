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
from typing import Iterable, Iterator, List, Optional

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

logger = logging.getLogger('llm_client')


class LLMClient:
    def __init__(self, endpoints: Optional[Iterable[str]] = None, timeout: int = 60, max_retries: int = 3, backoff_factor: float = 0.5, pool_maxsize: int = 10):
        self.timeout = int(os.environ.get('MCP_AI_TIMEOUT_SEC', str(timeout)))
        self.endpoints = list(endpoints or [])
        # Fallback to env OLLAMA_URL(S)
        if not self.endpoints:
            env_list = os.environ.get('OLLAMA_URLS')
            if env_list:
                self.endpoints = [u.strip() for u in env_list.split(',') if u.strip()]
            else:
                primary = os.environ.get('OLLAMA_URL') or os.environ.get('MCP_OLLAMA_URL')
                if primary:
                    self.endpoints = [primary]

        # Ensure requests available
        if requests is None:
            raise RuntimeError('requests library not available')

        self.session = requests.Session()
        # Configure retries and connection pool
        retries = Retry(total=max_retries, read=max_retries, connect=max_retries, backoff_factor=backoff_factor, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(['GET', 'POST']))
        adapter = HTTPAdapter(pool_connections=pool_maxsize, pool_maxsize=pool_maxsize, max_retries=retries)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

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
                with open(self._metrics_path, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(entry) + '\n')
        except Exception:
            pass

    def _iter_endpoints(self) -> Iterable[str]:
        # yield endpoints in order, prefer configured list
        for e in self.endpoints:
            yield e

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
            try:
                self._record_metric(ep, 'error', latency, error=str(e))
            except Exception:
                pass
            raise

    def call_with_failover(self, payload: dict, stream: bool = False, timeout: Optional[int] = None, headers: Optional[dict] = None) -> Optional[object]:
        """Try each configured endpoint in order until one succeeds.

        Returns response text (or iterator if stream=True), or raises the last exception.
        """
        last_err = None
        for ep in self._iter_endpoints():
            try:
                return self.call(payload, stream=stream, timeout=timeout, headers=headers, endpoint=ep)
            except Exception as e:
                last_err = e
                logger.warning('LLM endpoint %s failed: %s', ep, e)
                # already recorded per-call metric in call(); small backoff before next candidate
                try:
                    time.sleep(0.2)
                except Exception:
                    pass
                continue
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
