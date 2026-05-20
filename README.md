# mcp-rhel-manager / HAL

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

A self-evolving, self-healing management framework for RHEL 10 / Fedora workstations.  
HAL is the primary CLI — an AI-powered assistant that combines a local LLM (via Ollama) with intelligent training data management, live company intelligence, and Red Hat product expertise.

Cross-platform note: The installer and scripts aim to support RHEL / Fedora (dnf/yum), Debian/Ubuntu (apt), and macOS (Homebrew + launchd). On macOS some systemd-specific features (system-level units) are replaced with user launchd agents; run `./install_system.sh --dry-run` to preview platform-specific steps.

---

## System Architecture

| Layer          | Component                                         | Role                                    |
| -------------- | ------------------------------------------------- | --------------------------------------- |
| **Brain**      | Ollama (`qwen2.5-coder:7b`, `llama4:scout`, etc.) | LLM inference engine                    |
| **Bridge**     | `mcp-ai/bridge.py` on port 1776                   | Proxies HAL → Ollama; exposes `/health` |
| **CLI**        | `scripts/hal.py` / `bin/HAL`                      | All user-facing commands                |
| **MCP Server** | `scripts/server.py` (FastMCP)                     | System tool access for automation       |
| **Sentinel**   | `scripts/auto-fixer.sh`                           | Hourly audit loop                       |

All data stays local — nothing is sent externally.

---

## Quick Start

### 1. Installation

```bash
chmod +x install_system.sh
./install_system.sh --start --yes
```

Re-run with `--venv` to regenerate the virtual environment.

### 2. Start the Bridge

The bridge must be running for LLM-backed responses. HAL falls back to local training data search when the bridge is offline.

```bash
bash mcp-ai/start-bridge.sh
```

### 3. Verify

```bash
HAL --bridge-check
# Also: curl http://localhost:1776/health
```

### 4. Ask HAL

```bash
HAL "what needs attention?"
HAL help
HAL help quick-start
```

---

## HAL CLI Reference

Invoke as `HAL "..."` or `hal "..."`.

### General

| Command                   | Description                                               |
| ------------------------- | --------------------------------------------------------- |
| `HAL "question"`          | Chat with LLM bridge (auto-falls back to training search) |
| `HAL --interactive`       | Multi-turn REPL session                                   |
| `HAL help [topic]`        | Built-in help system                                      |
| `HAL --list-intents`      | Show all known intent routes                              |
| `HAL --explain`           | Show routing decision from last query                     |
| `HAL --bridge-check`      | Check bridge + Ollama health, list available models       |
| `HAL --diagnostics`       | Full system diagnostics report                            |
| `HAL --status`            | One-line health summary                                   |
| `HAL --run-tests`         | Run intent routing regression suite                       |
| `HAL --run-offline-tests` | Run offline routing regression suite                      |

### Training Data

Note: HAL can inject your local training data into LLM prompts (RAG). Control behavior with environment variables: `HAL_RAG_ALWAYS=0` disables automatic RAG collection; `HAL_RAG_FALLBACK=0` disables serving training-data fallback when the bridge fails. Defaults are enabled.


| Command                                | Description                                                      |
| -------------------------------------- | ---------------------------------------------------------------- |
| `HAL --import-docs PATH...`            | Import local files (xlsx, pdf, csv, md, json, yaml, …)           |
| `HAL --import-business-intel PATH...`  | Import Business_Tools JSONL intel records                        |
| `HAL --import-url URL...`              | Fetch and ingest URLs (default depth 3; use `--1` through `--9`) |
| `HAL --import-txt FILE...`             | Ingest plain-text files                                          |
| `HAL --import-redhat-docs [DOCSET...]` | Import curated Red Hat docs                                      |
| `HAL --sync-redhat-docs`               | Sync default Red Hat docs                                         |
| `HAL --auto-ingest`                    | Run auto-ingest from watch directories                           |
| `HAL --ingest-status`                  | Show import history                                              |
| `HAL --ingest-reset`                   | Reset tracker (re-import everything next run)                    |
| `HAL --training-report`                | Training data quality and statistics report                      |
| `HAL --training-maintenance`           | Dry-run duplicate analysis                                       |
| `HAL --training-maintenance-apply`     | Remove duplicate supplemental_document records                   |
| `HAL --setup-training-maintenance`     | Install daily/weekly cron jobs for maintenance                   |
| `HAL --encrypt-training`               | Encrypt training files at rest (PBKDF2 + AES-128)                |
| `HAL --decrypt-training`               | Decrypt training files                                           |
| `HAL --export-training-bundle`         | Export portable zip bundle of training data                      |

