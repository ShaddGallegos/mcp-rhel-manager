#!/usr/bin/env bash
set -euo pipefail

# install_system.sh
# Idempotent, distro-agnostic installer for mcp-rhel-manager
# Default: dry-run. Use --apply to perform changes, --start to enable/start services.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="/opt/mcp-rhel-manager"
MCP_USER="mcp"
MCP_HOME="/var/lib/mcp"
VENV_DIR="$BASE_DIR/venv"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
BRIDGE_PORT="${BRIDGE_PORT:-1776}"

APPLY=0
START_SERVICES=0
YES=0
FORCE=0

usage(){
  cat <<EOF
Usage: $(basename "$0") [--apply] [--start] [--yes] [--force]

Options:
  --apply       Actually perform changes (default: dry-run)
  --start       Enable & start systemd services after install (requires --apply)
  --yes         Don't prompt for confirmation when applying
  --force       Overwrite existing install targets

This script is designed to be safe: by default it runs in dry-run mode and
prints the actions it would take. To execute, run with --apply (and --yes to
skip confirmation). It is idempotent and attempts to be distro-agnostic.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply|-a) APPLY=1; shift ;;
    --start|-s) START_SERVICES=1; shift ;;
    --yes|-y) YES=1; shift ;;
    --force|-f) FORCE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

run(){
  # Print then execute when --apply provided
  echo "+ $*"
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo "$@"
    else
      "$@"
    fi
  fi
}

write_temp_and_move(){
  # $1=dest_path, stdin=content
  local dest="$1"
  local tmp
  tmp="$(mktemp)"
  cat >"$tmp"
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mv "$tmp" "$dest"
      sudo chmod 644 "$dest"
      sudo chown root:root "$dest"
    else
      mv "$tmp" "$dest"
      chmod 644 "$dest"
      chown root:root "$dest"
    fi
    echo "Wrote $dest"
  else
    echo "DRY-RUN: would write $dest (first 200 lines):"
    sed -n '1,200p' "$tmp" || true
    rm -f "$tmp"
  fi
}

prompt_confirm(){
  if [[ $YES -eq 1 ]]; then return 0; fi
  read -r -p "$1 [y/N]: " ans
  case "$ans" in
    [Yy]* ) return 0 ;;
    * ) return 1 ;;
  esac
}

echo "Install script for mcp-rhel-manager"
echo "Base dir: $BASE_DIR"
echo "MCP user: $MCP_USER (home: $MCP_HOME)"
echo "OLLAMA URL: $OLLAMA_URL  Bridge port: $BRIDGE_PORT"
echo

if [[ $APPLY -ne 1 ]]; then
  echo "Running in dry-run mode. Use --apply to make changes."
fi

if [[ $APPLY -eq 1 && $EUID -ne 0 ]]; then
  echo "This script will perform system changes and requires root. It will use sudo for privileged commands."
fi

if [[ $APPLY -eq 1 && $YES -ne 1 ]]; then
  echo "About to perform actions on this host:" 
  echo " - create user: $MCP_USER"
  echo " - create $BASE_DIR and copy files"
  echo " - create venv: $VENV_DIR and install python deps"
  echo " - place systemd units and sudoers snippets"
  echo
  if ! prompt_confirm "Proceed?"; then
    echo "Aborting."; exit 1
  fi
fi

# 1) Detect package manager (only used when --apply)
PKG_CMD=""
PKG_INSTALL_OPTS=""
if command -v dnf >/dev/null 2>&1; then
  PKG_CMD="dnf"
  PKG_INSTALL_OPTS="-y"
elif command -v apt-get >/dev/null 2>&1; then
  PKG_CMD="apt-get"
  PKG_INSTALL_OPTS="-y"
elif command -v yum >/dev/null 2>&1; then
  PKG_CMD="yum"
  PKG_INSTALL_OPTS="-y"
else
  PKG_CMD=""
fi

install_prereqs(){
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would install OS prerequisite packages (python3, venv, pip, rsync, lm_sensors, smartmontools, pciutils, jq, git)."
    return 0
  fi

  if [[ -z "$PKG_CMD" ]]; then
    echo "No supported package manager found; please install prerequisites manually." >&2
    return 1
  fi

  if [[ $PKG_CMD == "dnf" || $PKG_CMD == "yum" ]]; then
    run $PKG_CMD $PKG_INSTALL_OPTS install -y python3 python3-venv python3-pip python3-devel gcc make lm_sensors smartmontools pciutils jq rsync git
  else
    # apt-get
    run apt-get update
    run apt-get $PKG_INSTALL_OPTS install python3 python3-venv python3-pip python3-dev build-essential lm-sensors smartmontools pciutils jq rsync git
  fi
}

