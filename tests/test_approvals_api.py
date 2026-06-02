import os
import sys
import json
from pathlib import Path


def test_write_approval_and_api(tmp_path: Path):
    # Use temporary HOME so approvals are written under tmp
    os.environ['HOME'] = str(tmp_path)

    # Ensure mcp-ai modules are importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp-ai'))

    # Import fresh approvals_api module
    if 'approvals_api' in sys.modules:
        del sys.modules['approvals_api']
    import approvals_api

    # Test direct writer
    out = approvals_api.write_approval({'plan': 'plan-unit-test.json', 'message': 'ok'})
    assert Path(out).exists()
    with open(out, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    assert data.get('message') == 'ok'

    # Test HTTP endpoint via Flask test client if available
    if getattr(approvals_api, 'app', None) is not None:
        client = approvals_api.app.test_client()
        resp = client.post('/api/approve', json={'plan': 'plan-http.json', 'message': 'hello'})
        assert resp.status_code == 200
        j = resp.get_json()
        assert j.get('ok') is True
    else:
        import pytest
        pytest.skip('Flask not available; skipping HTTP endpoint test')
