import sys
from pathlib import Path
from tempfile import TemporaryDirectory

# ensure mcp-ai is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-ai"))

import ingest_common


def test_redact_text_basic():
    s = "password: hunter2 and token=ABC123 Bearer abc.def.ghi"
    out = ingest_common.redact_text(s)
    assert "[REDACTED]" in out or "[REDACTED_PRIVATE_KEY]" not in out
    assert "hunter2" not in out


def test_write_jsonl(tmp_path: Path):
    entries = [{"a": 1}, {"b": 2}]
    outdir = tmp_path / "out"
    path = ingest_common.write_jsonl(entries, str(outdir), "testprefix")
    assert outdir.exists()
    assert Path(path).exists()
    content = Path(path).read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 2
