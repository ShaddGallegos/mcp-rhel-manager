# Bundled fixes

This directory contains curated remediation plans shipped with the repository.

Purpose
- Provide operator-reviewed remediation plans that the remediator can write or execute.
- Allow the server to auto-provision a copy into a user's `~/.mcp-ai/fixes` so the plans
  are available to the remediator and dashboard.

How to provision
- The repository contains a small helper script at `mcp-ai/provision_fixes.sh` that will
  copy the bundled plans into your user AI home (`~/.mcp-ai/fixes`) without overwriting
  any existing files by default.

Manual usage examples

Install (no overwrite):

    ./mcp-ai/provision_fixes.sh

Install and overwrite existing files:

    ./mcp-ai/provision_fixes.sh --force

Using a plan
- The remediator and server reference plans under `~/.mcp-ai/fixes`. For example,
  the PipeWire reset plan is `plan-audio-pipewire.json` and can be executed via
  the remediator after approval or when `ALLOW_AUTO_FIX=1` is set and the plan
  has been approved.

See `mcp-ai/remediate.py --help` for details on writing and executing plans.
