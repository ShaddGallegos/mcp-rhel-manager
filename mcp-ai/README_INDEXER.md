MCP AI System Indexer
=====================

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

Purpose
-------

The system indexer discovers local logs, installed packages, services, common binaries, and library/plugin folders and writes a single JSONL record suitable for ingestion as supplemental training data.

Where it writes
---------------

By default the indexer writes to the system training directory:

/var/lib/mcp/training/system-index-YYYYMMDDTHHMMSSZ.jsonl

Usage
-----

Run once manually as the `mcp-ai` user:

```
sudo -u mcp-ai /opt/mcp-rhel-manager/venv/bin/python /var/lib/mcp/indexer.py --outdir /var/lib/mcp/training
```

Or use the provided systemd timer (installed by `architect_genesis.sh`):

```
sudo systemctl start mcp-ai-indexer.service
sudo systemctl enable --now mcp-ai-indexer.timer
```

Output format
-------------

Each JSONL line is a single JSON object with these top-level keys:

- `type`: `system_index`
- `meta`: host metadata (`hostname`, `timestamp`, `os_release`, `uname`)
- `data`: discovered artifacts (`logs`, `packages_rpm`, `pip_for_python`, `services`, `binaries`, `libraries`)
- `_sha256`: stable sha256 hash of the full record (used for deduplication)

Security and redaction
----------------------

Indexer output may contain sensitive local information (logs, paths, package lists). Always run `redact_training.py` before merging or ingesting an index file into training. Example:

```
sudo -u mcp-ai /opt/mcp-rhel-manager/venv/bin/python /opt/mcp-rhel-manager/mcp-ai/redact_training.py \
  --infile /var/lib/mcp/training/system-index-...jsonl \
  --outfile /var/lib/mcp/training/system-index-...-redacted.jsonl
```

Configuration
-------------

`indexer.py` accepts `--outdir`, `--prefix`, `--limit-logs` and `--max-depth` to tune scan size and depth.

Notes
-----

- The indexer is intentionally conservative and fast — it samples common locations and limits depth. Adjust flags for deeper scans.
- Files written by the indexer are owned by `mcp-ai` when possible; the systemd unit runs the indexer as `mcp-ai`.
