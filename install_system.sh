#!/usr/bin/env bash
set -euo pipefail

# install_system.sh — unified installer for mcp-rhel-manager
# (merges former install_system.sh + architect_genesis.sh into a single entry point)
# Default: apply mode (real install). Use --dry-run to preview without changes.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="/opt/mcp-rhel-manager"
MCP_USER="mcp"
MCP_HOME="/var/lib/mcp"
AI_USER="mcp-ai"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
BRIDGE_PORT="${BRIDGE_PORT:-1776}"
VENV_DIR=""
INSTALL_USER="${SUDO_USER:-$(id -un)}"
INSTALL_HOME=""

APPLY=1
APPLY_EXPLICIT=0
START_SERVICES=0
YES=0
FORCE=0
VERIFY_MODE=0
VERIFY_JSON=0
ROLLBACK_ON_FAIL=1
VENV_MODE=0
SELINUX_POLICY="permissive"
FIREWALLD_POLICY="disabled"

usage(){
  cat <<EOF
Usage: $(basename "$0") [--apply|--dry-run] [--start] [--verify] [--verify-json] [--yes] [--force]
                          [--venv] [--base-dir PATH] [--user NAME] [--home PATH]
                          [--selinux {permissive|enforcing|unchanged}] [--firewalld {enabled|disabled|unchanged}]

Options:
  --apply       Run real install/apply mode (default)
  --dry-run,-n  Preview actions without making changes
  --start       Enable & start systemd services after install (requires apply mode)
  --venv        Keep Python venv inside the repo dir (default: under --base-dir)
  --yes         Don't prompt for confirmation when applying
  --force       Overwrite existing install targets
  --verify      Run post-install verification checks (can be used standalone)
  --verify-json Run compact JSON verification output (implies --verify)
  --base-dir    Install root (default: /opt/mcp-rhel-manager)
  --user        Service account username (default: mcp)
  --home        Service account home directory (default: /var/lib/mcp)
  --selinux     SELinux target mode (default: permissive)
  --firewalld   firewalld target state (default: disabled)
  --no-rollback Disable automatic rollback on failure in --apply mode

Unified installer — replaces architect_genesis.sh (now a compat shim).
Apply mode by default; use --dry-run to preview. Idempotent & distro-agnostic.

Common workflows:
  Full system install:       ./install_system.sh --start --yes
  Dry-run preview:           ./install_system.sh --dry-run
  In-repo venv (dev/user):   ./install_system.sh --venv --start
  Verify after install:      ./install_system.sh --verify
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply|-a) APPLY=1; APPLY_EXPLICIT=1; shift ;;
    --dry-run|-n) APPLY=0; APPLY_EXPLICIT=1; shift ;;
    --start|-s) START_SERVICES=1; shift ;;
    --verify) VERIFY_MODE=1; shift ;;
    --verify-json) VERIFY_MODE=1; VERIFY_JSON=1; shift ;;
    --yes|-y) YES=1; shift ;;
    --force|-f) FORCE=1; shift ;;
    --venv) VENV_MODE=1; shift ;;
    --base-dir) BASE_DIR="${2:-}"; shift 2 ;;
    --user) MCP_USER="${2:-}"; shift 2 ;;
    --home) MCP_HOME="${2:-}"; shift 2 ;;
    --selinux) SELINUX_POLICY="${2:-}"; shift 2 ;;
    --firewalld) FIREWALLD_POLICY="${2:-}"; shift 2 ;;
    --no-rollback) ROLLBACK_ON_FAIL=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# Keep `--verify` standalone by default; use `--verify --apply` for install+verify.
if [[ $VERIFY_MODE -eq 1 && $APPLY_EXPLICIT -eq 0 ]]; then
  APPLY=0
fi

fail(){
  echo "ERROR: $*" >&2
  exit 1
}

[[ -n "$BASE_DIR" ]] || fail "--base-dir requires a value"
[[ -n "$MCP_USER" ]] || fail "--user requires a value"
[[ -n "$MCP_HOME" ]] || fail "--home requires a value"
[[ "$SELINUX_POLICY" =~ ^(permissive|enforcing|unchanged)$ ]] || fail "--selinux must be one of: permissive, enforcing, unchanged"
[[ "$FIREWALLD_POLICY" =~ ^(enabled|disabled|unchanged)$ ]] || fail "--firewalld must be one of: enabled, disabled, unchanged"

if [[ $VENV_MODE -eq 1 ]]; then
  VENV_DIR="$SCRIPT_DIR/venv"
else
  VENV_DIR="$BASE_DIR/venv"
fi

if command -v getent >/dev/null 2>&1; then
  INSTALL_HOME="$(getent passwd "$INSTALL_USER" | awk -F: '{print $6}' || true)"
