Ansible remediation playbooks
---------------------------

This folder contains small, local playbooks to remediate common issues detected by
the HAL diagnostics tool.

Files:

- `remediate_firewall.yml` — ensure `firewalld` is installed, enabled and started.
- `remediate_auditd.yml` — ensure `auditd` is installed, enabled and a basic rule is present.
- `remediate_updates.yml` — list available package updates and optionally apply them (controlled by `apply_updates` variable).
- `register_insights.yml` — install and register `insights-client` (requires Red Hat subscription/registration and root).
 - `setup_redhat_features.yml` — install and configure Red Hat / Console features: installs and registers `insights-client`, installs OpenSCAP and a weekly scan timer, and updates the MCP dashboard feature toggles (RHEL systems only).

Usage (dry-run):

```bash
ansible-playbook -i localhost, -c local --check remediate_firewall.yml
ansible-playbook -i localhost, -c local --check remediate_auditd.yml
ansible-playbook -i localhost, -c local --check remediate_updates.yml
ansible-playbook -i localhost, -c local --check register_insights.yml
```

To execute changes, remove `--check` and run with `--become` (you'll need sudo/root):

```bash
ansible-playbook -i localhost, -c local remediate_firewall.yml --become
```

Notes:
- These playbooks run locally (`connection: local`) and require root privileges for most tasks.
- `remediate_updates.yml` will only apply updates when `apply_updates: true` is set (safeguard).
- The `register_insights.yml` playbook will attempt to run `insights-client` commands and requires root.
