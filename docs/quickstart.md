# Quickstart — mcp-rhel-manager

This quickstart covers common install paths and how to use the `HAL` CLI (`scripts/hal.py`). Use these examples to get started quickly; the commands below are safe to run in dry-run mode where shown.

## System installer

- Preview what would happen (dry-run):

```bash
./install_system.sh --dry-run
```

- Full system install (recommended for production hosts):

```bash
sudo ./install_system.sh --start --yes
```

- In-repo venv (developer / single-user install):

```bash
./install_system.sh --venv --start --yes
```

- Install to a custom base directory:

```bash
sudo ./install_system.sh --base-dir /opt/mcp-rhel-manager --start --yes
```

- Optional: precompute embeddings (may require additional packages):

```bash
sudo ./install_system.sh --precompute-embeds --start --yes
```

- Uninstall (safe defaults — use `--force` to remove base dir):

```bash
sudo ./install_system.sh --uninstall --yes --force
```

Notes:
- When running `--apply` (the default for a non-dry run) on a non-root account the script uses `sudo` where necessary.
- Use `--yes` to skip interactive confirmation when applying changes.

## Quick HAL CLI examples

Run a single query:

```bash
python3 scripts/hal.py "help me fix my audio"
# or, if you've installed the HAL wrapper
HAL "help me fix my audio"
```

Start an interactive HAL session (REPL):

```bash
python3 scripts/hal.py --interactive
```

Useful one-shot commands:

```bash
python3 scripts/hal.py --bridge-check           # Check Ollama/bridge connectivity
python3 scripts/hal.py --list-intents           # Show available intent routes/examples
python3 scripts/hal.py --training-maintenance   # Training maintenance (dry-run)
python3 scripts/hal.py --training-maintenance --apply  # Apply training maintenance
python3 scripts/hal.py --export-training-bundle --bundle-output-dir ~/Downloads
python3 scripts/hal.py --model-list            # List available Ollama models
python3 scripts/hal.py --model-pull mistral:7b # Pull a model via the bridge
```

Ingest URLs into the training corpus (example crawl depth 3):

```bash
python3 scripts/hal.py --import-url https://example.com --3
```

Run audio/voice mode (requires a local TTS engine such as `espeak`):

```bash
python3 scripts/hal.py --voice "Please run audio diagnostics"
```

## Developer checks

- Quick Python syntax check for the main CLI: 

```bash
python3 -m py_compile scripts/hal.py
```

- Run the installer in dry-run first to confirm what will be changed.

## Troubleshooting & notes

- If you hit a `NameError` referencing `_is_aap_migration_strategy_query`, update to the latest `scripts/hal.py` in this repo — a compatibility shim is present to avoid that runtime error.
- When running `install_system.sh --apply` make sure you have `sudo` available if you are not root. The installer will abort if it cannot escalate privileges.
- Be cautious with `--exec` / `--apply` flags; they cause the remediator or other automation to execute changes on your system.

## Security

- Treat the `--exec` / `--apply` flags as privileged; inspect generated playbooks/scripts before applying.

---

For more details, read `install_system.sh` usage and the HAL CLI help:

```bash
./install_system.sh --help
python3 scripts/hal.py --help
```

## Supplemental training (watch a folder) & GGUF

Goal: monitor a folder (e.g. an SD card at `/run/media/sgallego/SD_Card/Downloads`), convert/translate supported files into HAL supplemental training records, optionally build a JSONL dataset and attempt a private GGUF export (requires an external converter), and place training files under HAL's training dir so the system can use them for RAG/answers.

1) Quick one-shot: convert a folder into per-file training records (HAL will read these from `~/.mcp-ai/training`):

```bash
python3 mcp-ai/ingest_documents.py /run/media/sgallego/SD_Card/Downloads --recursive
# (or explicitly:) python3 mcp-ai/ingest_documents.py --outdir ~/.mcp-ai/training /run/media/sgallego/SD_Card/Downloads --recursive
```

2) Continuous watch (auto-ingest): have the auto-ingest engine monitor your SD card path and import new/updated files automatically. Set the watch path via env var and run the watcher:

```bash
DOCUMENT_WATCH_PATHS_1=/run/media/sgallego/SD_Card/Downloads python3 mcp-ai/auto_ingest_training.py --watch
```

Notes: `auto_ingest_training.py` defaults to `~/Downloads` and `~/Documents` unless you override `DOCUMENT_WATCH_PATHS_1` (and `_2`). It calls `ingest_documents.py` under the hood and writes records into `~/.mcp-ai/training`.

3) Build a JSONL dataset and (optionally) attempt GGUF conversion:

