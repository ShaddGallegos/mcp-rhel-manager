import importlib.util
import tempfile
import os
from pathlib import Path
import pytest


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gguf_converter_detection():
    mod = load_module('mcp-ai/gguf_converter.py', 'gguf_converter')
    tool = mod.find_converter()
    if not tool:
        pytest.skip('No GGUF converter installed')
    # If a tool exists, try a tiny conversion run (it may still fail on malformed input)
    with tempfile.NamedTemporaryFile(delete=False, suffix='.jsonl') as fh:
        fh.write(b'{"id":"t","text":"sample"}\n')
        fh.flush()
        src = fh.name
    out = Path(src).with_suffix('.gguf')
    try:
        res = mod.convert(src, out)
        assert res is None or res.exists()
    finally:
        try:
            Path(src).unlink()
        except Exception:
            pass
        try:
            out.unlink()
        except Exception:
            pass
