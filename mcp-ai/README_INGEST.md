Supplemental Training Data — Ingestion Tools
===========================================

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

This folder contains small helper scripts to ingest supplemental training data into `~/.mcp-ai/training`.

Scripts

- `ingest_urls.py` — crawl URLs and write per-URL JSON files (already present).
- `ingest_history.py` — detect the latest Copilot chat transcript and write sanitized JSONL entries.
- `ingest_artifacts.py` — collect repository artifacts (fix plans, patches, generated scripts) and write JSONL.
- `ingest_documents.py` — import local files (txt/csv/tsv/json/md/log/xml/html), spreadsheets (`.xlsx/.xls/.ods`), and optional PDF/DOCX into training JSON records.
- `merge_supplemental.py` — merge `supplemental-*.jsonl` files into a single deduplicated JSONL.
- `training_crypto.py` — encrypt/decrypt local training files with a password-derived key.

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

Import local files/directories into training data:

```bash
python3 mcp-ai/ingest_documents.py --recursive ~/Documents/my-notes ~/datasets/training.csv
```

Encrypt local training data at rest:

```bash
python3 mcp-ai/training_crypto.py encrypt --recursive --indir ~/.mcp-ai/training
```

Decrypt local training data when needed:

```bash
python3 mcp-ai/training_crypto.py decrypt --recursive --indir ~/.mcp-ai/training
```

Notes & safety

- Files are lightly redacted (private key blocks, inline secrets). Review the resulting JSONL before using it to train models.
- These scripts write into `~/.mcp-ai/training` by default. Ensure you have adequate disk space and backup if necessary.
- Use `--dry-run` (where supported) to preview actions.
- `training_crypto.py` prompts for a password unless `HAL_TRAINING_KEY` is set in your shell environment.
- This repository now ignores common local training paths and encrypted files via `.gitignore` to help prevent accidental git commits.

Next steps

- Add these steps to your project `CHECKLIST.md` or deployment docs if you want ingestion to be part of your release workflow.
