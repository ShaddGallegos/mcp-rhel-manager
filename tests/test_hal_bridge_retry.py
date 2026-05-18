import os
import types

import pytest

import importlib


def test_call_bridge_retries(monkeypatch):
    # Reload module to ensure our env vars are read fresh
    os.environ['HAL_BRIDGE_MAX_RETRIES'] = '2'
    os.environ['HAL_BRIDGE_BACKOFF_BASE'] = '0.01'
    import scripts.hal as hal
    importlib.reload(hal)

    calls = {'count': 0}

    class DummyResp:
        def __init__(self, text):
            self.text = text

    def fake_post(url, json=None, timeout=None):
        calls['count'] += 1
        if calls['count'] < 3:
            raise RuntimeError('simulated transient failure')
        return DummyResp('{"content":"ok"}')

    monkeypatch.setattr(hal, 'requests', types.SimpleNamespace(post=fake_post))

    res = hal.call_bridge('hello', timeout=1)
    assert 'ok' in res or 'ok' in getattr(res, 'text', '')
    assert calls['count'] >= 3