```bash
# Create a supplemental dataset (writes to <outdir>/<name>/dataset.jsonl)
python3 mcp-ai/supplemental_training.py --name sdcard --dirs /run/media/sgallego/SD_Card/Downloads --outdir /run/media/sgallego/SD_Card/Downloads --no-encrypt

# If you have a GGUF conversion tool available on PATH (example: gguf-convert)
python3 mcp-ai/supplemental_training.py --name sdcard --dirs /run/media/sgallego/SD_Card/Downloads --outdir /run/media/sgallego/SD_Card/Downloads --no-encrypt --convert-gguf --gguf-tool /usr/local/bin/gguf-convert
```

`supplemental_training.py` writes `dataset.jsonl` to `<outdir>/<name>/dataset.jsonl`. The `--convert-gguf` step only works if you have an external converter available (the repository includes a small wrapper `mcp-ai/gguf_converter.py` that calls whatever converter is on your PATH).

4) Watch for a created dataset and attempt conversion in a background watcher (example helper provided):

```bash
# Use the provided helper to watch an OUTDIR for dataset.jsonl and try conversion
OUTDIR=/run/media/sgallego/SD_Card/Downloads/.supplementaltraining bash scripts/watch_convert_sdcard.sh
```

5) Import a produced `dataset.jsonl` into HAL training (split into per-file JSON entries):

```bash
# Create training dir if needed
mkdir -p ~/.mcp-ai/training

# Split JSONL lines into individual JSON files and place under HAL training
python3 - <<'PY'
import json, pathlib
src = pathlib.Path('/run/media/sgallego/SD_Card/Downloads/sdcard/dataset.jsonl')
outdir = pathlib.Path.home() / '.mcp-ai' / 'training'
outdir.mkdir(parents=True, exist_ok=True)
for i, line in enumerate(src.read_text(encoding='utf-8').splitlines(), start=1):
	try:
		obj = json.loads(line)
	except Exception:
		continue
	fname = outdir / f'supp-{i:05d}.json'
	fname.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
print('Imported dataset ->', outdir)
PY
```

6) Make HAL aware of new training data:

```bash
python3 scripts/hal.py --training-maintenance   # dry-run
python3 scripts/hal.py --training-maintenance --apply  # apply cleanup/maintenance
```

7) Serving a local GGUF model: after `dataset.gguf` is produced you must load/serve it with your local model runtime (ollama, llama.cpp, etc.). Once a runtime serves the model, point HAL to the runtime via `OLLAMA_URL` (or the relevant bridge configuration) so HAL can use that model in addition to any remote models.

Security & privacy notes:
- `supplemental_training.py` will try to run `clamscan` if present — set `SUPPLEMENTAL_SKIP_CLAMS=1` to bypass in trusted environments.
- By default supplemental artifacts may be encrypted; use `--no-encrypt` when you need an unencrypted dataset for local conversion, or provide a vault password file at `~/.ansible/conf/.vaultpass.txt` to encrypt the artifact.

If you want, I can add a short systemd unit or a `systemd --user` service snippet to run the auto-ingest watcher or the `watch_convert_sdcard.sh` helper on boot. Would you like that?

## Auto-ingest systemd examples

Below are example `systemd --user` unit files you can adapt to run the auto-ingest watcher and the watch/convert helper on login/startup. Replace `/home/you/GIT/mcp-rhel-manager` with your repository path and adjust `DOCUMENT_WATCH_PATHS_1` as needed.

- `~/.config/systemd/user/hal-auto-ingest.service` — runs the Python watcher:

```ini
[Unit]
Description=HAL auto-ingest training watcher
After=network-online.target

[Service]
Type=simple
Environment=DOCUMENT_WATCH_PATHS_1=/run/media/sgallego/SD_Card/Downloads
ExecStart=/usr/bin/env bash -c "DOCUMENT_WATCH_PATHS_1=/run/media/sgallego/SD_Card/Downloads python3 /home/you/GIT/mcp-rhel-manager/mcp-ai/auto_ingest_training.py --watch"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

- `~/.config/systemd/user/watch-convert-sdcard.service` — runs the helper that watches an OUTDIR and attempts GGUF conversion:

```ini
[Unit]
Description=Watch SD card OUTDIR and run conversion helper
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env bash -c "OUTDIR=/run/media/sgallego/SD_Card/Downloads/.supplementaltraining bash /home/you/GIT/mcp-rhel-manager/scripts/watch_convert_sdcard.sh"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Enable and start the user services:

```bash
# reload unit files
systemctl --user daemon-reload
# enable to start at login
systemctl --user enable --now hal-auto-ingest.service
systemctl --user enable --now watch-convert-sdcard.service
```

If you prefer a timer-based run (e.g., run every 5 minutes) create a matching `.timer` unit and enable it instead of the service.
