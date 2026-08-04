import importlib.util
import sys

import pytest


@pytest.mark.parametrize(
    "query",
    [
        "how do I patch with ansible automation platform",
        "migration strategy for migrating ansible 2.5 to 2.6",
        "are you back with me now",
    ],
)
def test_hal_main_smoke_routes_no_nameerror(monkeypatch, query):
    spec = importlib.util.spec_from_file_location('hal_smoke_test', 'scripts/hal.py')
    hal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hal)

    # Keep the smoke test deterministic and avoid file writes/network use.
    monkeypatch.setattr(hal, "write_interaction", lambda *_a, **_k: "/tmp/hal-smoke.jsonl")
    monkeypatch.setattr(hal, "call_bridge", lambda *_a, **_k: '{"content":"ok"}')

    monkeypatch.setattr(sys, "argv", ["hal.py", query])

    try:
        hal.main()
    except SystemExit as exc:
        # main() exits intentionally for many routed commands.
        assert exc.code in (0, None)
    except NameError as exc:
        pytest.fail(f"NameError during HAL routing for query {query!r}: {exc}")


def test_hal_wellbeing_query_no_nameerror(monkeypatch):
    spec = importlib.util.spec_from_file_location('hal_smoke_test_well', 'scripts/hal.py')
    hal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hal)

    monkeypatch.setattr(hal, "write_interaction", lambda *_a, **_k: "/tmp/hal-smoke-well.jsonl")
    monkeypatch.setattr(hal, "call_bridge", lambda *_a, **_k: '{"content":"I am okay."}')
    monkeypatch.setattr(sys, "argv", ["hal.py", "how are you today"])

    try:
        hal.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    except NameError as exc:
        pytest.fail(f"NameError during HAL wellbeing route: {exc}")


def test_hal_doctor_route_smoke(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('hal_smoke_test_doctor', 'scripts/hal.py')
    hal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hal)

    monkeypatch.setattr(hal, "_run_doctor_report", lambda: ("doctor ok\n", 0))
    monkeypatch.setattr(sys, "argv", ["hal.py", "--doctor"])

    with pytest.raises(SystemExit) as exc:
        hal.main()

    out = capsys.readouterr().out
    assert exc.value.code == 0
    assert "doctor ok" in out