fi
[[ -z "$INSTALL_HOME" ]] && INSTALL_HOME="$(eval echo "~$INSTALL_USER" 2>/dev/null || true)"
[[ -n "$INSTALL_HOME" ]] || INSTALL_HOME="/home/$INSTALL_USER"

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
  [[ $WROTE_SUDOERS -eq 1 ]] && run rm -f /etc/sudoers.d/mcp-ai >/dev/null 2>&1 || true
  [[ $CREATED_VENV -eq 1 ]] && run rm -rf "$VENV_DIR" >/dev/null 2>&1 || true
  [[ $WROTE_MCP_CONFIG -eq 1 ]] && run rm -f "$MCP_HOME/.mcp-ai/config.json" >/dev/null 2>&1 || true
  [[ $CREATED_BASE_DIR -eq 1 ]] && run rm -rf "$BASE_DIR" >/dev/null 2>&1 || true
  [[ $CREATED_HOME_DIR -eq 1 ]] && run rm -rf "$MCP_HOME" >/dev/null 2>&1 || true
  [[ $CREATED_USER -eq 1 ]] && run userdel "$MCP_USER" >/dev/null 2>&1 || true
  echo "Rollback complete (best-effort)." >&2
  exit "$rc"
}

trap rollback_on_error ERR

run(){
  echo "+ $*"
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then sudo "$@"; else "$@"; fi
  fi
}

write_temp_and_move(){
  local dest="$1"
  local tmp
  tmp="$(mktemp)"
  cat >"$tmp"
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mv "$tmp" "$dest"; sudo chmod 644 "$dest"; sudo chown root:root "$dest"
    else
      mv "$tmp" "$dest"; chmod 644 "$dest"; chown root:root "$dest"
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
  case "$ans" in [Yy]* ) return 0 ;; * ) return 1 ;; esac
}

if [[ $VERIFY_JSON -ne 1 ]]; then
  echo "install_system.sh (unified installer) for mcp-rhel-manager"
  echo "Base dir: $BASE_DIR  Venv: $VENV_DIR  Mode: $([ $VENV_MODE -eq 1 ] && echo venv || echo system)"
  echo "MCP user: $MCP_USER (home: $MCP_HOME)  AI user: $AI_USER"
  echo "OLLAMA URL: $OLLAMA_URL  Bridge port: $BRIDGE_PORT"
  echo "SELinux policy target: $SELINUX_POLICY  firewalld target: $FIREWALLD_POLICY"
  echo
fi

[[ -d "$SCRIPT_DIR" ]] || fail "Script directory not found: $SCRIPT_DIR"
[[ "$BRIDGE_PORT" =~ ^[0-9]+$ ]] || fail "BRIDGE_PORT must be numeric (got: $BRIDGE_PORT)"
(( BRIDGE_PORT >= 1 && BRIDGE_PORT <= 65535 )) || fail "BRIDGE_PORT out of range: $BRIDGE_PORT"
[[ $START_SERVICES -eq 1 && $APPLY -ne 1 ]] && fail "--start requires --apply"
[[ $APPLY -eq 1 && $EUID -ne 0 ]] && { command -v sudo >/dev/null 2>&1 || fail "sudo is required when running --apply as non-root"; }
[[ $APPLY -ne 1 && $VERIFY_JSON -ne 1 ]] && echo "Running in dry-run mode. Use --apply to make changes."

if [[ $APPLY -eq 1 && $YES -ne 1 ]]; then
  echo "About to perform actions on this host:"
  echo " - create users: $MCP_USER, $AI_USER"
  echo " - create $BASE_DIR and copy files"
  echo " - create venv: $VENV_DIR and install python deps"
  echo " - place systemd units and sudoers snippets"
  echo " - install HAL CLI to /usr/local/bin"
  echo " - apply SELinux contexts and file permission hardening"
  echo
  if ! prompt_confirm "Proceed?"; then echo "Aborting."; exit 1; fi
fi

PKG_CMD=""
PKG_INSTALL_OPTS=""
if command -v dnf >/dev/null 2>&1; then
  PKG_CMD="dnf"; PKG_INSTALL_OPTS="-y"
elif command -v apt-get >/dev/null 2>&1; then
  PKG_CMD="apt-get"; PKG_INSTALL_OPTS="-y"
elif command -v yum >/dev/null 2>&1; then
  PKG_CMD="yum"; PKG_INSTALL_OPTS="-y"
fi

# ===========================================================================
install_prereqs(){
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would install OS prerequisite packages."
    return 0
  fi
  [[ -z "$PKG_CMD" ]] && { echo "No supported package manager found; install prerequisites manually." >&2; return 1; }
  if [[ $PKG_CMD == "dnf" || $PKG_CMD == "yum" ]]; then
    run "$PKG_CMD" install $PKG_INSTALL_OPTS \
      python3 python3-pip python3-devel gcc make \
      redhat-rpm-config libffi-devel openssl-devel \
      lm_sensors smartmontools pciutils \
      jq rsync git curl which ripgrep iproute dkms \
      audit audit-libs firewalld fail2ban || true
    run systemctl enable --now auditd 2>/dev/null || true
    run systemctl enable --now fail2ban 2>/dev/null || true
    case "$FIREWALLD_POLICY" in
      enabled) run systemctl enable --now firewalld 2>/dev/null || true ;;
      disabled) run systemctl disable --now firewalld 2>/dev/null || true ;;
      unchanged) : ;;
    esac
  else
    run apt-get update
    run apt-get install $PKG_INSTALL_OPTS \
      python3 python3-venv python3-pip python3-dev build-essential gcc make \
      libffi-dev libssl-dev \
      lm-sensors smartmontools pciutils \
      jq rsync git curl which ripgrep dkms \
      auditd ufw fail2ban || true
    case "$FIREWALLD_POLICY" in
      enabled) run systemctl enable --now ufw 2>/dev/null || true ;;
      disabled) run systemctl disable --now ufw 2>/dev/null || true ;;
      unchanged) : ;;
    esac
  fi
}

