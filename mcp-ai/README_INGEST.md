Supplemental Training Data — Ingestion Tools
===========================================

This folder contains small helper scripts to ingest supplemental training data into `~/.mcp-ai/training`.

Scripts
- `ingest_urls.py` — crawl URLs and write per-URL JSON files (already present).
- `ingest_history.py` — detect the latest Copilot chat transcript and write sanitized JSONL entries.
- `ingest_artifacts.py` — collect repository artifacts (fix plans, patches, generated scripts) and write JSONL.
- `merge_supplemental.py` — merge `supplemental-*.jsonl` files into a single deduplicated JSONL.

Basic usage

Run the history ingester:
```bash
python3 mcp-ai/ingest_history.py --outdir ~/.mcp-ai/training --prefix supplemental-history
```

Run the artifact ingester:
```bash
python3 mcp-ai/ingest_artifacts.py --outdir ~/.mcp-ai/training --prefix supplemental-solutions
```

Merge supplemental files into a single combined file:
```bash
python3 mcp-ai/merge_supplemental.py --indir ~/.mcp-ai/training --pattern 'supplemental-*.jsonl' --outdir ~/.mcp-ai/training --prefix supplemental-combined
```

Notes & safety
- Files are lightly redacted (private key blocks, inline secrets). Review the resulting JSONL before using it to train models.
- These scripts write into `~/.mcp-ai/training` by default. Ensure you have adequate disk space and backup if necessary.
- Use `--dry-run` (where supported) to preview actions.

Next steps
- Add these steps to your project `CHECKLIST.md` or deployment docs if you want ingestion to be part of your release workflow.