create_user_and_dirs(){
  if id "$MCP_USER" >/dev/null 2>&1; then
    echo "User $MCP_USER already exists"
  else
    echo "Creating system user $MCP_USER"
    run useradd --system --no-create-home --home-dir "$MCP_HOME" --shell /sbin/nologin "$MCP_USER" || run adduser --system --no-create-home --home-dir "$MCP_HOME" --shell /sbin/nologin "$MCP_USER"
  fi

  run mkdir -p "$BASE_DIR"
  run mkdir -p "$MCP_HOME"
  # ensure proper ownership after files copied
}

deploy_files(){
  echo "Syncing files from $SCRIPT_DIR to $BASE_DIR"
  # Exclude venv, git metadata, and large artifacts
  run rsync -a --delete --exclude ".git" --exclude "venv" --exclude ".venv" --exclude "artifacts" --exclude "artifacts_user" "$SCRIPT_DIR/" "$BASE_DIR/"
  # chown to mcp
  run chown -R $MCP_USER:$MCP_USER "$BASE_DIR"
}

create_venv_and_install(){
  echo "Creating venv at $VENV_DIR"
  run python3 -m venv "$VENV_DIR"
  run "$VENV_DIR/bin/pip" install -U pip setuptools wheel
  if [[ -f "$BASE_DIR/requirements.txt" ]]; then
    echo "Installing Python requirements"
    run "$VENV_DIR/bin/pip" install -r "$BASE_DIR/requirements.txt"
  fi
}

write_systemd_units(){
  echo "Preparing systemd unit files"
  # mcp-bridge.service
  tmpfile1="$(mktemp)"
  cat >"$tmpfile1" <<EOF
[Unit]
Description=MCP Ollama Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$MCP_USER
Group=$MCP_USER
WorkingDirectory=$BASE_DIR
Environment=HOME=$MCP_HOME
ExecStart=$VENV_DIR/bin/python -m ollama_mcp_bridge.main --port $BRIDGE_PORT --ollama-url $OLLAMA_URL
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mv "$tmpfile1" /etc/systemd/system/mcp-bridge.service
      sudo chmod 644 /etc/systemd/system/mcp-bridge.service
    else
      mv "$tmpfile1" /etc/systemd/system/mcp-bridge.service
      chmod 644 /etc/systemd/system/mcp-bridge.service
    fi
    echo "/etc/systemd/system/mcp-bridge.service written"
  else
    echo "DRY-RUN: would write /etc/systemd/system/mcp-bridge.service (first 200 lines):"
    sed -n '1,200p' "$tmpfile1" || true
    rm -f "$tmpfile1"
  fi

  # mcp-ai-remediator.service
  tmpfile2="$(mktemp)"
  cat >"$tmpfile2" <<EOF
[Unit]
Description=MCP AI Remediator
After=network.target

[Service]
Type=simple
User=$MCP_USER
Group=$MCP_USER
WorkingDirectory=$BASE_DIR
Environment=HOME=$MCP_HOME
ExecStart=$VENV_DIR/bin/python $BASE_DIR/mcp-ai/remediate.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mv "$tmpfile2" /etc/systemd/system/mcp-ai-remediator.service
      sudo chmod 644 /etc/systemd/system/mcp-ai-remediator.service
    else
      mv "$tmpfile2" /etc/systemd/system/mcp-ai-remediator.service
      chmod 644 /etc/systemd/system/mcp-ai-remediator.service
    fi
    echo "/etc/systemd/system/mcp-ai-remediator.service written"
  else
    echo "DRY-RUN: would write /etc/systemd/system/mcp-ai-remediator.service (first 200 lines):"
    sed -n '1,200p' "$tmpfile2" || true
    rm -f "$tmpfile2"
  fi

  # mcp-ai-collector.service + timer
  tmpfile3="$(mktemp)"
  cat >"$tmpfile3" <<EOF
[Unit]
Description=MCP AI Collector

[Service]
User=$MCP_USER
WorkingDirectory=$BASE_DIR
ExecStart=$VENV_DIR/bin/python $BASE_DIR/mcp-ai/collector.py
EOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mv "$tmpfile3" /etc/systemd/system/mcp-ai-collector.service
      sudo chmod 644 /etc/systemd/system/mcp-ai-collector.service
    else
      mv "$tmpfile3" /etc/systemd/system/mcp-ai-collector.service
      chmod 644 /etc/systemd/system/mcp-ai-collector.service
    fi
    echo "/etc/systemd/system/mcp-ai-collector.service written"
  else
    echo "DRY-RUN: would write /etc/systemd/system/mcp-ai-collector.service (first 200 lines):"
    sed -n '1,200p' "$tmpfile3" || true
    rm -f "$tmpfile3"
  fi

  tmpfile4="$(mktemp)"
  cat >"$tmpfile4" <<EOF
[Unit]
Description=Run MCP AI Collector every 30 minutes

[Timer]
OnCalendar=*:0/30
Persistent=true

[Install]
WantedBy=timers.target
EOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mv "$tmpfile4" /etc/systemd/system/mcp-ai-collector.timer
      sudo chmod 644 /etc/systemd/system/mcp-ai-collector.timer
    else
      mv "$tmpfile4" /etc/systemd/system/mcp-ai-collector.timer
      chmod 644 /etc/systemd/system/mcp-ai-collector.timer
    fi
    echo "/etc/systemd/system/mcp-ai-collector.timer written"
  else
    echo "DRY-RUN: would write /etc/systemd/system/mcp-ai-collector.timer (first 200 lines):"
    sed -n '1,200p' "$tmpfile4" || true
    rm -f "$tmpfile4"
  fi

}

