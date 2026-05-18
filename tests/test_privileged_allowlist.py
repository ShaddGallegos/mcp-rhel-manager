import json
import sys


def _import_hal_with_home(tmp_home):
    import importlib
    import os
    if 'scripts.hal' in sys.modules:
        del sys.modules['scripts.hal']
    os.environ['HOME'] = str(tmp_home)
    hal = importlib.import_module('scripts.hal')
    return hal


def test_allowlist_match(tmp_path, monkeypatch):
    hal = _import_hal_with_home(tmp_path)
    # create allowlist
    ai_dir = tmp_path / '.mcp-ai'
    ai_dir.mkdir()
    allow = [
        {
            "name": "restart_service",
            "pattern": "^systemctl\\s+restart\\s+[a-zA-Z0-9_.:-]+$",
            "description": "Restart a service"
        }
    ]
    (ai_dir / 'privileged_allowlist.json').write_text(json.dumps(allow))
    allowed, matched = hal._is_command_allowed('systemctl restart sshd')
    assert allowed
    assert 'restart_service' in matched


def test_allowlist_no_match(tmp_path, monkeypatch):
    hal = _import_hal_with_home(tmp_path)
    ai_dir = tmp_path / '.mcp-ai'
    ai_dir.mkdir()
    allow = [
        {"name": "apt_install", "pattern": "^(apt|apt-get)\\s+install\\s+-y\\s+[a-zA-Z0-9_.:-]+$"}
    ]
    (ai_dir / 'privileged_allowlist.json').write_text(json.dumps(allow))
    allowed, matched = hal._is_command_allowed('rm -rf /tmp/somefile')
    assert not allowed