# ===========================================================================
apply_runtime_baseline(){
  echo "Applying host baseline policies (SELinux/firewalld)"
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would apply SELinux=$SELINUX_POLICY and firewalld=$FIREWALLD_POLICY"
    return 0
  fi

  if command -v getenforce >/dev/null 2>&1 && command -v setenforce >/dev/null 2>&1; then
    case "$SELINUX_POLICY" in
      permissive)
        if [[ "$(getenforce 2>/dev/null || true)" == "Enforcing" ]]; then
          run setenforce 0 2>/dev/null || true
        fi
        ;;
      enforcing)
        if [[ "$(getenforce 2>/dev/null || true)" == "Permissive" ]]; then
          run setenforce 1 2>/dev/null || true
        fi
        ;;
      unchanged)
        :
        ;;
    esac
  fi

  if command -v systemctl >/dev/null 2>&1; then
    case "$FIREWALLD_POLICY" in
      enabled)
        run systemctl enable --now firewalld 2>/dev/null || true
        ;;
      disabled)
        run systemctl disable --now firewalld 2>/dev/null || true
        ;;
      unchanged)
        :
        ;;
    esac
  fi
}

# ===========================================================================
create_user_and_dirs(){
  if id "$MCP_USER" >/dev/null 2>&1; then
    echo "User $MCP_USER already exists"
  else
    echo "Creating system user $MCP_USER"
    run useradd --system --no-create-home --home-dir "$MCP_HOME" --shell /sbin/nologin "$MCP_USER" \
      || run adduser --system --no-create-home --home-dir "$MCP_HOME" --shell /sbin/nologin "$MCP_USER"
    [[ $APPLY -eq 1 ]] && CREATED_USER=1
  fi
  [[ ! -d "$BASE_DIR" && $APPLY -eq 1 ]] && CREATED_BASE_DIR=1
  run mkdir -p "$BASE_DIR"
  [[ ! -d "$MCP_HOME" && $APPLY -eq 1 ]] && CREATED_HOME_DIR=1
  run mkdir -p "$MCP_HOME"
}

# ===========================================================================
deploy_files(){
  echo "Syncing files from $SCRIPT_DIR to $BASE_DIR"
  command -v rsync >/dev/null 2>&1 || fail "rsync is required for deploy_files"
  if [[ $FORCE -ne 1 ]]; then
    if [[ -d "$BASE_DIR" && -n "$(ls -A "$BASE_DIR" 2>/dev/null || true)" ]]; then
      if [[ ! -f "$BASE_DIR/install_system.sh" ]]; then
        fail "$BASE_DIR is non-empty and does not look like this project. Re-run with --force to override."
      fi
    fi
  fi
  local rsync_delete=""
  [[ $FORCE -eq 1 ]] && rsync_delete="--delete"
  run rsync -a $rsync_delete \
    --exclude ".git" --exclude "venv" --exclude ".venv" \
    --exclude "artifacts" --exclude "artifacts_user" \
    "$SCRIPT_DIR/" "$BASE_DIR/"
  [[ -f "$BASE_DIR/bin/HAL" ]] && run chmod +x "$BASE_DIR/bin/HAL"
  for f in \
    "$BASE_DIR/install_system.sh" \
    "$BASE_DIR/scripts/architect_genesis.sh" \
    "$BASE_DIR/scripts/auto-fixer.sh" \
    "$BASE_DIR/scripts/hal-auto-update.sh" \
    "$BASE_DIR/mcp-ai/start-bridge.sh" \
    "$BASE_DIR/mcp-ai/enable_services.sh" \
    "$BASE_DIR/mcp-ai/setup_auto_ingest.sh"; do
    [[ -f "$f" ]] && run chmod +x "$f"
  done
  run chown -R "$MCP_USER:$MCP_USER" "$BASE_DIR"
}