write_sudoers(){
  echo "Writing /etc/sudoers.d/mcp-ai (narrow permissions)"
  sudo_file="/etc/sudoers.d/mcp-ai"
  tmp="$(mktemp)"
  cat >"$tmp" <<EOF
# Allow mcp user to run the remediation runner and remediate.py as root without password
Cmnd_Alias MCPAI_CMDS = $BASE_DIR/mcp-ai/runner.py, $BASE_DIR/mcp-ai/remediate.py
mcp ALL=(root) NOPASSWD: MCPAI_CMDS
EOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mv "$tmp" "$sudo_file"
      sudo chmod 440 "$sudo_file"
    else
      mv "$tmp" "$sudo_file"
      chmod 440 "$sudo_file"
    fi
    # validate
    if ! sudo visudo -cf "$sudo_file" >/dev/null 2>&1; then
      echo "ERROR: sudoers file syntax invalid"; return 1
    fi
    echo "$sudo_file written and validated"
  else
    echo "DRY-RUN: would write $sudo_file (content):"
    sed -n '1,200p' "$tmp" || true
    rm -f "$tmp"
  fi
}

write_mcp_config(){
  cfg_dir="$MCP_HOME/.mcp-ai"
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mkdir -p "$cfg_dir"
      sudo chown -R $MCP_USER:$MCP_USER "$cfg_dir"
    else
      mkdir -p "$cfg_dir"
      chown -R $MCP_USER:$MCP_USER "$cfg_dir"
    fi
  else
    echo "DRY-RUN: would create $cfg_dir"
  fi

  cfg_file="$cfg_dir/config.json"
  tmp="$(mktemp)"
  cat >"$tmp" <<EOF
{
  "ollama_url": "$OLLAMA_URL",
  "bridge_port": $BRIDGE_PORT,
  "allow_auto_fix": false
}
EOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mv "$tmp" "$cfg_file"
      sudo chown $MCP_USER:$MCP_USER "$cfg_file"
      sudo chmod 600 "$cfg_file"
    else
      mv "$tmp" "$cfg_file"
      chown $MCP_USER:$MCP_USER "$cfg_file"
      chmod 600 "$cfg_file"
    fi
    echo "Wrote $cfg_file"
  else
    echo "DRY-RUN: would write $cfg_file (content):"
    sed -n '1,200p' "$tmp" || true
    rm -f "$tmp"
  fi
}

post_install(){
  echo "Running post-install steps"
  # daemon-reload
  if [[ $APPLY -eq 1 ]]; then
    run systemctl daemon-reload
    if [[ $START_SERVICES -eq 1 ]]; then
      run systemctl enable --now mcp-bridge.service mcp-ai-remediator.service mcp-ai-collector.timer || true
    fi
    # create a convenient system-wide command for HAL if wrapper exists
    if [[ -f "$BASE_DIR/bin/HAL" ]]; then
      run ln -sf "$BASE_DIR/bin/HAL" /usr/local/bin/HAL
      run chmod +x "$BASE_DIR/bin/HAL"
      run chown $MCP_USER:$MCP_USER "$BASE_DIR/bin/HAL" || true
    fi
  else
    echo "DRY-RUN: would run systemctl daemon-reload and optionally enable/start services"
  fi
}

check_ollama(){
  echo "Checking Ollama/LLM endpoint: $OLLAMA_URL"
  if command -v curl >/dev/null 2>&1; then
    if curl -sfS "$OLLAMA_URL" >/dev/null 2>&1; then
      echo "OLLAMA endpoint reachable"
    else
      echo "WARNING: OLLAMA endpoint at $OLLAMA_URL not reachable. Ensure Ollama or your LLM endpoint is running."
    fi
  else
    echo "curl not installed; cannot test Ollama reachability"
  fi
}

main(){
  echo "Starting install (apply=$APPLY start=$START_SERVICES)"
  install_prereqs || true
  create_user_and_dirs
  deploy_files
  create_venv_and_install
  write_systemd_units
  write_sudoers
  write_mcp_config
  post_install
  check_ollama
  echo "Install script completed (dry-run=$((1-APPLY)))"
}

main
