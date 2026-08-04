import importlib.util
import tempfile
import shutil
from pathlib import Path
import os


def load_module(path, name):
    import sys
    # Ensure sibling modules in mcp-ai can be imported
    repo_root = os.getcwd()
    mcp_ai_dir = os.path.join(repo_root, 'mcp-ai')
    if mcp_ai_dir not in sys.path:
        sys.path.insert(0, mcp_ai_dir)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_supplemental_training_basic_run(tmp_path):
    mod = load_module('mcp-ai/supplemental_training.py', 'supplemental_training')
    # create a tiny text file to include
    src = tmp_path / 'sample.txt'
    src.write_text('This is a sample document for supplemental training.')

    out_base = tmp_path / 'out'
    out_base.mkdir()

    # call main with --paths and --no-encrypt to avoid vault requirements
    # Skip clamscan in CI/test environments
    os.environ['SUPPLEMENTAL_SKIP_CLAMS'] = '1'
    rc = mod.main(['--name', 'smoke-test', '--paths', str(src), '--outdir', str(out_base), '--no-encrypt'])
    assert rc == 0
    # verify dataset created
    outdir = Path(out_base) / 'smoke-test'
    ds = outdir / 'dataset.jsonl'
    assert ds.exists()
    content = ds.read_text(encoding='utf-8')
    assert 'sample' in content
