#!/usr/bin/env bash
# ==============================================================================
# architect_genesis.sh — COMPATIBILITY SHIM
# This script has been merged into install_system.sh (unified installer).
# It forwards arguments for backward compatibility.
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SCRIPT_DIR/install_system.sh"

if [[ ! -f "$INSTALLER" ]]; then
  echo "ERROR: install_system.sh not found at $INSTALLER" >&2
  exit 1
fi

ARGS=("--apply")
for arg in "$@"; do
  case "$arg" in
    --globally|--global)
      ;; # system install is already the default
    -h|--help)
      exec "$INSTALLER" --help
      ;;
    *)
      ARGS+=("$arg")
      ;;
  esac
done

echo "architect_genesis.sh: this script has been merged into install_system.sh."
echo "Forwarding: install_system.sh ${ARGS[*]}"
echo ""
exec "$INSTALLER" "${ARGS[@]}"
