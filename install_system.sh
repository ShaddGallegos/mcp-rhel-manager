#!/usr/bin/env bash
set -euo pipefail

# install_system.sh
# Idempotent, distro-agnostic installer for mcp-rhel-manager
# Default: dry-run. Use --apply to perform changes, --start to enable/start services.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="/opt/mcp-rhel-manager"
MCP_USER="mcp"
MCP_HOME="/var/lib/mcp"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
BRIDGE_PORT="${BRIDGE_PORT:-1776}"
VENV_DIR=""

APPLY=0
START_SERVICES=0
YES=0
FORCE=0
VERIFY_MODE=0
VERIFY_JSON=0
ROLLBACK_ON_FAIL=1

usage(){
  cat <<EOF
Usage: $(basename "$0") [--apply] [--start] [--verify] [--verify-json] [--yes] [--force]
                          [--base-dir PATH] [--user NAME] [--home PATH]

Options:
  --apply       Actually perform changes (default: dry-run)
  --start       Enable & start systemd services after install (requires --apply)
  --yes         Don't prompt for confirmation when applying
  --force       Overwrite existing install targets
  --verify      Run post-install verification checks (can be used standalone)
  --verify-json Run compact JSON verification output (implies --verify)
  --base-dir    Install root (default: /opt/mcp-rhel-manager)
  --user        Service account username (default: mcp)
  --home        Service account home directory (default: /var/lib/mcp)
  --no-rollback Disable automatic rollback on failure in --apply mode

This script is designed to be safe: by default it runs in dry-run mode and
prints the actions it would take. To execute, run with --apply (and --yes to
skip confirmation). It is idempotent and attempts to be distro-agnostic.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply|-a) APPLY=1; shift ;;
    --start|-s) START_SERVICES=1; shift ;;
    --verify) VERIFY_MODE=1; shift ;;
    --verify-json) VERIFY_MODE=1; VERIFY_JSON=1; shift ;;
    --yes|-y) YES=1; shift ;;
    --force|-f) FORCE=1; shift ;;
    --base-dir) BASE_DIR="${2:-}"; shift 2 ;;
    --user) MCP_USER="${2:-}"; shift 2 ;;
    --home) MCP_HOME="${2:-}"; shift 2 ;;
    --no-rollback) ROLLBACK_ON_FAIL=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

fail(){
  echo "ERROR: $*" >&2
  exit 1
}

[[ -n "$BASE_DIR" ]] || fail "--base-dir requires a value"
[[ -n "$MCP_USER" ]] || fail "--user requires a value"
[[ -n "$MCP_HOME" ]] || fail "--home requires a value"
VENV_DIR="$BASE_DIR/venv"

# Rollback state (best-effort cleanup on failure in apply mode)
CREATED_USER=0
CREATED_BASE_DIR=0
CREATED_HOME_DIR=0
CREATED_VENV=0
WROTE_UNIT_BRIDGE=0
WROTE_UNIT_REMEDIATOR=0
WROTE_UNIT_COLLECTOR=0
WROTE_UNIT_TIMER=0
WROTE_SUDOERS=0
WROTE_MCP_CONFIG=0