Supplemental training workflow
------------------------------

Use the `mcp-ai supplemental-training` command to assemble private supplemental datasets from URLs, a file containing URLs, or local directories. The tool will:

- fetch and archive input content (HTTP or local files)
- run a malware scan with `clamscan` if available
- transcode HTML/PDF/DOCX to plaintext where possible
- redact obvious secrets using `mcp-ai/redact_training.py`
- deduplicate and assemble a `dataset.jsonl` in `~/.ansible/.supplementaltraining/<name>`
- optionally attempt GGUF conversion when a conversion tool is present
- encrypt the final artifact using `ansible-vault` (if installed) or the local `mcp-ai/training_crypto.py` fallback that honors `~/.ansible/conf/.vaultpass.txt`.

This keeps imported training artifacts private and scanned before being ingested or used for RAG. See `mcp-ai/supplemental_training.py --help` for usage details.

### Company Intelligence

| Command                             | Description                                                 |
| ----------------------------------- | ----------------------------------------------------------- |
| `HAL --intel-report ACCOUNT`        | Generate seller-ready account intel brief                   |
| `HAL --intel-report-all ACCOUNT...` | Generate reports for multiple accounts                      |
| `HAL --intel-report-file PATH`      | Generate reports for accounts listed in file (one per line) |
| `HAL --enrich-companies COMPANY...` | Enrich named companies from public web sources              |
| `HAL --enrich-from-training`        | Discover companies from training data and enrich them       |
| `HAL --max-enrich-companies N`      | Limit discovery count (default: 10)                         |

Intel reports include:

- Account ownership, territory, and team
- Technology stack and integration signals (**DETECTED INTEGRATION SIGNALS** section)
- Red Hat strategic focus areas
- Primary contacts with clickable email links
- Recent headlines and activity
- **PRIMARY OBJECTIVE** — company-specific objective from imported intel (not script instructions)
- Key discovery questions

Intel cache: `~/.mcp-ai/cache/intel/` — refreshed daily, retained for 3 days (`HAL_INTEL_CACHE_TTL_DAYS`, default `3`).

### Model Routing

| Command                              | Description                                                                     |
| ------------------------------------ | ------------------------------------------------------------------------------- |
| `HAL --task-profile PROFILE "query"` | Force task profile: `general`, `codegen`, `strategy`, `business`, `diagnostics` |
| `HAL --model MODEL "query"`          | Force exact Ollama model for this command                                       |

Environment overrides:

```bash
export HAL_FORCE_PROFILE=strategy
export HAL_FORCE_MODEL=llama4:scout
export HAL_MODEL_OVERRIDES='{"codegen":["qwen2.5-coder:7b"],"business":["mistral"]}'
```

### Git Helpers

```bash
HAL git log
HAL git status
HAL git diff
HAL git commit-msg     # generate conventional commit message from staged diff
HAL git pr-desc        # generate PR description vs main/master
```

### Remediation

```bash
HAL --remediate "check disk"
HAL --exec --remediate "auto-fix any issues"   # executes fixes (ALLOW_AUTO_FIX=1)
HAL --feedback ENTRY "feedback text"
HAL --playbook-run /path/to/playbook.yml       # dry-run with ansible-playbook --check
```

---

## Bridge Details

`mcp-ai/bridge.py` proxies requests from HAL to Ollama using a threaded HTTP server.

- **HAL → Bridge**: `http://localhost:1776/api/chat`
- **Bridge → Ollama**: `http://localhost:11434`
- **Health endpoint**: `GET http://localhost:1776/health` — reports bridge and Ollama status
- Auto-detects available Ollama models and selects the best one per task profile
- Circuit breaker clears automatically when the bridge recovers

If the bridge is down, HAL will:

1. Attempt auto-start via `mcp-ai/start-bridge.sh`
2. Fall back to local training data search

**To run as a systemd service:**

