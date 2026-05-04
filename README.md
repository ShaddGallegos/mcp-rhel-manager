# mcp-rhel-manager / HAL

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

A self-evolving, self-healing management framework for RHEL 10 / Fedora workstations.  
HAL is the primary CLI — an AI-powered assistant that combines a local LLM (via Ollama) with intelligent training data management, live company intelligence, and Red Hat product expertise.

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

| Command                                | Description                                                      |
| -------------------------------------- | ---------------------------------------------------------------- |
| `HAL --import-docs PATH...`            | Import local files (xlsx, pdf, csv, md, json, yaml, …)           |
| `HAL --import-business-intel PATH...`  | Import Business_Tools JSONL intel records                        |
| `HAL --import-url URL...`              | Fetch and ingest URLs (default depth 3; use `--1` through `--9`) |
| `HAL --import-txt FILE...`             | Ingest plain-text files                                          |
| `HAL --import-redhat-docs [DOCSET...]` | Import curated Red Hat docs                                      |
| `HAL --sync-redhat-docs`               | Sync default Red Hat docs (Satellite 6.18, AAP 2.6, IdM 5.0)     |
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

[Service]
Type=simple
ExecStart=bash /home/sgallego/GIT/mcp-rhel-manager/mcp-ai/start-bridge.sh
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
