Lenovo-ThinkPad-IdeaPad-P-Series-Autonomous-AI-Node

Lenovo Architect is a self-evolving, self-healing management framework designed for Lenovo ThinkPad-IdeaPad-P-Series-Autonomous-AI-Node Mobile Workstations running RHEL 10 or Fedora. It leverages the **Model Context Protocol (MCP)** to allow a local LLM (via Ollama) to monitor hardware health, optimize performance for AI workloads, and manage system configuration as code.

## Core Features

- **Autonomous Evolution**: Automatically handles Kernel and NVIDIA driver updates, ensuring `akmods` are successfully built before finalizing.
- **AI Performance Optimization**: Dynamically adjusts CPU governors to `performance` and enables NVIDIA Persistence Mode to reduce latency for LLM inference.
- **Sentinel Security**: Real-time monitoring of `sshd` and `firewalld` logs to identify and remediate network intrusion attempts.
- **Predictive Self-Healing**: Monitors NVMe SMART health. If hardware failure is predicted, the system automatically exports its "DNA" (configurations and Ansible roles) to a remote repository.
- **Configuration as Code (CaC)**: Automatically generates Ansible manifests of the current system state for rapid propagation to new hardware.

## System Architecture

The project consists of three primary layers:

1.  **The Brain (Ollama)**: Runs `qwen2.5-coder:7b` to process telemetry and make executive decisions.
2.  **The Hands (MCP Server)**: A Python-based FastMCP server (`server.py`) providing granular access to system tools.
3.  **The Nervous System (Sentinel Agent)**: A background loop (`auto-fixer.sh`) that triggers the LLM to perform audits every hour.

## Installation (Genesis Sequence)

To deploy the Architect on a fresh Lenovo P-Series machine, run the Genesis script:

```bash
chmod +x architect_genesis.sh
./architect_genesis.sh

## HAL & Remediation

HAL is the user-facing CLI for interacting with the local MCP bridge and recording interactions for training and remediation.

- Invoke: `HAL "your question"` or `hal "your question"`.
- Recordings are placed under `~/.mcp-ai/training/` as JSONL entries.
- To request remediation at the time of the query: `HAL --remediate "check disk"`.

Auto-remediation is performed by `mcp-ai/remediate.py`. For safety, privileged actions are executed via a validated wrapper installed at `/usr/local/bin/mcp-ai-runner` which enforces a whitelist of allowed binaries and services. The genesis script installs this runner and updates `/etc/sudoers.d/mcp-ai` so the AI user and the invoking user may execute only the runner with passwordless sudo.

Logs and results:

- Suggestions: `~/.mcp-ai/fixes/*.json`
- Execution results: `~/.mcp-ai/fixes/result-*.json` and `~/.mcp-ai/cache/`
- Runner audit log: `~/.mcp-ai/runner.log` (written by `/usr/local/bin/mcp-ai-runner`)

If you need to expand allowed operations, edit `/usr/local/bin/mcp-ai-runner` and update the `allowed_bins` and `allowed_services` lists, then re-run `architect_genesis.sh` to persist changes.


## (2026-04-25) Added diagnostics and remediation features


Run `./architect_genesis.sh --venv` to regenerate everything.

## (2026-04-26) System Indexer

- Added a system indexer (`mcp-ai/indexer.py`) that discovers logs, installed packages, running services, common binaries, and library/plugin folders and writes a JSONL record to `/var/lib/mcp/training` for supplemental training data. The indexer is scheduled daily with `mcp-ai-indexer.timer` and can be run manually with `sudo systemctl start mcp-ai-indexer.service` or as the `mcp-ai` user using the venv python.
- IMPORTANT: Always run `redact_training.py` on index output before merging or ingesting into training datasets to avoid leaking sensitive information.


## (2026-04-25) Added diagnostics and remediation features
- The genesis script now deploys the server template from the repo and prepares the remediation playbook and CHECKLIST.

Run `./architect_genesis.sh --venv` to regenerate everything.
