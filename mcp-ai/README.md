> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

To restore `/etc` from the last backup created by `remediate.py`:

```bash
sudo python3 mcp-ai/rollback.py --list
sudo python3 mcp-ai/rollback.py --restore /var/lib/mcp/backups/etc-backup-...tar.gz --yes
```

To run the minimal dashboard (optional):

```bash
# install flask in the venv
/opt/mcp-rhel-manager/venv/bin/pip install flask
FLASK_APP=mcp-ai/dashboard.py /opt/mcp-rhel-manager/venv/bin/flask run --host=0.0.0.0 --port=8080
```

- `rollback.py` — restore most recent `/etc` backup created during remediation (requires sudo).
- `enable_services.sh` — helper script to enable/start MCP-related systemd units.
- `tests/run_sim.sh` — end-to-end simulation harness for CI-like validation.
- `dashboard.py` — minimal Flask-based plan review UI (optional).

MCP AI: Collector & Remediator
=================================

This folder contains helper scripts used by the Architect Genesis flow to
collect system errors, produce small training entries, and consult a local
LLM for remediation suggestions.

Directory layout (created under `~/.mcp-ai`):


Usage

Collector (invoked automatically by systemd user timer):

  /usr/bin/env python3 <REPO_ROOT>/mcp-ai/collector.py --run

Run once and push to remediator:

  /usr/bin/env python3 <REPO_ROOT>/mcp-ai/collector.py --once --push

Remediator (analyze latest entry):

  /usr/bin/env python3 <REPO_ROOT>/mcp-ai/remediate.py --latest

To allow automatic execution of suggested commands set an environment variable
and ensure you understand the risk:

  export ALLOW_AUTO_FIX=1

Then run:

  /usr/bin/env python3 <REPO_ROOT>/mcp-ai/remediate.py --latest --exec

Notes & Safety
--------------

- By default the remediator will NOT execute commands. Execution is gated by
  `ALLOW_AUTO_FIX=1` (operator opt-in).
- The remediator expects a local LLM bridge at `http://localhost:1776/api/chat`.
  Adjust `OLLAMA_URL` env var to change.
- Training data and logs are stored under `~/.mcp-ai` and should be protected
  (permissions 700) as they may contain sensitive information.
