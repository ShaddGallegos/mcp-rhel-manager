#!/usr/bin/env bash
set -euo pipefail

# Provision bundled fixes from the repo into the user's ~/.mcp-ai/fixes
# Usage: ./provision_fixes.sh [--force]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLED_DIR="$SCRIPT_DIR/fixes"
# Default to Ansible-managed training fixes directory; allow override via env
USER_DIR="${ANSIBLE_FIXES_DIR:-$HOME/.ansible/.trainingdata/fixes}"
# Vault password file (env override allowed)
VAULT_PASS="${ANSIBLE_VAULT_PASSWORD_FILE:-$HOME/.ansible/conf/vaultpass.txt}"
LEGACY_VAULT_PASS="$HOME/.ansible/conf/.vaultpass.txt"
if [ ! -f "$VAULT_PASS" ] && [ -f "$LEGACY_VAULT_PASS" ]; then
  VAULT_PASS="$LEGACY_VAULT_PASS"
fi
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--force) FORCE=1; shift;;
    -h|--help) echo "Usage: $0 [--force]"; exit 0;;
    *) echo "Unknown arg: $1"; echo "Usage: $0 [--force]"; exit 2;;
  esac
done

if [[ ! -d "$BUNDLED_DIR" ]]; then
  echo "No bundled fixes found at: $BUNDLED_DIR" >&2
  exit 1
fi

mkdir -p "$USER_DIR"

for src in "$BUNDLED_DIR"/*; do
  [[ -f "$src" ]] || continue
  fn="$(basename "$src")"
  dst="$USER_DIR/$fn"

  if [[ $FORCE -eq 1 ]]; then
    if cp -a "$src" "$dst"; then
      echo "Installed $fn (overwritten)"
    else
      echo "Failed to install $fn"
      continue
    fi
  else
    if [[ -e "$dst" ]]; then
      echo "Skipping $fn (already exists)"
      continue
    fi
    if cp -a "$src" "$dst"; then
      echo "Installed $fn"
    else
      echo "Failed to install $fn"
      continue
    fi
  fi

  # Post-copy actions: patch plan JSONs to reference absolute helper script path
  if [[ -f "$dst" ]]; then
    if [[ -f "$USER_DIR/mcp_fix_pipewire.sh" && "$fn" == plan-*.json ]]; then
      # Replace any reference to the local script with its absolute path in the destination
      sed -i "s|mcp_fix_pipewire.sh|$USER_DIR/mcp_fix_pipewire.sh|g" "$dst" || true
    fi

    # Encrypt in-place if vault password file exists and ansible-vault available
    if [[ -f "$VAULT_PASS" ]] && command -v ansible-vault >/dev/null 2>&1; then
      # Only encrypt if file not already an Ansible vault (header check)
      if ! head -n1 "$dst" 2>/dev/null | grep -q "^\$ANSIBLE_VAULT;"; then
        if ansible-vault encrypt --vault-password-file "$VAULT_PASS" "$dst" >/dev/null 2>&1; then
          echo "Encrypted $fn with Ansible Vault"
        else
          echo "Failed to encrypt $fn with Ansible Vault"
        fi
      fi
    fi
  fi
done

echo "Provisioning complete -> $USER_DIR"
