# HAL Security & Secrets Management

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

This document describes HAL's comprehensive security features for managing secrets, vault passwords, and detecting unencrypted sensitive information in git repositories.

## Overview

HAL now includes a complete security framework that:

1. **Manages Vault Password** - Creates and maintains `~/.ansible/conf/.vaultpass.txt`
2. **Scans Git Repos** - Finds unencrypted secrets in tracked files
3. **Detects Credential Exposure** - Identifies plaintext credentials in git config
4. **Encrypts Sensitive Files** - Uses ansible-vault to encrypt detected secrets
5. **Integrates with Auto-Healing** - All checks run in health check mode 3

## Vault Password Management

### Automatic Setup

The vault password file is automatically created and managed when you run:

```bash
python3 scripts/hal.py 'system health'
# Choose: y for health check, then 3 for full auto-remediation
```

### Manual Setup

```bash
mkdir -p ~/.ansible/conf
python3 scripts/hal.py 'system health'
```

### File Location

- **Path**: `~/.ansible/conf/.vaultpass.txt`
- **Permissions**: 600 (read/write for owner only)
- **Contents**: 32-character random password (auto-generated if missing)

### Verify Vault Setup

```bash
cd /home/sgallego/GIT/mcp-rhel-manager
python3 scripts/hal-security-audit.py check
```

Output:

```
✓ Vault password file ready: /home/sgallego/.ansible/conf/.vaultpass.txt
```

## Detecting Secrets in Git Repos

### Using HAL Health Check

```bash
python3 scripts/hal.py 'system health'
# Mode 3: Full auto-remediation
```

This will:

- Scan up to 39 git repositories
- Find files with potential unencrypted secrets
- Report findings with file paths and issue types

### Using Security Audit Tool

Run the dedicated security audit script:

```bash
# Quick check of vault status
python3 scripts/hal-security-audit.py check

# Audit all git repos for secrets
python3 scripts/hal-security-audit.py audit

# Audit specific repo
python3 scripts/hal-security-audit.py audit /path/to/repo

# Scan with custom depth
python3 scripts/hal-security-audit.py audit --depth=2

# Generate JSON report
python3 scripts/hal-security-audit.py report > security_report.json
```

## Detected Secret Patterns

The security scanner looks for:

| Pattern         | Example                    | Issue                        |
| --------------- | -------------------------- | ---------------------------- |
| **password**    | `password: "secret123"`    | Plaintext password in config |
| **api_key**     | `api_key: abc123def456`    | Exposed API key              |
| **secret**      | `secret: mysecret`         | Generic secret field         |
| **token**       | `token: ghp_xyz123`        | Authentication token         |
| **aws_secret**  | `aws_secret: ...`          | AWS credentials              |
| **private_key** | References to `.pem` files | Private key exposure         |

## Encrypting Sensitive Files

### Prerequisites

Ensure ansible-vault is installed:

```bash
pip install ansible-core
# or
sudo dnf install ansible-core
```

### Encrypt a File

```bash
python3 scripts/hal-security-audit.py encrypt /path/to/sensitive/file.yml
```

Output:

```
✓ Encrypted: /path/to/sensitive/file.yml
```

### Decrypt a File

```bash
python3 scripts/hal-security-audit.py decrypt /path/to/sensitive/file.yml
```

Output:

```
✓ Decrypted: /path/to/sensitive/file.yml
```

### Batch Encrypt Files

```bash
# Encrypt all YAML files in a directory
for file in /path/to/dir/*.yml; do
    python3 scripts/hal-security-audit.py encrypt "$file"
done
```

### Using Vault Password in Git

Store vault password path for automatic decryption:

```bash
# Add to ~/.ansible/ansible.cfg
echo "vault_password_file = ~/.ansible/conf/.vaultpass.txt" >> ~/.ansible/ansible.cfg

# Or use environment variable
export ANSIBLE_VAULT_PASSWORD_FILE=~/.ansible/conf/.vaultpass.txt

# Ansible will now auto-decrypt vault files
ansible-playbook playbook.yml
```

## Security Checks in HAL Health Scan

When you run mode 3 (full auto-remediation), HAL checks:

### 1. Vault Password Management

- Creates `~/.ansible/conf/.vaultpass.txt` if missing
- Ensures correct file permissions (600)
- Verifies password has content

### 2. Git Repository Scanning

- Locates up to 39 git repositories
- Scans tracked files for secret patterns
- Reports findings by repo and file
- Limits checks to prevent performance impact

### 3. Git Credentials Exposure

