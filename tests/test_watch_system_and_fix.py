import argparse
import importlib.util
import os
import sys


def _load_watcher_module():
    # Load by path to be robust when tests are invoked from repo root
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    script_path = os.path.join(repo_root, 'scripts', 'watch_system_and_fix.py')
    spec = importlib.util.spec_from_file_location('watch_system_and_fix', script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


def test_process_line_service_restart(monkeypatch):
    # load module
    w = _load_watcher_module()

    called = {}

    def fake_attempt(unit, dry_run=False):
        called['unit'] = unit
        called['dry'] = dry_run
        return {'unit': unit, 'attempted': 'yes', 'result': 'ok', 'output': 'restarted'}

    monkeypatch.setattr(w, 'attempt_restart_unit', fake_attempt)

    recorded = []

    def fake_send(hal_mod, message, title, severity):
        recorded.append({'title': title, 'severity': severity, 'message': message})

    monkeypatch.setattr(w, 'send_notification', fake_send)

    state = {'recent': []}
    args = argparse.Namespace(dry_run=False)

    line = "Job for foo.service failed: Unit entered failed state"
    w.process_line(line, state, None, args)

    assert called.get('unit') in ('foo.service', 'foo')
    assert recorded, "send_notification should have been called"


def test_process_line_io_schedules_smart(monkeypatch, tmp_path):
    w = _load_watcher_module()

    # simulate a single disk
    monkeypatch.setattr(w, 'list_disks', lambda: ['sda'])
    # make smart report a failure
    monkeypatch.setattr(w, 'smart_health', lambda dev: 'FAILED')

    scheduled = []

    def fake_schedule(dev, dry_run=False):
        scheduled.append((dev, dry_run))
        return {'device': dev, 'scheduled': 'short'}

    monkeypatch.setattr(w, 'schedule_short_smart_test', fake_schedule)

    notifications = []
    monkeypatch.setattr(w, 'send_notification', lambda *a, **k: notifications.append(a))

    state = {'recent': []}
    args = argparse.Namespace(dry_run=True)

    line = "buffer I/O error on device /dev/sda"
    w.process_line(line, state, None, args)

    assert scheduled, "SMART test should have been scheduled"
    assert notifications, "Notification should have been sent"
