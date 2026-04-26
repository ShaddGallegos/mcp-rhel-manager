# MCP RHEL Manager - Checklist

- Run genesis (recommended venv): `./architect_genesis.sh --venv`
- Verify venvs: `ls -la venv venv-bridge`
- Ensure OS packages installed: `smartmontools lm_sensors pciutils sof-firmware auditd firewalld fail2ban`
- Start services: `systemctl --user status mcp-bridge.service mcp-sentinel.service` and `systemctl status auditd firewalld`
- Run diagnostics: `python3 -c "import server; print(server.hardware_diagnostics())"`
- Run security scan: `python3 -c "import server; print(server.security_diagnostics())"`
- Run full JSON diagnostics: `python3 -c "import server; print(server.full_diagnostics_json())"`