```ini
[Unit]
Description=HAL MCP Bridge
After=network.target

Pre-commit hook
---------------

This repository provides a lightweight `pre-commit` configuration that runs the
local secret scanner before commits. To enable it in your environment run:

```bash
./scripts/install_precommit.sh
```

The hook executes `mcp-ai/secret_scan.py` and will block commits if likely
secrets are detected (false positives are possible). Use `pre-commit run --all-files`
to scan the repo immediately.

GGUF conversion helper
----------------------

A small wrapper `mcp-ai/gguf_converter.py` detects available external GGUF
conversion tools (`gguf-convert`, `convert-gguf`, etc.) and invokes them when
requested. A smoke test that skips if no converter is present is included in
`tests/test_gguf_converter_smoke.py`.

[Service]
Type=simple
ExecStart=bash <REPO_ROOT>/mcp-ai/start-bridge.sh  # replace <REPO_ROOT> with your repository path
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now hal-bridge
```

---

## Auto-Remediation

Privileged actions run via `/usr/local/bin/mcp-ai-runner` — a validated wrapper enforcing a whitelist of allowed binaries and services.  
The genesis script installs the runner and updates `/etc/sudoers.d/mcp-ai`.

| Path                            | Contents          |
| ------------------------------- | ----------------- |
| `~/.mcp-ai/fixes/*.json`        | Suggested fixes   |
| `~/.mcp-ai/fixes/result-*.json` | Execution results |
| `~/.mcp-ai/runner.log`          | Runner audit log  |

---

## System Indexer

`mcp-ai/indexer.py` discovers logs, packages, running services, binaries, and library paths and writes a JSONL record to `/var/lib/mcp/training`.  
Scheduled daily via `mcp-ai-indexer.timer`.

```bash
sudo systemctl start mcp-ai-indexer.service
```

> Always run `mcp-ai/redact_training.py` on indexer output before merging into training datasets.

---

## Privacy & Data Storage

| Path                        | Contents                                         |
| --------------------------- | ------------------------------------------------ |
| `~/.mcp-ai/training/`       | All training data (JSONL records)                |
| `~/.mcp-ai/cache/intel/`    | Company intel cache (daily TTL, 3-day retention) |
| `~/.mcp-ai/fixes/`          | Remediator fix suggestions and results           |
| `~/.mcp-ai/reports/`        | Generated reports and eval results               |
| `~/.mcp-ai/auto_ingest.log` | Auto-ingest run log                              |

Training paths and encrypted artifacts are git-ignored via `.gitignore`.  
Use `HAL --encrypt-training` for at-rest protection (PBKDF2, 390 000 iterations, AES-128/Fernet).

---

## Environment Variables

| Variable                   | Description                                      |
| -------------------------- | ------------------------------------------------ |
| `HAL_TRAINING_KEY`         | Encryption password for training data            |
| `HAL_DISPLAY_NAME`         | Your name shown in prompts (default: `Dave`)     |
| `HAL_ASSISTANT_NAME`       | Assistant name (default: `HAL9000`)              |
| `HAL_CONVERSATIONAL`       | Enable conversational mode (`true`/`false`)      |
| `HAL_FORCE_MODEL`          | Force exact Ollama model globally                |
| `HAL_FORCE_PROFILE`        | Force task profile globally                      |
| `HAL_MODEL_OVERRIDES`      | JSON map to override per-profile model selection |
| `HAL_INTEL_CACHE_TTL_DAYS` | Intel cache retention in days (default: `3`)     |
| `HAL_INTERACTION_DIR`      | Custom interaction storage path                  |

---

## Changelog

### 2026-05-01

- **Bridge reliability**: `/health` endpoint now reports Ollama liveness; circuit breaker clears on recovery; bridge auto-start attempted when down.
- **Daily intel cache**: Company intel cached to `~/.mcp-ai/cache/intel/`; refreshed once per day; files pruned after 3 days.
- **Intel report improvements**:
  - `## DETECTED INTEGRATION SIGNALS` section lists all technology tags and stack signals from imported records.
  - `## PRIMARY OBJECTIVE` now shows the company's actual objective from intel data; falls back to *"Not explicitly provided in the imported company record."* — never shows script/tooling pipeline text.
- **Bulk report generation**: `--intel-report-all` and `--intel-report-file` CLI flags added.
- **Bridge check**: `--bridge-check` now uses `/health` endpoint and shows correct startup guidance.

### 2026-04-26

- Added system indexer (`mcp-ai/indexer.py`) with daily timer.

### 2026-04-25

- Added diagnostics, remediation features, and genesis script venv support.
- Added `HAL-FUNCTIONALITY.md` capability inventory.

---