rollback_on_error(){
  local rc=$?
  if [[ $ROLLBACK_ON_FAIL -ne 1 || $APPLY -ne 1 ]]; then
    exit "$rc"
  fi
  echo "ERROR: install failed (rc=$rc); starting best-effort rollback..." >&2

  if [[ $WROTE_UNIT_BRIDGE -eq 1 || $WROTE_UNIT_REMEDIATOR -eq 1 || $WROTE_UNIT_COLLECTOR -eq 1 || $WROTE_UNIT_TIMER -eq 1 ]]; then
    run systemctl disable --now mcp-bridge.service mcp-ai-remediator.service mcp-ai-collector.timer >/dev/null 2>&1 || true
    [[ $WROTE_UNIT_BRIDGE -eq 1 ]] && run rm -f /etc/systemd/system/mcp-bridge.service >/dev/null 2>&1 || true
    [[ $WROTE_UNIT_REMEDIATOR -eq 1 ]] && run rm -f /etc/systemd/system/mcp-ai-remediator.service >/dev/null 2>&1 || true
    [[ $WROTE_UNIT_COLLECTOR -eq 1 ]] && run rm -f /etc/systemd/system/mcp-ai-collector.service >/dev/null 2>&1 || true
    [[ $WROTE_UNIT_TIMER -eq 1 ]] && run rm -f /etc/systemd/system/mcp-ai-collector.timer >/dev/null 2>&1 || true
    run systemctl daemon-reload >/dev/null 2>&1 || true
  fi

  if [[ $WROTE_SUDOERS -eq 1 ]]; then
    run rm -f /etc/sudoers.d/mcp-ai >/dev/null 2>&1 || true
  fi

  if [[ $CREATED_VENV -eq 1 ]]; then
    run rm -rf "$VENV_DIR" >/dev/null 2>&1 || true
  fi

  if [[ $WROTE_MCP_CONFIG -eq 1 ]]; then
    run rm -f "$MCP_HOME/.mcp-ai/config.json" >/dev/null 2>&1 || true
  fi

  if [[ $CREATED_BASE_DIR -eq 1 ]]; then
    run rm -rf "$BASE_DIR" >/dev/null 2>&1 || true
  fi

  if [[ $CREATED_HOME_DIR -eq 1 ]]; then
    run rm -rf "$MCP_HOME" >/dev/null 2>&1 || true
  fi

  if [[ $CREATED_USER -eq 1 ]]; then
    run userdel "$MCP_USER" >/dev/null 2>&1 || true
  fi

  echo "Rollback complete (best-effort)." >&2
  exit "$rc"
}

trap rollback_on_error ERR

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

if [[ $VERIFY_JSON -ne 1 ]]; then
  echo "Install script for mcp-rhel-manager"
  echo "Base dir: $BASE_DIR"
  echo "MCP user: $MCP_USER (home: $MCP_HOME)"
  echo "OLLAMA URL: $OLLAMA_URL  Bridge port: $BRIDGE_PORT"
  echo
fi

[[ -d "$SCRIPT_DIR" ]] || fail "Script directory not found: $SCRIPT_DIR"
[[ "$BRIDGE_PORT" =~ ^[0-9]+$ ]] || fail "BRIDGE_PORT must be numeric (got: $BRIDGE_PORT)"
if (( BRIDGE_PORT < 1 || BRIDGE_PORT > 65535 )); then
  fail "BRIDGE_PORT out of range: $BRIDGE_PORT"
fi

if [[ $START_SERVICES -eq 1 && $APPLY -ne 1 ]]; then
  fail "--start requires --apply"
fi

