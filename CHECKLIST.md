# MCP RHEL Manager - Checklist

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

- Run installer (recommended venv): `./install_system.sh --venv --start --yes`
- Verify venvs: `ls -la venv venv-bridge`
- Ensure OS packages installed: `smartmontools lm_sensors pciutils firewalld fail2ban`
- Start services: `systemctl --user status mcp-bridge.service mcp-sentinel.service` and `systemctl status firewalld`
- Run diagnostics: `python3 -c "import scripts.server as s; print(s.hardware_diagnostics())"`
- Run security scan: `python3 -c "import scripts.server as s; print(s.security_diagnostics())"`
- Run full JSON diagnostics: `python3 -c "import scripts.server as s; print(s.full_diagnostics_json())"`
