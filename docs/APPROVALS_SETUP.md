# Approvals API Setup

This document explains how to install and run the local Approvals API used
by the HAL dashboard and the remediation runner.

Service unit
------------

A systemd unit template is provided at `packaging/systemd/mcp-ai-approvals.service`.
Install and enable it with the helper script:

```bash
./scripts/install_approvals_service.sh
```

This will copy the unit to `/etc/systemd/system/` and attempt to enable/start
`mcp-ai-approvals.service`. The unit's `ExecStart` points at a typical
installation path; edit the unit or override it with a drop-in if your
installation root differs.

Runtime configuration
---------------------

- `MCP_APPROVALS_HOST` — listen address (default: `127.0.0.1`).
- `MCP_APPROVALS_PORT` — port (default: `8001`).

You can set these as environment variables for the systemd unit using a
drop-in file at `/etc/systemd/system/mcp-ai-approvals.service.d/override.conf`.

Vault & ansible notes
---------------------

- The HAL runner and remediator expect ansible secrets to live in
  `~/.ansible/conf/env.yml` and the vault password file referenced by
  `ANSIBLE_VAULT_PASSWORD_FILE` (default: `~/.ansible/conf/vaultpass.txt`).
- See `scripts/hal.py` and the dashboard for helpers that will create a
  secure `~/.ansible/conf/.vaultpass.txt` when needed.

Optional cpupower helper
------------------------

If your distribution does not provide a `cpupower` systemd unit, an
optional helper unit lives at `packaging/systemd/cpupower.service` and a
convenience installer script at `scripts/install_cpupower_service.sh`.

```bash
./scripts/install_cpupower_service.sh
```

Security
--------

The approvals API binds to localhost by default. Do not expose it to
untrusted networks. Systemd unit templates are intentionally simple; for
production use consider adding sandboxing (`NoNewPrivileges=yes`,
`ProtectSystem=full`, `PrivateTmp=yes`, etc.) and running the service as a
dedicated user.
