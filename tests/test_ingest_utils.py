import hashlib
from pathlib import Path
import sys
from pathlib import Path as _P

# ensure mcp-ai is on sys.path for tests
sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "mcp-ai"))
import ingest_utils


def test_get_file_hash(tmp_path: Path):
    p = tmp_path / "foo.txt"
    p.write_text("hello world")
    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    assert ingest_utils.get_file_hash(p) == expected


def test_tracker_save_load(tmp_path: Path):
    tracker_path = tmp_path / "tracker.json"
    data = {"imported": {"a": {"hash": "x"}}, "last_run": None}
    ingest_utils.save_import_tracker(tracker_path, data)
    loaded = ingest_utils.load_import_tracker(tracker_path)
    assert isinstance(loaded, dict)
    assert loaded.get("imported") == data["imported"]
