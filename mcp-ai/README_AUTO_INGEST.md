<!-- Automated Intelligence Data Ingestion -->

# HAL Auto-Ingest Training Data

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

Automatically import new business intelligence and documents into HAL training without manual commands.

## Overview

The `auto_ingest_training.py` script monitors watch directories and automatically imports:

- **Business Intelligence**: New/updated JSONL files from `~/GIT/Business_Tools/Training_Data/`
- **Documents**: CSV, Excel, PDF, Word docs, etc. from `~/Downloads/` and `~/Documents/`

Import history is tracked to avoid re-importing unchanged files.

## Quick Start

### Manual Trigger

```bash
# Run auto-ingest once
HAL --auto-ingest

# Check import history
HAL --ingest-status

# Reset tracker (re-import everything next run)
HAL --ingest-reset
```

### Automated via Cron (Recommended)

Add to your crontab for automatic daily ingestion at 2 AM:

```bash
crontab -e
```

Then add this line:

```cron
0 2 * * * cd <REPO_ROOT> && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py >> ~/.mcp-ai/auto_ingest.log 2>&1
```

### View Ingestion Log

```bash
# Tail live updates
tail -f ~/.mcp-ai/auto_ingest.log

# View full history
cat ~/.mcp-ai/auto_ingest.log
```

## Features

### Smart Deduplication

- Tracks file content hash and modification time
- Skips unchanged files
- Re-imports only when file is modified

### Watch Directories

Automatically scanned for new files:

- `~/GIT/Business_Tools/Training_Data/` — Business intelligence JSONL
- `~/Downloads/` — Documents and spreadsheets
- `~/Documents/` — Personal documents

Supported formats: xlsx, xls, csv, tsv, json, jsonl, txt, md, pdf, docx, doc, log, yml, yaml, xml, html, htm

### Document Age Filter

- Only imports documents modified in last 7 days
- Limits to 5 documents per run (prevent overwhelming)
- Business_Tools JSONL always imported

### Import Tracking

Tracker file: `~/.mcp-ai/training/.import_tracker.json`

Contains:

- File paths
- Content hashes
- Modification times
- Import timestamps
- Last run time

## Advanced Usage

```bash
# Show import tracker details
HAL --ingest-status

# Manually sync specific Business_Tools directory
python3 mcp-ai/auto_ingest_training.py --sync-intel /path/to/Business_Tools/Training_Data/

# Verbose output
python3 mcp-ai/auto_ingest_training.py -v

# Reset tracker to re-import all files
python3 mcp-ai/auto_ingest_training.py --track-reset
```

## Cron Examples

### Daily at 2 AM

```cron
0 2 * * * cd <REPO_ROOT> && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py >> ~/.mcp-ai/auto_ingest.log 2>&1
```

### Every 6 hours

```cron
0 */6 * * * cd <REPO_ROOT> && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py >> ~/.mcp-ai/auto_ingest.log 2>&1
```

### Every hour

```cron
0 * * * * cd <REPO_ROOT> && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py >> ~/.mcp-ai/auto_ingest.log 2>&1
```

## Integration with HAL Chat

After setting up auto-ingest, HAL will:

- Have access to latest business intelligence immediately
- Search across all imported documents automatically
- Generate updated intel reports with latest data
- Include newly imported training data in responses

Example:

```bash
$ HAL 'what are the latest activities at centene'
# Returns: Latest intel from most recent ingestion

$ HAL --intel-report davita
0 2 * * * cd <REPO_ROOT> && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py >> ~/.mcp-ai/auto_ingest.log 2>&1
```

## Troubleshooting

### Import Log Grows Large

0 */6 * * * cd <REPO_ROOT> && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py >> ~/.mcp-ai/auto_ingest.log 2>&1
# Rotate or truncate
> ~/.mcp-ai/auto_ingest.log

# Or view specific run
grep "Auto-Ingest started" ~/.mcp-ai/auto_ingest.log | tail -1
```
0 * * * * cd <REPO_ROOT> && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py >> ~/.mcp-ai/auto_ingest.log 2>&1
### Some Files Not Being Imported

```bash
# Check tracker for skip reasons
python3 mcp-ai/auto_ingest_training.py --show-tracker | grep filename

cd <REPO_ROOT> && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py
HAL --ingest-reset
```

### Cron Job Not Running

```bash
# Check crontab was added
crontab -l

# Verify environment
which python3
pwd

# Test manually
cd <REPO_ROOT> && source .venv/bin/activate && python3 mcp-ai/auto_ingest_training.py
```

## Configuration

Environment variables (optional):

- `HAL_TRAIN_DIR` — Override training data directory (default: `~/.mcp-ai/training`)

To use:

```bash
export HAL_TRAIN_DIR=/custom/path
HAL --auto-ingest
```

## Performance

- **Typical run time**: 5-30 seconds depending on file count
- **Memory usage**: ~100MB
- **Disk usage**: Tracked metadata file is <1MB
- **Network**: None (all local)
- **CPU**: Minimal (JSON parsing + file hashing)

## Privacy & Security

- All ingestion happens locally (no external calls)
- Encrypted training data is updated when auto-ingest runs
- Import tracker is stored locally in `~/.mcp-ai/training/`
- No data is shared or uploaded
- Supports `HAL_TRAINING_KEY` env var for encryption password

## Next Steps

1. ✓ Set up cron job for daily auto-ingest
2. ✓ Verify first run with `HAL --auto-ingest`
3. ✓ Check log: `tail -f ~/.mcp-ai/auto_ingest.log`
4. ✓ Use HAL as normal — will always have latest data
