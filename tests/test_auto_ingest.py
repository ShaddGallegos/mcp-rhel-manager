import sys
from pathlib import Path

# ensure mcp-ai is available on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-ai"))
import auto_ingest_training as ait


def test_module_imports_and_tracker():
    assert callable(ait.auto_ingest)
    tracker = ait.load_import_tracker()
    assert isinstance(tracker, dict)
