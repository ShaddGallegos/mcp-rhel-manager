import os
import importlib


def test_log_and_eval(monkeypatch, tmp_path):
    # prepare module and override REPORTS_DIR to tmp
    import scripts.hal as hal
    importlib.reload(hal)
    hal.REPORTS_DIR = str(tmp_path)

    captured = []

    def fake_log(entry):
        captured.append(entry)

    monkeypatch.setattr(hal, '_log_interaction', fake_log)

    # monkeypatch call_bridge to return a deterministic response
    def fake_call_bridge(prompt, **kwargs):
        return f"This is an answer for: {prompt} -- contains KEYWORD"

    monkeypatch.setattr(hal, 'call_bridge', fake_call_bridge)

    prompts = ["hello world", "another prompt"]
    res = hal.evaluate_prompts(prompts, expected_keywords=["keyword"], max_examples=5)

    assert res['total'] == 2
    assert res['ok'] == 2
    assert res['keyword_hits'] == 2
    assert len(captured) == 2
