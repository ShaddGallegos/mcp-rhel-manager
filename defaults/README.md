# mcp-rhel-manager local defaults/env.yml variable guide

This file documents variables in defaults/env.yml for standalone project use.

- Keep defaults/env.yml encrypted with ansible-vault.
- Vault password file is expected at ~/.ansible/conf/.defaults_env.vaultpass.txt by default.

## Variables

| Variable | Synopsis | More info |
|---|---|---|
| grafana_admin_password | Enter value for mcp rhel manager grafana admin password (mcp-rhel-manager) | https://grafana.com/docs/grafana/latest/ |
| grafana_admin_user | Enter value for mcp rhel manager grafana admin user (mcp-rhel-manager) | https://grafana.com/docs/grafana/latest/ |
| installed_packages | - Ensure OS packages installed: `smartmontools lm_sensors pciutils firewalld fail2ban` | See project checklist |
| monitoring_compose | Enter value for mcp rhel manager monitoring compose (mcp-rhel-manager) | See project checklist |
| monitoring_dir | Enter value for mcp rhel manager monitoring dir (mcp-rhel-manager) | See project checklist |
| monitoring_install_docker | Enter value for mcp rhel manager monitoring install docker (mcp-rhel-manager) | See project checklist |
| monitoring_install_grafana | Enter value for mcp rhel manager monitoring install grafana (mcp-rhel-manager) | https://grafana.com/docs/grafana/latest/ |
| monitoring_install_prometheus | Enter value for mcp rhel manager monitoring install prometheus (mcp-rhel-manager) | https://grafana.com/docs/grafana/latest/ |

## Sources

- Checklist: /home/sgallego/GIT/mcp-rhel-manager/CHECKLIST.md
