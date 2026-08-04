import os
import sys
import json
from pathlib import Path


def test_dashboard_approve_flow(tmp_path: Path):
    # Use temporary HOME so dashboard writes to tmp dirs
    os.environ['HOME'] = str(tmp_path)

    # Ensure mcp-ai modules are importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp-ai'))

    # Import approvals_api and dashboard fresh
    if 'approvals_api' in sys.modules:
        del sys.modules['approvals_api']
    if 'dashboard' in sys.modules:
        del sys.modules['dashboard']

    import approvals_api
    try:
        import dashboard
    except ModuleNotFoundError:
        import pytest
        pytest.skip('Flask not available; skipping dashboard integration test')

    # Create a fake plan file under .mcp-ai/fixes
    base = tmp_path / '.mcp-ai'
    fixes = base / 'fixes'
    fixes.mkdir(parents=True, exist_ok=True)
    plan_file = fixes / 'plan-test.json'
    plan_file.write_text(json.dumps({'title': 'test plan', 'steps': []}))

    client = dashboard.app.test_client()
    resp = client.post('/approve?file=plan-test.json', data={'message': 'approved', 'patterns': 'ls,echo'})
    assert resp.status_code in (302, 200)

    approved_file = tmp_path / '.mcp-ai' / 'approvals' / 'plan-test.approved.json'
    assert approved_file.exists()
    data = json.loads(approved_file.read_text(encoding='utf-8'))
    assert data.get('allowed_cmd_patterns') == ['ls', 'echo']
