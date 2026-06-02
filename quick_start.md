# Quick Start - MCP RHEL Manager (HAL)

This guide is generated from a Jinja2 template so teams can render host-specific onboarding docs.

Generated profile:
- OS profile: fedora
- Service manager: systemd
- Package manager: dnf

## 1. Install and bootstrap

### Syntax
```bash
./install_system.sh [--apply|--dry-run] [--start] [--verify] [--verify-json] [--yes] [--force] [--venv] [--base-dir PATH] [--user NAME] [--home PATH] [--selinux permissive|enforcing|unchanged] [--firewalld enabled|disabled|unchanged]
```

### Common examples
```bash
# Preview everything without changing the system
./install_system.sh --dry-run

# Full unattended install and start services
./install_system.sh --start --yes

# Post-install verification
./install_system.sh --verify
```

### RHEL/Fedora example
```bash
sudo dnf install -y python3 python3-pip git
./install_system.sh --start --yes --selinux permissive --firewalld enabled
```




## 2. Configure secrets and policy (single canonical location)

All tokens/passwords go in `~/.ansible/conf/env.yml`.

### Syntax
```bash
python3 scripts/configure_ansible_env.py [--no-encrypt] [--yes|-y]
```

### Example
```bash
python3 scripts/configure_ansible_env.py --yes
```

Important settings to fill in:
- `HAL_SELF_HEAL_MODE`: `approval` or `self-heal`
- `HAL_SELF_HEAL_RISK_MODE`: `prompt`, `auto`, or `abort`
- `HAL_WEEKLY_REPORT_EMAIL`, `HAL_EMAIL_BACKEND`, `HAL_WEEKLY_REPORT_DAYS`
- `HAL_LLM_PROVIDER_PRIORITY`
- `HAL_ENABLE_WEEKLY_MALWARE_SCAN`, `HAL_MALWARE_SCAN_PATHS`, `HAL_RHEL_MALWARE_ENABLE`
- `HAL_ENABLE_WEEKLY_CHKROOTKIT`, `HAL_CHKROOTKIT_INSTALLER`

## 3. HAL CLI usage and syntax

### Syntax
```bash
HAL [flags] [text]
python3 scripts/hal.py [flags] [text]
```

### High-value flags
- `--interactive`: REPL session
- `--bridge-check`: bridge and model health
- `--status`: one-line system summary
- `--diagnostics`: full diagnostics payload
- `--import-docs PATH...`, `--import-url URL...`, `--import-txt FILE...`
- `--remediate` with optional `--exec`
- `--task-profile` and `--model`
- `--training-report`, `--training-maintenance`, `--training-maintenance-apply`

### Quick examples
```bash
HAL --bridge-check
HAL --status
HAL "what needs attention right now?"
HAL --task-profile diagnostics "investigate kernel warnings"
HAL --import-docs ~/Documents/*.pdf
HAL --import-url https://access.redhat.com/documentation
HAL --remediate "check disk pressure"
```

## 4. Weekly automation jobs

These jobs do not ask for a second email; they use existing settings from `~/.ansible/conf/env.yml`.

### Weekly report
```bash
python3 scripts/weekly_report.py --stdout
```

### Weekly malware scan (ClamAV + RHEL malware collector when available)
```bash
python3 scripts/weekly_malware_scan.py --stdout
```

### Weekly chkrootkit maintenance and scan
```bash
python3 scripts/weekly_chkrootkit.py --stdout
```

Expected outputs written to:
- `~/.mcp-ai/reports/malware-scan-latest.json`
- `~/.mcp-ai/reports/chkrootkit-latest.json`
- `~/.mcp-ai/reports/weekly-report-YYYYMMDD.txt`

## 5. System-specific service commands

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-bridge.service
sudo systemctl enable --now mcp-weekly-report.timer
sudo systemctl enable --now mcp-weekly-malware-scan.timer
sudo systemctl enable --now mcp-weekly-chkrootkit.timer
systemctl list-timers | grep mcp-weekly
```

## 6. Troubleshooting flags and commands

```bash
./install_system.sh --verify
HAL --bridge-check
HAL --diagnostics
python3 scripts/weekly_report.py --no-email --stdout
python3 scripts/weekly_malware_scan.py --stdout
python3 scripts/weekly_chkrootkit.py --stdout
```

If supplemental training appears to fail with mostly binary sources, rerun and inspect the generated manifest:
```bash
python3 mcp-ai/supplemental_training.py --name myset --dirs /path/to/docs --no-encrypt
cat ~/.ansible/.supplementaltraining/myset/manifest.json
```

## 7. Template rendering

Render this document for a target OS profile:
```bash
python3 scripts/render_quickstart.py --os fedora --output quick_start.md
```

Supported `--os` values:
- `auto`, `rhel`, `fedora`, `ubuntu`, `debian`, `macos`, `wsl`