## Execution Environment (mcp-ee)

**Synopsis:**

- `mcp-ee` is the Ansible Builder v3 execution-environment image used to run Ansible and HAL-related tooling in a reproducible container. The image purposefully does not include large LLM model files; those remain on the host and are mounted into the container at runtime.

**How to connect / run a shell in the image:**

- Quick interactive shell (replace `<TAG>` with the timestamp tag produced at build time):

```
podman run --rm -it \
  -v /var/lib/mcp-llms:/var/lib/mcp-llms:Z \
  localhost/mcp-ee:<TAG> /bin/bash
```

- Run a smoke-test (check Ansible and Ansible Runner are present):

```
podman run --rm localhost/mcp-ee:<TAG> ansible-galaxy --version
podman run --rm localhost/mcp-ee:<TAG> ansible-runner --version
```

**How to build (recommended, temporary context in /tmp):**

- A helper script is provided to avoid creating build artifacts in the git tree and to tag the image with a UTC timestamp. It creates `/tmp/<image>_<datetime>/context`, prunes podman caches, builds, and (by default) removes the temporary context.

Examples:

```
# build and remove temporary context when finished
scripts/build_ee_tmp_context.sh -f path/to/execution-environment.yml -n mcp-ee

# build and keep the temporary context for inspection
scripts/build_ee_tmp_context.sh -f path/to/execution-environment.yml -n mcp-ee -k

# build, flatten and run smoke tests (see scripts/ee_smoke_test.sh)
scripts/build_ee_tmp_context.sh -f path/to/execution-environment.yml -n mcp-ee -F -r
```

- The script tags images as `$(hostname)/mcp-ee:<YYYYMMDDTHHMMSSZ>` (UTC) by default. Use that exact tag when running the image.

- Use `-r` to run a quick smoke test after a successful build; the smoke test runner is `scripts/ee_smoke_test.sh` and performs basic checks (ansible-galaxy, ansible-runner, python, and a minimal ansible-runner playbook).

**How to update features and functions (rebuild flow):**

1. Edit `path/to/execution-environment.yml` to change system packages, additional build steps, or Python/pip dependencies.
2. Optionally update `additional_build_files` paths referenced by the manifest. Keep these paths outside of git-tracked build contexts when possible.
3. Run the build helper to produce a fresh, timestamped image:

```
scripts/build_ee_tmp_context.sh -f path/to/execution-environment.yml -n mcp-ee
```

Notes:
- The helper runs `podman system prune -a -f` before each build to reduce layer/cache problems and keep iterations clean.
- For rapid debugging only: you may `ansible-builder create -f path/to/execution-environment.yml -c /tmp/my-debug-context` and then edit `/tmp/my-debug-context/Containerfile` directly, but prefer modifying the manifest and re-running the helper for reproducibility.

**How to use `mcp-ee` in containers (recommended runtime patterns):**

- Keep large LLM models on the host and mount them read-only into the container. Example host model path: `/var/lib/mcp-llms`.

```
podman run --rm -it \
  -v /var/lib/mcp-llms:/var/lib/mcp-llms:Z \
  -v $PWD:/work:Z -w /work \
  localhost/mcp-ee:<TAG> bash
```

- Run HAL or other services inside the container (example starting HAL in an ephemeral container):

```
podman run --rm -it \
  -v /var/run/docker.sock:/var/run/docker.sock:Z \
  -v /var/lib/mcp-llms:/var/lib/mcp-llms:Z \
  -v $HOME/.mcp-ai:/root/.mcp-ai:Z \
  localhost/mcp-ee:<TAG> bash -c "HAL --bridge-check && HAL 'what needs attention?'"
```

**Advanced notes and troubleshooting:**

- Rootless podman: building images rootless requires `newuidmap`/`newgidmap` properly configured; if you see `permission denied` for user namespaces, either configure subordinate uid/gid maps or run builds with elevated privileges.
- If a generated `context/Containerfile` contains `python` invocations that fail on images that only provide `python3`, the build helper accommodates correct `PYCMD` usage; prefer fixing the `execution-environment.yml` rather than editing generated Containerfiles in git.
- Never bake model files into the image; always mount them from the host to ensure small, portable images and separate storage for large model artifacts.

If you want, I can:
- run a fresh build now and show the generated tag, or
- add a short smoke-test script to the repo that validates a built image (ansible-galaxy, ansible-runner, python version).

