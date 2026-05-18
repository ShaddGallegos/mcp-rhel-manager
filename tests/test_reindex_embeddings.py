import tempfile
import os
import json

import scripts.reindex_embeddings as reidx


def test_build_index(tmp_path):
    td = tmp_path / "training"
    td.mkdir()
    f = td / "sample.txt"
    f.write_text("This is a sample training record about databases and cloud.")
    out = tmp_path / "index.jsonl"
    reidx.build_index(str(td), str(out), dim=8)
    assert out.exists()
    lines = out.read_text().splitlines()
    assert len(lines) == 1
    j = json.loads(lines[0])
    assert 'id' in j and 'vector' in j
    assert len(j['vector']) == 8