# ===========================================================================
create_venv_and_install(){
  echo "Creating venv at $VENV_DIR"
  [[ ! -d "$VENV_DIR" && $APPLY -eq 1 ]] && CREATED_VENV=1
  run python3 -m venv "$VENV_DIR"
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then sudo chmod +x "$VENV_DIR/bin"/*; else chmod +x "$VENV_DIR/bin"/*; fi
  fi
  run "$VENV_DIR/bin/pip" install -U pip setuptools wheel
  if [[ -f "$BASE_DIR/requirements.txt" ]]; then
    echo "Installing Python requirements"
    run "$VENV_DIR/bin/pip" install -r "$BASE_DIR/requirements.txt"
    run "$VENV_DIR/bin/pip" install --upgrade "filelock>=3.24.2" 2>/dev/null || true
    echo ""
    echo "NOTE: aider-chat is NOT in requirements.txt (filelock conflict). Install separately:"
    echo "  pip3 install --upgrade aider-chat virtualenv filelock transformers huggingface-hub tox tox-ansible ansible-dev-tools"
    echo "  (aider-chat pins filelock==3.20.3; the one-liner upgrades all affected packages together)"
  else
    echo "WARNING: requirements.txt not found at $BASE_DIR/requirements.txt"
  fi
  if [[ $APPLY -eq 1 ]]; then
    "$VENV_DIR/bin/pip" show ollama-mcp-bridge >/dev/null 2>&1 || run "$VENV_DIR/bin/pip" install ollama-mcp-bridge
  else
    echo "DRY-RUN: would ensure ollama-mcp-bridge is present"
  fi
}

# ===========================================================================
setup_ai_user(){
  echo "Setting up AI service user: $AI_USER"
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would create group/user '$AI_USER', configure sudoers, subuid/subgid"
    return 0
  fi
  getent group "$AI_USER" >/dev/null 2>&1 || run groupadd -r "$AI_USER" 2>/dev/null || true
  id -u "$AI_USER" >/dev/null 2>&1 || run useradd -m -g "$AI_USER" -s /bin/bash "$AI_USER" 2>/dev/null || true
  id -nG "$INSTALL_USER" 2>/dev/null | grep -qw "$AI_USER" || {
    run usermod -aG "$AI_USER" "$INSTALL_USER" || true
    echo "Added $INSTALL_USER to group $AI_USER (re-login to activate)"
  }
  local runner_src="$BASE_DIR/mcp-ai/runner.py"
  [[ -f "$runner_src" ]] && run install -m 755 "$runner_src" /usr/local/bin/mcp-ai-runner || true
  local tmp_s
  tmp_s="$(mktemp)"
  cat >"$tmp_s" <<SUDOEOF
# mcp-ai: allow AI group to run the runner wrapper as root
%${AI_USER} ALL=(ALL) NOPASSWD: /usr/local/bin/mcp-ai-runner
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/local/bin/mcp-ai-runner
SUDOEOF
  if [[ $EUID -ne 0 ]]; then
    sudo install -m 440 -o root -g root "$tmp_s" /etc/sudoers.d/mcp-ai-runner || true
  else
    install -m 440 -o root -g root "$tmp_s" /etc/sudoers.d/mcp-ai-runner || true
  fi
  rm -f "$tmp_s"
  grep -q "^${AI_USER}:" /etc/subuid 2>/dev/null || {
    if [[ $EUID -ne 0 ]]; then echo "${AI_USER}:100000:65536" | sudo tee -a /etc/subuid >/dev/null || true
    else echo "${AI_USER}:100000:65536" >>/etc/subuid || true; fi
  }
  grep -q "^${AI_USER}:" /etc/subgid 2>/dev/null || {
    if [[ $EUID -ne 0 ]]; then echo "${AI_USER}:100000:65536" | sudo tee -a /etc/subgid >/dev/null || true
    else echo "${AI_USER}:100000:65536" >>/etc/subgid || true; fi
  }
  run usermod -d "$MCP_HOME" "$AI_USER" 2>/dev/null || true
  run chown -R "$AI_USER:$AI_USER" "$MCP_HOME" || true
  if ! command -v podman >/dev/null 2>&1 && [[ -n "$PKG_CMD" ]]; then
    run "$PKG_CMD" install $PKG_INSTALL_OPTS podman 2>/dev/null || true
  fi
  echo "AI user ($AI_USER) setup complete."
}
_install_unit(){
  # Helper: install a temp file as a systemd unit. src dest
  local src="$1" dest="$2"
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then sudo install -m 644 -o root -g root "$src" "$dest"
    else install -m 644 -o root -g root "$src" "$dest"; fi
    rm -f "$src"
    command -v restorecon >/dev/null 2>&1 && restorecon -v "$dest" >/dev/null 2>&1 || true
    echo "$dest written"
  else
    echo "DRY-RUN: would write $dest"; rm -f "$src"
  fi
}

write_systemd_units(){
  echo "Preparing systemd unit files"
  local t
  t="$(mktemp)"
  cat >"$t" <<UNITEOF
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
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-bridge.service && WROTE_UNIT_BRIDGE=1 || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=MCP AI Remediator (on-demand)
After=network.target
[Service]
Type=oneshot
User=$AI_USER
Group=$AI_USER
WorkingDirectory=$BASE_DIR
Environment=HOME=$MCP_HOME
ExecStart=$VENV_DIR/bin/python $BASE_DIR/mcp-ai/remediate.py --latest
TimeoutStartSec=600
[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-remediator.service && WROTE_UNIT_REMEDIATOR=1 || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=MCP AI Collector (system)
After=network.target
[Service]
Type=simple
User=$AI_USER
Group=$AI_USER
Environment=HOME=$MCP_HOME
ExecStart=$VENV_DIR/bin/python $BASE_DIR/mcp-ai/collector.py --run
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-collector.service && WROTE_UNIT_COLLECTOR=1 || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=Run MCP AI Collector every 30 minutes
[Timer]
OnCalendar=*:0/30
Persistent=true
[Install]
WantedBy=timers.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-collector.timer && WROTE_UNIT_TIMER=1 || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=MCP AI Dashboard (Flask)
After=network.target
[Service]
Type=simple
User=$AI_USER
Group=$AI_USER
Environment=HOME=$MCP_HOME
WorkingDirectory=$BASE_DIR/mcp-ai
ExecStart=$VENV_DIR/bin/python $BASE_DIR/mcp-ai/dashboard.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-dashboard.service || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=MCP AI HAL Brain Background Worker
After=network.target

[Service]
Type=simple
User=$AI_USER
Group=$AI_USER
Environment=HOME=$MCP_HOME
WorkingDirectory=$BASE_DIR
ExecStart=/bin/bash -lc 'while true; do $VENV_DIR/bin/python $BASE_DIR/scripts/hal-brain.py --brain-status --mcp-server-status >> $MCP_HOME/hal-brain.log 2>&1 || true; sleep 60; done'
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-hal-brain.service || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=MCP AI System Indexer
After=network.target
[Service]
Type=oneshot
User=$AI_USER
Group=$AI_USER
Environment=HOME=$MCP_HOME
ExecStart=$VENV_DIR/bin/python $MCP_HOME/indexer.py --outdir $MCP_HOME/training
TimeoutStartSec=600
[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-indexer.service || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=Run MCP AI System Indexer daily
[Timer]
OnBootSec=10min
OnUnitActiveSec=1d
Persistent=true
[Install]
WantedBy=timers.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-indexer.timer || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=Watch $MCP_HOME/fixes for new fix plans
[Path]
PathExistsGlob=$MCP_HOME/fixes/*.json
Unit=mcp-ai-remediator.service
[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-remediator.path || true
}

# ===========================================================================
write_sudoers(){
  echo "Writing /etc/sudoers.d/mcp-ai"
  local sudo_file="/etc/sudoers.d/mcp-ai"
  local tmp
  tmp="$(mktemp)"
  cat >"$tmp" <<SUDOEOF
# Allow mcp user to run remediation scripts as root without password
Cmnd_Alias MCPAI_CMDS = $BASE_DIR/mcp-ai/runner.py, $BASE_DIR/mcp-ai/remediate.py
$MCP_USER ALL=(root) NOPASSWD: MCPAI_CMDS
SUDOEOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then sudo install -m 440 -o root -g root "$tmp" "$sudo_file"
    else install -m 440 -o root -g root "$tmp" "$sudo_file"; fi
    rm -f "$tmp"
    command -v restorecon >/dev/null 2>&1 && restorecon -v "$sudo_file" >/dev/null 2>&1 || true
    if [[ $EUID -eq 0 ]]; then visudo -cf "$sudo_file" >/dev/null 2>&1 || { echo "ERROR: sudoers syntax invalid"; return 1; }
    else sudo visudo -cf "$sudo_file" >/dev/null 2>&1 || { echo "ERROR: sudoers syntax invalid"; return 1; }; fi
    echo "$sudo_file written and validated"
    WROTE_SUDOERS=1
  else
    echo "DRY-RUN: would write $sudo_file"; sed -n '1,200p' "$tmp" || true; rm -f "$tmp"
  fi
}

# ===========================================================================
write_mcp_config(){
  local cfg_dir="$MCP_HOME/.mcp-ai"
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then sudo mkdir -p "$cfg_dir"; sudo chown -R "$MCP_USER:$MCP_USER" "$cfg_dir"
    else mkdir -p "$cfg_dir"; chown -R "$MCP_USER:$MCP_USER" "$cfg_dir"; fi
  else
    echo "DRY-RUN: would create $cfg_dir"
  fi
  local cfg_file="$cfg_dir/config.json"
  local tmp
  tmp="$(mktemp)"
  cat >"$tmp" <<JSONEOF
{
  "ollama_url": "$OLLAMA_URL",
  "bridge_port": $BRIDGE_PORT,
  "allow_auto_fix": false,
  "conversational": true
}
JSONEOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
      sudo mv "$tmp" "$cfg_file"; sudo chown "$MCP_USER:$MCP_USER" "$cfg_file"; sudo chmod 600 "$cfg_file"
    else
      mv "$tmp" "$cfg_file"; chown "$MCP_USER:$MCP_USER" "$cfg_file"; chmod 600 "$cfg_file"
    fi
    echo "Wrote $cfg_file"
    WROTE_MCP_CONFIG=1
  else
    echo "DRY-RUN: would write $cfg_file"; sed -n '1,200p' "$tmp" || true; rm -f "$tmp"
  fi
}

# ===========================================================================
write_genesis_config(){
  echo "Writing mcp-config.json, auto-fixer.sh, and gold seed"
  local mcp_py="$VENV_DIR/bin/python"
  local tmp_cfg
  tmp_cfg="$(mktemp)"
  cat >"$tmp_cfg" <<JSONEOF
{
  "mcpServers": {
    "architect": {
      "command": "$mcp_py",
      "args": ["$BASE_DIR/scripts/server.py"]
    }
  }
}
JSONEOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then sudo install -m 644 -o root -g root "$tmp_cfg" "$BASE_DIR/mcp-config.json"
    else install -m 644 -o root -g root "$tmp_cfg" "$BASE_DIR/mcp-config.json"; fi
    rm -f "$tmp_cfg"; echo "Wrote $BASE_DIR/mcp-config.json"
  else
    echo "DRY-RUN: would write $BASE_DIR/mcp-config.json"; rm -f "$tmp_cfg"
  fi

  local tmp_af
  tmp_af="$(mktemp)"
  cat >"$tmp_af" <<'AFEOF'
#!/bin/bash
# auto-fixer.sh — Architect Sentinel maintenance loop (generated by install_system.sh)
set -euo pipefail
while true; do
  curl -s -X POST http://localhost:1776/api/chat \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen2.5-coder:7b","messages":[
      {"role":"system","content":"You are the Architect Sentinel. 1. Run predict_failure_and_evacuate. 2. Run optimize_ai_performance. 3. Run sentinel_scan. 4. If all good, respond SYSTEM_EVOLVING."},
      {"role":"user","content":"Execute maintenance."}
    ]}' >> "$(dirname "$0")/evolution.log"
  sleep 3600
done
AFEOF
  if [[ $APPLY -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then sudo install -m 755 -o root -g root "$tmp_af" "$BASE_DIR/scripts/auto-fixer.sh"
    else install -m 755 -o root -g root "$tmp_af" "$BASE_DIR/scripts/auto-fixer.sh"; fi
    rm -f "$tmp_af"; echo "Wrote $BASE_DIR/scripts/auto-fixer.sh"
  else
    echo "DRY-RUN: would write $BASE_DIR/scripts/auto-fixer.sh"; rm -f "$tmp_af"
  fi

  local seed_dir="$INSTALL_HOME/.local/share/mcp-seed"
  if [[ $APPLY -eq 1 ]]; then
    mkdir -p "$seed_dir"
    if [[ -f "$BASE_DIR/scripts/server.py" ]]; then
      cp -f "$BASE_DIR/scripts/server.py" "$seed_dir/server.py.gold" 2>/dev/null || true
      chattr +i "$seed_dir/server.py.gold" 2>/dev/null || true
      echo "Gold seed locked: $seed_dir/server.py.gold"
    fi
  else
    echo "DRY-RUN: would create immutable gold seed at $seed_dir/server.py.gold"
  fi
}

# ===========================================================================
setup_user_services(){
  local user_sd="$INSTALL_HOME/.config/systemd/user"
  echo "Writing per-user systemd services to $user_sd"
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would write mcp-bridge/sentinel/collector user services"
    return 0
  fi
  mkdir -p "$user_sd"
  cat >"$user_sd/mcp-bridge.service" <<UNITEOF
[Unit]
Description=MCP Ollama Bridge (user)
After=default.target
[Service]
ExecStart=$VENV_DIR/bin/python -m ollama_mcp_bridge.main --config $BASE_DIR/mcp-config.json --port $BRIDGE_PORT
Restart=always
KillMode=process
[Install]
WantedBy=default.target
UNITEOF
  cat >"$user_sd/mcp-sentinel.service" <<UNITEOF
[Unit]
Description=MCP Architect Sentinel (user)
After=mcp-bridge.service
[Service]
ExecStart=$BASE_DIR/scripts/auto-fixer.sh
Restart=always
[Install]
WantedBy=default.target
UNITEOF
  cat >"$user_sd/mcp-ai-collector.service" <<UNITEOF
[Unit]
Description=MCP AI Collector (user)
After=default.target
[Service]
Type=simple
ExecStart=$VENV_DIR/bin/python $BASE_DIR/mcp-ai/collector.py --run
Restart=on-failure
KillMode=process
[Install]
WantedBy=default.target
UNITEOF
  cat >"$user_sd/mcp-ai-collector.timer" <<UNITEOF
[Unit]
Description=Periodic MCP AI Collector (user)
[Timer]
OnBootSec=2min
OnUnitActiveSec=1h
Persistent=true
[Install]
WantedBy=timers.target
UNITEOF
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable --now mcp-ai-collector.timer 2>/dev/null || true
  if [[ $START_SERVICES -eq 1 ]]; then
    systemctl --user enable --now mcp-bridge.service mcp-sentinel.service 2>/dev/null || true
    loginctl enable-linger "$INSTALL_USER" 2>/dev/null || true
  fi
  echo "Per-user systemd services written."
}

# ===========================================================================
post_install(){
  echo "Running post-install steps"
  if [[ $APPLY -eq 1 ]]; then
    run systemctl daemon-reload
    if [[ $START_SERVICES -eq 1 ]]; then
      run systemctl enable \
        mcp-ai-remediator.service \
        mcp-ai-remediator.path || true
      run systemctl enable --now \
        mcp-bridge.service \
        mcp-ai-collector.timer \
        mcp-ai-dashboard.service \
        mcp-ai-hal-brain.service || true
    fi
  else
    echo "DRY-RUN: would run systemctl daemon-reload and optionally enable/start services"
  fi
}

# ===========================================================================
install_hal_cli(){
  echo "Installing HAL CLI to /usr/local/bin"
  local wrapper_src=""
  [[ -f "$BASE_DIR/bin/HAL" ]] && wrapper_src="$BASE_DIR/bin/HAL"
  [[ -z "$wrapper_src" && -f "$BASE_DIR/HAL" ]] && wrapper_src="$BASE_DIR/HAL"
  if [[ -z "$wrapper_src" ]]; then
    echo "WARN: HAL wrapper not found in $BASE_DIR/bin/HAL; skipping"; return 0
  fi
  if [[ $APPLY -eq 1 ]]; then
    run chmod +x "$wrapper_src"
    run ln -sf "$wrapper_src" /usr/local/bin/HAL
    run ln -sf "$wrapper_src" /usr/local/bin/hal
    echo "HAL CLI: /usr/local/bin/HAL -> $wrapper_src"
  else
    echo "DRY-RUN: would link $wrapper_src to /usr/local/bin/{HAL,hal}"
  fi
}

# ===========================================================================
apply_selinux_hardening(){
  echo "Applying file permission hardening and SELinux contexts"
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would set permissions under $BASE_DIR and $MCP_HOME, apply SELinux contexts"
    return 0
  fi
  run chown -R root:root "$BASE_DIR" 2>/dev/null || true
  run find "$BASE_DIR" -type d -exec chmod 0755 {} + 2>/dev/null || true
  run find "$BASE_DIR" -type f -exec chmod 0644 {} + 2>/dev/null || true
  for f in \
    "$BASE_DIR/install_system.sh" \
    "$BASE_DIR/scripts/architect_genesis.sh" \
    "$BASE_DIR/scripts/auto-fixer.sh" \
    "$BASE_DIR/mcp-ai/start-bridge.sh" \
    "$BASE_DIR/mcp-ai/enable_services.sh" \
    "$BASE_DIR/mcp-ai/setup_auto_ingest.sh"; do
    [[ -f "$f" ]] && run chmod 0755 "$f" || true
  done
  run find "$BASE_DIR/mcp-ai" -name "*.py" -exec chmod 0755 {} + 2>/dev/null || true
  run find "$BASE_DIR/scripts" -name "*.sh" -exec chmod 0755 {} + 2>/dev/null || true
  run find "$BASE_DIR/scripts" -name "*.py" -exec chmod 0755 {} + 2>/dev/null || true
  run chown -R "$AI_USER:$AI_USER" "$MCP_HOME" 2>/dev/null || true
  run find "$MCP_HOME" -type d -exec chmod 0750 {} + 2>/dev/null || true
  run find "$MCP_HOME" -type f -exec chmod 0640 {} + 2>/dev/null || true
  if command -v semanage >/dev/null 2>&1; then
    run semanage fcontext -a -t var_lib_t "/var/lib/mcp(/.*)?" >/dev/null 2>&1 || true
    run restorecon -Rv "$MCP_HOME" 2>/dev/null || true
  elif [[ -n "$PKG_CMD" ]]; then
    echo "semanage not found; attempting to install policycoreutils-python-utils..."
    run "$PKG_CMD" install $PKG_INSTALL_OPTS policycoreutils-python-utils 2>/dev/null || true
    command -v semanage >/dev/null 2>&1 && {
      run semanage fcontext -a -t var_lib_t "/var/lib/mcp(/.*)?" >/dev/null 2>&1 || true
      run restorecon -Rv "$MCP_HOME" 2>/dev/null || true
    }
  fi
  echo "Hardening applied."
}

# ===========================================================================
check_ollama(){
  echo "Checking Ollama/LLM endpoint: $OLLAMA_URL"
  if command -v curl >/dev/null 2>&1; then
    if curl -sfS "$OLLAMA_URL" >/dev/null 2>&1; then
      echo "OLLAMA endpoint reachable"
    else
      echo "WARNING: OLLAMA endpoint at $OLLAMA_URL not reachable. Ensure Ollama is running."
    fi
  else
    echo "curl not installed; cannot test Ollama reachability"
  fi
}

# ===========================================================================
verify_install(){
  local failed=0 ok_count=0 warn_count=0 fail_count=0
  local results_file
  results_file="$(mktemp)"

  emit_line(){ [[ $VERIFY_JSON -ne 1 ]] && echo "$*"; }
  add_result(){
    local st="$1"; shift; local key="$1"; shift; local msg="$*"
    msg="${msg//|//}"
    echo "${st}|${key}|${msg}" >>"$results_file"
    case "$st" in ok) ok_count=$((ok_count+1)) ;; warn) warn_count=$((warn_count+1)) ;; fail) fail_count=$((fail_count+1)) ;; esac
  }

  emit_line "Running verification checks..."

  if [[ -d "$BASE_DIR" ]]; then add_result ok base_dir "base dir exists: $BASE_DIR"; emit_line "OK: base dir exists: $BASE_DIR"
  else add_result fail base_dir "base dir missing: $BASE_DIR"; emit_line "FAIL: base dir missing: $BASE_DIR"; failed=1; fi

  if [[ -x "$VENV_DIR/bin/python" ]]; then add_result ok venv_python "venv python exists: $VENV_DIR/bin/python"; emit_line "OK: venv python exists: $VENV_DIR/bin/python"
  else add_result fail venv_python "venv python missing: $VENV_DIR/bin/python"; emit_line "FAIL: venv python missing: $VENV_DIR/bin/python"; failed=1; fi

  if id "$MCP_USER" >/dev/null 2>&1; then add_result ok user "user exists: $MCP_USER"; emit_line "OK: user exists: $MCP_USER"
  else add_result fail user "user missing: $MCP_USER"; emit_line "FAIL: user missing: $MCP_USER"; failed=1; fi

  if id "$AI_USER" >/dev/null 2>&1; then add_result ok ai_user "AI user exists: $AI_USER"; emit_line "OK: AI user exists: $AI_USER"
  else add_result warn ai_user "AI user missing: $AI_USER"; emit_line "WARN: AI user missing: $AI_USER"; fi

  for unit in mcp-bridge.service mcp-ai-remediator.service mcp-ai-collector.service mcp-ai-collector.timer mcp-ai-dashboard.service; do
    if [[ -f "/etc/systemd/system/$unit" ]]; then add_result ok "unit_${unit}" "unit file present: /etc/systemd/system/$unit"; emit_line "OK: unit file present: /etc/systemd/system/$unit"
    else add_result warn "unit_${unit}" "unit file missing: /etc/systemd/system/$unit"; emit_line "WARN: unit file missing: /etc/systemd/system/$unit"; fi
  done

  local cfg_path="$MCP_HOME/.mcp-ai/config.json" cfg_exists=0
  [[ -f "$cfg_path" ]] && cfg_exists=1
  [[ $cfg_exists -eq 0 && $EUID -ne 0 ]] && command -v sudo >/dev/null 2>&1 && sudo test -f "$cfg_path" 2>/dev/null && cfg_exists=1
  if [[ $cfg_exists -eq 1 ]]; then
    if python3 -m json.tool "$cfg_path" >/dev/null 2>&1 || sudo python3 -m json.tool "$cfg_path" >/dev/null 2>&1; then
      add_result ok mcp_config_json "config JSON valid: $cfg_path"; emit_line "OK: config JSON valid: $cfg_path"
    else add_result fail mcp_config_json "config JSON invalid: $cfg_path"; emit_line "FAIL: config JSON invalid: $cfg_path"; failed=1; fi
  else add_result warn mcp_config_json "config JSON missing: $cfg_path"; emit_line "WARN: config JSON missing: $cfg_path"; fi

  if command -v systemctl >/dev/null 2>&1; then
    for svc in mcp-bridge.service mcp-ai-remediator.service; do
      local state; state="$(systemctl is-active "$svc" 2>/dev/null || true)"
      if [[ "$state" == "active" ]]; then add_result ok "svc_${svc}" "service active: $svc"; emit_line "OK: service active: $svc"
      else add_result warn "svc_${svc}" "service not active: $svc (state=$state)"; emit_line "WARN: service not active: $svc (state=$state)"; fi
    done
    local ts; ts="$(systemctl is-active mcp-ai-collector.timer 2>/dev/null || true)"
    if [[ "$ts" == "active" ]]; then add_result ok svc_collector_timer "timer active: mcp-ai-collector.timer"; emit_line "OK: timer active: mcp-ai-collector.timer"
    else add_result warn svc_collector_timer "timer not active: mcp-ai-collector.timer (state=$ts)"; emit_line "WARN: timer not active: mcp-ai-collector.timer (state=$ts)"; fi
  fi

  if command -v HAL >/dev/null 2>&1; then add_result ok hal_path "HAL command is on PATH"; emit_line "OK: HAL command is on PATH"
  else add_result warn hal_path "HAL command not on PATH"; emit_line "WARN: HAL command not on PATH"; fi

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
    'summary': {'ok': int(ok_count), 'warn': int(warn_count), 'fail': int(fail_count),
                'total': int(ok_count)+int(warn_count)+int(fail_count)},
    'results': results,
}
print(json.dumps(out, separators=(',', ':')))
PY
  fi

  if [[ $failed -eq 0 ]]; then emit_line "Verification PASSED"
  else emit_line "Verification FAILED"; fi
  rm -f "$results_file"
  return $failed
}

# ===========================================================================
main(){
  if [[ $VERIFY_MODE -eq 1 && $APPLY -eq 0 ]]; then
    [[ $VERIFY_JSON -ne 1 ]] && echo "Running in verify-only mode"
    verify_install; return $?
  fi
  echo "Starting install (apply=$APPLY start=$START_SERVICES venv=$VENV_MODE verify=$VERIFY_MODE force=$FORCE)"
  install_prereqs
  create_user_and_dirs
  deploy_files
  create_venv_and_install
  setup_ai_user
  write_systemd_units
  write_sudoers
  write_mcp_config
  write_genesis_config
  setup_user_services
  post_install
  apply_runtime_baseline
  install_hal_cli
  apply_selinux_hardening
  check_ollama
  [[ $VERIFY_MODE -eq 1 ]] && verify_install
  echo ""
  echo "============================================================"
  echo "Install complete (dry-run=$((1-APPLY)))"
  echo "  Base dir : $BASE_DIR"
  echo "  Venv     : $VENV_DIR"
  echo "  MCP user : $MCP_USER   AI user: $AI_USER"
  echo "  HAL CLI  : /usr/local/bin/HAL"
  echo "  Bridge   : http://localhost:$BRIDGE_PORT"
  echo "  Ollama   : $OLLAMA_URL"
  echo "============================================================"
}

main
