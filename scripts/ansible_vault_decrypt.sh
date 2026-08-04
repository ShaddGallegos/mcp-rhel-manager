#!/usr/bin/env bash
# Decrypt a file with ansible-vault using the configured password file.
# Usage: scripts/ansible_vault_decrypt.sh /path/to/file.yml

set -euo pipefail
file=${1:-}
if [ -z "$file" ]; then
  echo "Usage: $0 /path/to/file.yml" >&2
  exit 2
fi

vault_pw_file=${ANSIBLE_VAULT_PASSWORD_FILE:-"$HOME/.ansible/conf/.vaultpass.txt"}
if [ ! -f "$vault_pw_file" ]; then
  echo "Vault password file not found: $vault_pw_file" >&2
  exit 1
fi

ansible_vault_cmd=$(command -v ansible-vault || true)
if [ -z "$ansible_vault_cmd" ]; then
  echo "ansible-vault not found; install ansible-core or ensure ansible-vault is on PATH" >&2
  exit 1
fi

echo "Decrypting $file using vault password file $vault_pw_file"
"$ansible_vault_cmd" decrypt --vault-password-file="$vault_pw_file" "$file"
echo "Decrypted: $file"