- Checks `.gitconfig` for plaintext credentials
- Detects `.git-credentials` files (should use credential helper)
- Validates SSH key references

## Example Security Audit Output

```
============================================================
SECRETS & VAULT SECURITY TEST
============================================================

[VAULT]
✓ vault password file already exists and contains a password

[GIT_SECRETS]
✓ scanned 39 git repository(ies) for secrets
ℹ found 57 file(s) with potential unencrypted secrets

[GIT_CREDS]
✓ .gitconfig checked (no obvious credentials)

============================================================
SUMMARY: 3 checks completed
============================================================
```

## Best Practices

### 1. Regular Scanning

Run security checks weekly:

```bash
# Add to crontab
0 2 * * 0 cd /path/to/mcp-rhel-manager && python3 scripts/hal.py 'system health' | grep -i secret
```

### 2. Encrypt Sensitive Files

```bash
# Identify files to encrypt
python3 scripts/hal-security-audit.py audit | grep "\.yml\|\.json\|\.env"

# Encrypt them
python3 scripts/hal-security-audit.py encrypt path/to/file.yml
```

### 3. Use .gitignore

Exclude sensitive files from git:

```bash
# Add to .gitignore
.vaultpass.txt
*.key
*.pem
*.pfx
credentials
secrets/
.env.local
```

### 4. Store Vault Password Safely

- **Never commit** `.vaultpass.txt` to git
- **Use CI/CD secrets** for storing vault password in pipelines
- **Restrict permissions** to 600 (owner read/write only)
- **Backup securely** (encrypted backup, separate location)

### 5. Environment Variables

```bash
# Set vault password via environment
export ANSIBLE_VAULT_PASSWORD_FILE=~/.ansible/conf/.vaultpass.txt

# Use in scripts
ansible-vault encrypt --vault-password-file=$ANSIBLE_VAULT_PASSWORD_FILE file.yml
```

## Troubleshooting

### Vault Password File Not Found

```bash
# Create it manually
mkdir -p ~/.ansible/conf
python3 -c "import secrets; print(''.join(secrets.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(32)))" > ~/.ansible/conf/.vaultpass.txt
chmod 600 ~/.ansible/conf/.vaultpass.txt
```

### ansible-vault Not Found

```bash
# Install ansible
pip install ansible-core

# Or via dnf
sudo dnf install ansible-core
```

### Cannot Decrypt File

```bash
# Verify vault password file has content
cat ~/.ansible/conf/.vaultpass.txt

# Try decrypting with verbose output
ansible-vault view --vault-password-file=~/.ansible/conf/.vaultpass.txt file.yml -vvv
```

### Too Many False Positives

The scanner uses broad patterns to avoid missing secrets. False positives include:

- Example files (`.example`, `.sample`)
- Documentation files (`README.md`)
- Configuration templates
- Test data

**Resolution**: Manually review flagged files and decide if they need encryption.

## Integration with CI/CD

### GitHub Actions

```yaml
- name: Run HAL Security Audit
  run: |
    python3 scripts/hal-security-audit.py audit --repos=5 > security_report.json
    
- name: Check for Critical Secrets
  run: |
    if grep -q '"private_key"' security_report.json; then
      echo "ERROR: Found private keys in tracked files!"
      exit 1
    fi
```

### GitLab CI

```yaml
security_scan:
  script:
    - python3 scripts/hal-security-audit.py audit --repos=5
    - python3 scripts/hal-security-audit.py report
```

## HAL Auto-Healing Integration

All security checks are part of HAL's comprehensive health check:

```python
# In hal.py, _run_comprehensive_auto_healing() includes:
results['vault'] = _manage_vault_password()
results['git_secrets'] = _scan_git_repos_for_secrets()
results['git_creds'] = _check_git_credentials_exposure()
```

## Files Created

| File                             | Purpose                                       |
| -------------------------------- | --------------------------------------------- |
| `scripts/hal-security-audit.py`          | Standalone security audit tool                |
| `~/.ansible/conf/.vaultpass.txt` | Vault password (auto-created)                 |
| `~/.ansible/ansible.cfg`         | Optional: configure vault password (optional) |

## Summary

- ✅ Vault password automatically created and managed
- ✅ Git repositories scanned for unencrypted secrets
- ✅ Credentials exposure detected and reported
- ✅ Files can be encrypted/decrypted with ansible-vault
- ✅ All checks integrated into HAL health check mode 3
- ✅ Standalone security audit tool for manual scanning
- ✅ CI/CD integration ready

Run `python3 scripts/hal.py 'system health'` with mode 3 to start using these security features!
