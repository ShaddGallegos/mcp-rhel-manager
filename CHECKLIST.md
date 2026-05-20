# MCP RHEL Manager - Checklist

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

- Run installer (recommended venv): `./install_system.sh --venv --start --yes`
- Verify virtualenvs exist (do NOT commit virtualenvs to git): `test -d venv && test -d venv-bridge`
- Ensure OS packages installed: `smartmontools lm_sensors pciutils firewalld fail2ban`
- Start services: `systemctl --user status mcp-bridge.service mcp-sentinel.service` and `systemctl status firewalld`
- Run diagnostics: `python3 -c "import scripts.server as s; print(s.hardware_diagnostics())"`
- Run security scan: `python3 scripts/hal-security-audit.py report --repos=5` and review `security_report.json`
- Ensure no tracked virtualenvs: run `scripts/prepare_repo_for_push.sh --check` to detect tracked venvs and get recommended git commands to untrack.
- Run full JSON diagnostics: `python3 -c "import scripts.server as s; print(s.full_diagnostics_json())"`
 - Encrypt ansible env when ready: `scripts/ansible_vault_encrypt.sh $ANSIBLE_ENV_PATH` (defaults to `~/.ansible/conf/env.yml`)
 - Decrypt ansible env when needed: `scripts/ansible_vault_decrypt.sh $ANSIBLE_ENV_PATH` (defaults to `~/.ansible/conf/env.yml`)
 - Interactive setup: `python3 scripts/configure_ansible_env.py` or `./install_system.sh --reconfigure`
 - Supplemental training dataset: build and encrypt with `mcp-ai supplemental-training --name <name> --url-file urls.txt` (results in `~/.ansible/.supplementaltraining/<name>`)
 - On macOS: prefer `./install_system.sh --dry-run` then follow the printed instructions; launchd agents are used instead of systemd units.
