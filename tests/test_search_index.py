import json
import tempfile
from pathlib import Path

import scripts.reindex_embeddings as reidx
import scripts.search_index as sidx


def test_search_index_roundtrip(tmp_path):
    td = tmp_path / 'training'
    td.mkdir()
    (td / 'a.txt').write_text('Database scaling and cloud migrations are critical for reliability')
    out = tmp_path / 'idx.jsonl'
    reidx.build_index(str(td), str(out), dim=8)
    results = sidx.search_index(str(out), 'cloud migration', top_k=3, dim=8)
    assert isinstance(results, list)
    assert len(results) >= 1
    assert 'id' in results[0]
