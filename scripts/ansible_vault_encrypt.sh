#!/usr/bin/env bash
# Encrypt a file with ansible-vault using the configured password file.
# Usage: scripts/ansible_vault_encrypt.sh /path/to/file.yml

set -euo pipefail
file=${1:-}
if [ -z "$file" ]; then
  echo "Usage: $0 /path/to/file.yml" >&2
  exit 2
fi

vault_pw_file=${ANSIBLE_VAULT_PASSWORD_FILE:-"$HOME/.ansible/conf/.vaultpass.txt"}
if [ ! -f "$vault_pw_file" ]; then
  echo "Vault password file not found: $vault_pw_file" >&2
  echo "Create it with: mkdir -p \$(dirname $vault_pw_file) && python3 -c \"import secrets; print(''.join(secrets.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(32)))\" > $vault_pw_file && chmod 600 $vault_pw_file" >&2
  exit 1
fi

ansible_vault_cmd=$(command -v ansible-vault || true)
if [ -z "$ansible_vault_cmd" ]; then
  echo "ansible-vault not found; install ansible-core or ensure ansible-vault is on PATH" >&2
  exit 1
fi

echo "Encrypting $file using vault password file $vault_pw_file"
"$ansible_vault_cmd" encrypt --vault-password-file="$vault_pw_file" "$file"
echo "Encrypted: $file"
