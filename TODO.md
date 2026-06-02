# TODO: Missed items captured from recent session (2026-05-23)

Recently completed (2026-06-02)
- Weekly malware scan + weekly report integration completed and validated.
- Weekly chkrootkit maintenance/scan job added, with findings included in weekly report output.
- System-specific quick start templating added via `templates/quick_start.md.j2` + `scripts/render_quickstart.py`.
- Automatic quick start regeneration wired into installer post-install and `bin/HAL` runtime wrapper.
- Supplemental training binary-only input flow hardened to return success with a manifest instead of failing.

High priority
- RH SSO token refresh failure: fix `refresh_insights_token.sh` to log HTTP response body and code; provide manual-refresh instructions and verify token lifecycle. (files: `scripts/`, `ansible/`, `~/.ansible/conf/env.yml`) — DONE
- Implement `hal_control` approval CLI: create an approval queue for remediator actions and integrate with the runner policy. (files: `scripts/hal.py`, `mcp-ai/`) — DONE
- Add AV-specific remediator (`--av-remediate`): targeted, auditable fixes limited to safe A/V actions + explicit confirmation flow. (files: `scripts/hal.py`, `scripts/hal-multimedia.py`) — DONE (see `scripts/av_remediate.py`)
- Review and update `/usr/local/bin/mcp-ai-runner` policy to allow safe remediator operations or provide a per-action allowlist + audit. (files: `/usr/local/bin/mcp-ai-runner`, `scripts/hal.py`) — DONE (runner now merges per-plan patterns and audits privileged actions)
- Dashboard integration: wire approval queue and `features.redhat` toggles into the dashboard UI/API and expose approval actions. (files: `mcp-ai/dashboard*`, `docs/`) — PENDING

Medium priority
- Add optional inotify-based event-driven auto-ingest mode as a more responsive alternative to cron. (files: `mcp-ai/auto_ingest_training.py`)
- Extract more ingestion primitives (tracking/deduping/hashing) into a shared module (e.g. `mcp-ai/ingest_utils.py`). (files: `mcp-ai/`) 
- Split `scripts/hal.py` into smaller modules (core CLI, remediator, diagnostics, AV tooling). (files: `scripts/hal.py`, `scripts/`) 
- Add unit tests for `mcp-ai/ingest_common.py` and `mcp-ai/auto_ingest_training.py`. (files: `tests/`, `mcp-ai/`)
- Add CI checks to run `python -m py_compile` and a lightweight linter on changed Python files. (files: `.github/workflows/` or CI config)

Low priority
- Document the ansible-vault / refresh workflow and instructions for obtaining a valid RH refresh token; add secure storage guidance. (files: `docs/`, `ansible/`)
- Add a scheduled verification run for OpenSCAP and ingestion of results into MCP training. (files: `scripts/`, `ansible/playbooks/`)
- Add tests and a documented dry-run mode for `organize.sh`; ensure classification rules are reversible. (files: `organize.sh`, `scripts/`)
- Add a safe-default setting in HAL config to require explicit approval for any system-change actions. (files: `~/.mcp-ai/dashboard_config.json`, `scripts/hal.py`)
- Create an automated cleanup step (post-organization) to identify and remove fragments/junk safely (dry-run first). (files: `scripts/organize.sh`, `scripts/cleanup.sh`)

Notes
- These items were collected from the interactive session and pending work described in session transcripts and recent edits. Prioritize RH token refresh, approval flow, and runner policy changes first.