if [[ $APPLY -eq 1 && $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || fail "sudo is required when running --apply as non-root"
fi

if [[ $APPLY -ne 1 && $VERIFY_JSON -ne 1 ]]; then
  echo "Running in dry-run mode. Use --apply to make changes."
fi

if [[ $APPLY -eq 1 && $EUID -ne 0 && $VERIFY_JSON -ne 1 ]]; then
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
    # Note: python3-venv is built into python3 on RHEL 10+, so we don't specify it separately
    run "$PKG_CMD" install $PKG_INSTALL_OPTS python3 python3-pip python3-devel gcc make lm_sensors smartmontools pciutils jq rsync git curl || true
  else
    # apt-get
    run apt-get update
    run apt-get install $PKG_INSTALL_OPTS python3 python3-venv python3-pip python3-dev build-essential lm-sensors smartmontools pciutils jq rsync git curl || true
  fi
}

create_user_and_dirs(){
  if id "$MCP_USER" >/dev/null 2>&1; then
    echo "User $MCP_USER already exists"
  else
    echo "Creating system user $MCP_USER"
    run useradd --system --no-create-home --home-dir "$MCP_HOME" --shell /sbin/nologin "$MCP_USER" || run adduser --system --no-create-home --home-dir "$MCP_HOME" --shell /sbin/nologin "$MCP_USER"
    [[ $APPLY -eq 1 ]] && CREATED_USER=1
  fi

  if [[ ! -d "$BASE_DIR" ]]; then
    [[ $APPLY -eq 1 ]] && CREATED_BASE_DIR=1
  fi
  run mkdir -p "$BASE_DIR"

  if [[ ! -d "$MCP_HOME" ]]; then
    [[ $APPLY -eq 1 ]] && CREATED_HOME_DIR=1
  fi
  run mkdir -p "$MCP_HOME"
  # ensure proper ownership after files copied
}

deploy_files(){
  echo "Syncing files from $SCRIPT_DIR to $BASE_DIR"
  if ! command -v rsync >/dev/null 2>&1; then
    fail "rsync is required for deploy_files"
  fi

  if [[ $FORCE -ne 1 ]]; then
    # Prevent accidental destructive sync into an unrelated existing target.
    if [[ -d "$BASE_DIR" && -n "$(ls -A "$BASE_DIR" 2>/dev/null || true)" ]]; then
      if [[ ! -f "$BASE_DIR/install_system.sh" ]]; then
        fail "$BASE_DIR is non-empty and does not look like this project. Re-run with --force to override."
      fi
    fi
  fi

  # Exclude venv, git metadata, and large artifacts
  local rsync_delete=""
  if [[ $FORCE -eq 1 ]]; then
    rsync_delete="--delete"
  fi
  run rsync -a $rsync_delete --exclude ".git" --exclude "venv" --exclude ".venv" --exclude "artifacts" --exclude "artifacts_user" "$SCRIPT_DIR/" "$BASE_DIR/"
  # Ensure wrapper is executable even if source permissions were not preserved.
  if [[ -f "$BASE_DIR/bin/HAL" ]]; then
    run chmod +x "$BASE_DIR/bin/HAL"
  fi
  # Ensure core setup/ops scripts are executable after sync.
  for f in \
    "$BASE_DIR/architect_genesis.sh" \
    "$BASE_DIR/install_system.sh" \
    "$BASE_DIR/auto-fixer.sh" \
    "$BASE_DIR/mcp-ai/start-bridge.sh" \
    "$BASE_DIR/mcp-ai/enable_services.sh" \
    "$BASE_DIR/mcp-ai/setup_auto_ingest.sh"; do
    if [[ -f "$f" ]]; then
      run chmod +x "$f"
    fi
  done
  # chown to mcp
  run chown -R $MCP_USER:$MCP_USER "$BASE_DIR"
}

create_venv_and_install(){
  echo "Creating venv at $VENV_DIR"
  if [[ ! -d "$VENV_DIR" ]]; then
    [[ $APPLY -eq 1 ]] && CREATED_VENV=1
  fi
  run python3 -m venv "$VENV_DIR"
  # Ensure venv executables have execute permission (required after sudo venv creation)
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo chmod +x "$VENV_DIR/bin"/*
    else
      chmod +x "$VENV_DIR/bin"/*
    fi
  fi
  echo "+ $VENV_DIR/bin/pip install -U pip setuptools wheel"
  run "$VENV_DIR/bin/pip" install -U pip setuptools wheel
  if [[ -f "$BASE_DIR/requirements.txt" ]]; then
    echo "Installing Python requirements"
    run "$VENV_DIR/bin/pip" install -r "$BASE_DIR/requirements.txt"
  else
    echo "WARNING: requirements.txt not found at $BASE_DIR/requirements.txt"
  fi
  # Bridge runtime is managed by requirements.txt to avoid resolver conflicts
  # with other pinned tools (for example aider-chat).
  if [[ $APPLY -eq 1 ]]; then
    if ! "$VENV_DIR/bin/pip" show ollama-mcp-bridge >/dev/null 2>&1; then
      run "$VENV_DIR/bin/pip" install ollama-mcp-bridge
    fi
  else
    echo "DRY-RUN: would ensure ollama-mcp-bridge is present via requirements.txt"
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
      sudo install -m 644 -o root -g root "$tmpfile1" /etc/systemd/system/mcp-bridge.service
    else
      install -m 644 -o root -g root "$tmpfile1" /etc/systemd/system/mcp-bridge.service
    fi
    rm -f "$tmpfile1"
    command -v restorecon >/dev/null 2>&1 && run restorecon -v /etc/systemd/system/mcp-bridge.service >/dev/null 2>&1 || true
    echo "/etc/systemd/system/mcp-bridge.service written"
    WROTE_UNIT_BRIDGE=1
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
      sudo install -m 644 -o root -g root "$tmpfile2" /etc/systemd/system/mcp-ai-remediator.service
    else
      install -m 644 -o root -g root "$tmpfile2" /etc/systemd/system/mcp-ai-remediator.service
    fi
    rm -f "$tmpfile2"
    command -v restorecon >/dev/null 2>&1 && run restorecon -v /etc/systemd/system/mcp-ai-remediator.service >/dev/null 2>&1 || true
    echo "/etc/systemd/system/mcp-ai-remediator.service written"
    WROTE_UNIT_REMEDIATOR=1
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
      sudo install -m 644 -o root -g root "$tmpfile3" /etc/systemd/system/mcp-ai-collector.service
    else
      install -m 644 -o root -g root "$tmpfile3" /etc/systemd/system/mcp-ai-collector.service
    fi
    rm -f "$tmpfile3"
    command -v restorecon >/dev/null 2>&1 && run restorecon -v /etc/systemd/system/mcp-ai-collector.service >/dev/null 2>&1 || true
    echo "/etc/systemd/system/mcp-ai-collector.service written"
    WROTE_UNIT_COLLECTOR=1
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
      sudo install -m 644 -o root -g root "$tmpfile4" /etc/systemd/system/mcp-ai-collector.timer
    else
      install -m 644 -o root -g root "$tmpfile4" /etc/systemd/system/mcp-ai-collector.timer
    fi
    rm -f "$tmpfile4"
    command -v restorecon >/dev/null 2>&1 && run restorecon -v /etc/systemd/system/mcp-ai-collector.timer >/dev/null 2>&1 || true
    echo "/etc/systemd/system/mcp-ai-collector.timer written"
    WROTE_UNIT_TIMER=1
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
$MCP_USER ALL=(root) NOPASSWD: MCPAI_CMDS
EOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo install -m 440 -o root -g root "$tmp" "$sudo_file"
    else
      install -m 440 -o root -g root "$tmp" "$sudo_file"
    fi
    rm -f "$tmp"
    command -v restorecon >/dev/null 2>&1 && run restorecon -v "$sudo_file" >/dev/null 2>&1 || true
    # validate
    if [[ $EUID -eq 0 ]]; then
      if ! visudo -cf "$sudo_file" >/dev/null 2>&1; then
        echo "ERROR: sudoers file syntax invalid"; return 1
      fi
    else
      if ! sudo visudo -cf "$sudo_file" >/dev/null 2>&1; then
        echo "ERROR: sudoers file syntax invalid"; return 1
      fi
    fi
    echo "$sudo_file written and validated"
    WROTE_SUDOERS=1
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
    WROTE_MCP_CONFIG=1
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
      run systemctl enable --now mcp-bridge.service mcp-ai-remediator.service mcp-ai-collector.timer
    fi
    # create a convenient system-wide command for HAL if wrapper exists
    if [[ -f "$BASE_DIR/bin/HAL" ]]; then
      run chmod +x "$BASE_DIR/bin/HAL"
      run ln -sf "$BASE_DIR/bin/HAL" /usr/local/bin/HAL
      run ln -sf "$BASE_DIR/bin/HAL" /usr/local/bin/hal
      run chown $MCP_USER:$MCP_USER "$BASE_DIR/bin/HAL" || true
    fi
  else
    echo "DRY-RUN: would run systemctl daemon-reload, optionally enable/start services, and link HAL/hal in /usr/local/bin"
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

verify_install(){
  local failed=0
  local ok_count=0
  local warn_count=0
  local fail_count=0
  local results_file
  results_file="$(mktemp)"

  emit_line(){
    if [[ $VERIFY_JSON -ne 1 ]]; then
      echo "$*"
    fi
  }

  add_result(){
    # $1=status (ok|warn|fail), $2=check key, $3=message
    local st="$1"; shift
    local key="$1"; shift
    local msg="$*"
    msg="${msg//|//}"
    echo "${st}|${key}|${msg}" >>"$results_file"
    case "$st" in
      ok) ok_count=$((ok_count+1)) ;;
      warn) warn_count=$((warn_count+1)) ;;
      fail) fail_count=$((fail_count+1)) ;;
    esac
  }

  emit_line "Running verification checks..."

  if [[ -d "$BASE_DIR" ]]; then
    add_result ok base_dir "base dir exists: $BASE_DIR"
    emit_line "OK: base dir exists: $BASE_DIR"
  else
    add_result fail base_dir "base dir missing: $BASE_DIR"
    emit_line "FAIL: base dir missing: $BASE_DIR"
    failed=1
  fi

  if [[ -x "$VENV_DIR/bin/python" ]]; then
    add_result ok venv_python "venv python exists: $VENV_DIR/bin/python"
    emit_line "OK: venv python exists: $VENV_DIR/bin/python"
  else
    add_result fail venv_python "venv python missing: $VENV_DIR/bin/python"
    emit_line "FAIL: venv python missing: $VENV_DIR/bin/python"
    failed=1
  fi

  if id "$MCP_USER" >/dev/null 2>&1; then
    add_result ok user "user exists: $MCP_USER"
    emit_line "OK: user exists: $MCP_USER"
  else
    add_result fail user "user missing: $MCP_USER"
    emit_line "FAIL: user missing: $MCP_USER"
    failed=1
  fi

  for unit in mcp-bridge.service mcp-ai-remediator.service mcp-ai-collector.service mcp-ai-collector.timer; do
    if [[ -f "/etc/systemd/system/$unit" ]]; then
      add_result ok "unit_file_${unit}" "unit file present: /etc/systemd/system/$unit"
      emit_line "OK: unit file present: /etc/systemd/system/$unit"
    else
      add_result warn "unit_file_${unit}" "unit file missing: /etc/systemd/system/$unit"
      emit_line "WARN: unit file missing: /etc/systemd/system/$unit"
    fi
  done

  cfg_path="$MCP_HOME/.mcp-ai/config.json"
  cfg_exists=0
  if [[ -f "$cfg_path" ]]; then
    cfg_exists=1
  elif [[ $EUID -ne 0 ]] && command -v sudo >/dev/null 2>&1 && sudo test -f "$cfg_path" >/dev/null 2>&1; then
    cfg_exists=1
  fi

  if [[ $cfg_exists -eq 1 ]]; then
    if [[ -r "$cfg_path" ]] && python3 -m json.tool "$cfg_path" >/dev/null 2>&1; then
      add_result ok mcp_config_json "config JSON valid: $MCP_HOME/.mcp-ai/config.json"
      emit_line "OK: config JSON valid: $MCP_HOME/.mcp-ai/config.json"
    elif [[ $EUID -ne 0 ]] && command -v sudo >/dev/null 2>&1 && sudo python3 -m json.tool "$cfg_path" >/dev/null 2>&1; then
      add_result ok mcp_config_json "config JSON valid: $MCP_HOME/.mcp-ai/config.json"
      emit_line "OK: config JSON valid: $MCP_HOME/.mcp-ai/config.json"
    else
      add_result fail mcp_config_json "config JSON invalid: $MCP_HOME/.mcp-ai/config.json"
      emit_line "FAIL: config JSON invalid: $MCP_HOME/.mcp-ai/config.json"
      failed=1
    fi
  else
    add_result warn mcp_config_json "config JSON missing: $MCP_HOME/.mcp-ai/config.json"
    emit_line "WARN: config JSON missing: $MCP_HOME/.mcp-ai/config.json"
  fi

  if command -v systemctl >/dev/null 2>&1; then
    for svc in mcp-bridge.service mcp-ai-remediator.service; do
      state="$(systemctl is-active "$svc" 2>/dev/null || true)"
      if [[ "$state" == "active" ]]; then
        add_result ok "service_${svc}" "service active: $svc"
        emit_line "OK: service active: $svc"
      else
        add_result warn "service_${svc}" "service not active: $svc (state=$state)"
        emit_line "WARN: service not active: $svc (state=$state)"
      fi
    done
    timer_state="$(systemctl is-active mcp-ai-collector.timer 2>/dev/null || true)"
    if [[ "$timer_state" == "active" ]]; then
      add_result ok service_mcp_ai_collector_timer "timer active: mcp-ai-collector.timer"
      emit_line "OK: timer active: mcp-ai-collector.timer"
    else
      add_result warn service_mcp_ai_collector_timer "timer not active: mcp-ai-collector.timer (state=$timer_state)"
      emit_line "WARN: timer not active: mcp-ai-collector.timer (state=$timer_state)"
    fi
  fi

  if command -v HAL >/dev/null 2>&1; then
    add_result ok hal_path "HAL command is on PATH"
    emit_line "OK: HAL command is on PATH"
  else
    add_result warn hal_path "HAL command not on PATH"
    emit_line "WARN: HAL command not on PATH"
  fi

  if [[ $VERIFY_JSON -eq 1 ]]; then
    python3 - "$results_file" "$failed" "$ok_count" "$warn_count" "$fail_count" <<'PY'
import json, sys
path, failed, ok_count, warn_count, fail_count = sys.argv[1:]
results = []
with open(path, 'r', encoding='utf-8', errors='replace') as fh:
    for line in fh:
        line = line.rstrip('\n')
        if not line:
            continue
        parts = line.split('|', 2)
        if len(parts) != 3:
            continue
        results.append({'status': parts[0], 'check': parts[1], 'message': parts[2]})
out = {
    'ok': failed == '0',
    'summary': {
        'ok': int(ok_count),
        'warn': int(warn_count),
        'fail': int(fail_count),
        'total': int(ok_count) + int(warn_count) + int(fail_count),
    },
    'results': results,
}
print(json.dumps(out, separators=(',', ':')))
PY
  fi

  if [[ $failed -eq 0 ]]; then
    emit_line "Verification PASSED"
  else
    emit_line "Verification FAILED"
  fi
  rm -f "$results_file"
  return $failed
}

main(){
  if [[ $VERIFY_MODE -eq 1 && $APPLY -eq 0 ]]; then
    if [[ $VERIFY_JSON -ne 1 ]]; then
      echo "Running in verify-only mode"
    fi
    verify_install
    return $?
  fi

  echo "Starting install (apply=$APPLY start=$START_SERVICES verify=$VERIFY_MODE force=$FORCE)"
  install_prereqs
  create_user_and_dirs
  deploy_files
  create_venv_and_install
  write_systemd_units
  write_sudoers
  write_mcp_config
  post_install
  check_ollama
  if [[ $VERIFY_MODE -eq 1 ]]; then
    verify_install
  fi
  echo "Install script completed (dry-run=$((1-APPLY)))"
}

main
