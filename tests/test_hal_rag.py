import os
import json
import importlib.util
import textwrap


def load_hal_module(path='scripts/hal.py'):
    spec = importlib.util.spec_from_file_location('hal_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_search_training_data_for_rag(tmp_path):
    train_dir = tmp_path / 'training'
    train_dir.mkdir()
    # create a supplemental_document record
    rec = {
        'type': 'supplemental_document',
        'source_name': 'test-doc',
        'text': 'This document discusses widgets, gadgets, and example company use.'
    }
    (train_dir / 'sample.json').write_text(json.dumps(rec))

    os.environ['HAL_TRAIN_DIR'] = str(train_dir)
    os.environ['HAL_RAG_ALWAYS'] = '1'
    os.environ['HAL_RAG_FALLBACK'] = '1'

    hal = load_hal_module()

    out = hal.search_training_data_for_rag('widgets')
    assert out is not None
    assert 'widgets' in out.lower() or 'test-doc' in out.lower()


def test_call_bridge_fallback_uses_training(tmp_path):
    train_dir = tmp_path / 'training'
    train_dir.mkdir()
    rec = {
        'type': 'supplemental_document',
        'source_name': 'fallback-doc',
        'text': 'Fallback content about foobar and bridge testing.'
    }
    (train_dir / 'fallback.json').write_text(json.dumps(rec))

    os.environ['HAL_TRAIN_DIR'] = str(train_dir)
    os.environ['HAL_RAG_ALWAYS'] = '1'
    os.environ['HAL_RAG_FALLBACK'] = '1'

    hal = load_hal_module()

    # Force the requests path to raise so call_bridge exercises fallback logic.
    class BadRequests:
        def post(self, *a, **k):
            raise RuntimeError('simulated bridge failure')

    hal.requests = BadRequests()

    res = hal.call_bridge('foobar')
    assert isinstance(res, str)
    assert 'Bridge error' in res or 'training-data' in res or 'Found in your training data' in res
