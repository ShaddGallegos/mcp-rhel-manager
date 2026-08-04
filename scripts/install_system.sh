#!/usr/bin/env bash
set -euo pipefail

# install_system.sh — unified installer for mcp-rhel-manager
# (merges former install_system.sh + architect_genesis.sh into a single entry point)
ensure_ollama_and_models(){
  echo "Ensuring Ollama runtime and required models"
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would verify/install ollama and pull models"
    return 0
  fi

  # Detect ollama binary
  if ! command -v ollama >/dev/null 2>&1; then
    echo "ollama binary not found"
    if [[ -z "$PKG_CMD" ]]; then
      echo "No package manager detected; cannot auto-install ollama. Please install ollama manually." >&2
    else
      echo "Attempting to install ollama via package manager ($PKG_CMD)"
      if [[ "$PKG_CMD" == "dnf" || "$PKG_CMD" == "yum" ]]; then
        run "$PKG_CMD" install -y ollama || echo "Failed to install ollama via $PKG_CMD"
      else
        run apt-get update || true
        run apt-get install -y ollama || echo "Failed to install ollama via apt-get"
      fi
    fi
  else
    echo "ollama binary present"
  fi

  # After installation attempt, ensure ollama is available
  if ! command -v ollama >/dev/null 2>&1; then
    echo "ollama not available after install attempt; skipping model pulls" >&2
    return 1
  fi

  # Default model list can be overridden via OLLAMA_MODELS env var (space-separated)
  local models
  if [[ -n "${OLLAMA_MODELS:-}" ]]; then
    IFS=' ' read -r -a models <<< "$OLLAMA_MODELS"
  else
    models=("qwen2.5-coder:7b")
  fi

  # Query local Ollama tags to see what's present
  local tags_json
  if ! tags_json=$(curl -sfS "${OLLAMA_URL%/}/api/tags" 2>/dev/null || true); then
    echo "Warning: could not query Ollama tags at $OLLAMA_URL; ensure Ollama is running before pulling models"
  fi

  for m in "${models[@]}"; do
    if [[ -n "$tags_json" && $(printf '%s' "$tags_json" | grep -F -w "$m" >/dev/null 2>&1; echo $?) -eq 0 ]]; then
      echo "Model $m already available in Ollama"
      continue
    fi
    echo "Model $m not found locally"
    # Prepare pull log directory
    local log_dir="/var/log/mcp"
    local pull_log="$log_dir/ollama-pull.log"
    if [[ ! -d "$log_dir" ]]; then
      run mkdir -p "$log_dir" || true
      run chown "$INSTALL_USER:$INSTALL_USER" "$log_dir" 2>/dev/null || true
    fi

    if [[ $YES -ne 1 ]]; then
      if ! prompt_confirm "Pull model $m via 'ollama pull $m'? (may be large)"; then
        echo "Skipping model $m; remediator may fail until model is available"
        continue
      fi
    else
      echo "--yes provided: proceeding non-interactively. Note: model downloads can be large (hundreds of MB to many GB)."
    fi

    echo "Pulling model $m (logs -> $pull_log)"
    # Run the pull as the install user and capture output to pull log
    if ! run_as_install_user bash -lc "ollama pull \"$m\" 2>&1 | tee -a \"$pull_log\""; then
      echo "ollama pull $m failed; see $pull_log for details" >&2
      continue
    fi

    # wait for model to appear in tags (timeout)
    local tries=0
    local max=60
    until curl -sfS "${OLLAMA_URL%/}/api/tags" 2>/dev/null | grep -F -w "$m" >/dev/null 2>&1; do
      tries=$((tries+1))
      if [[ $tries -ge $max ]]; then
        echo "Timed out waiting for model $m to register in Ollama" >&2
        break
      fi
      sleep 2
    done
    echo "Model $m pull attempt complete"
  done
}

# Default: apply mode (real install). Use --dry-run to preview without changes.

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(basename "$SELF_DIR")" == "scripts" ]]; then
  SCRIPT_DIR="$(dirname "$SELF_DIR")"
else
  SCRIPT_DIR="$SELF_DIR"
fi
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
BACKGROUND_ONLY=1
YES=0
FORCE=0
DEV_PACKAGES=0
AI_WORKSTATION=0
VERIFY_MODE=0
VERIFY_JSON=0
ROLLBACK_ON_FAIL=1
VENV_MODE=0
SELINUX_POLICY="permissive"
FIREWALLD_POLICY="disabled"
RECONFIGURE=0
UNINSTALL=0
PRESERVE_DATA=0
REALLY_FORCE=0
PRECOMPUTE_EMBEDS=0
ASSIMILATE_FIXES=0
AI_PKGS=""
DEV_PKGS=""

usage(){
  cat <<EOF
Usage: $(basename "$0") [--apply|--dry-run] [--start] [--background-only|--eager-start] [--verify] [--verify-json] [--yes] [--force]
                          [--venv] [--base-dir PATH] [--user NAME] [--home PATH]
                          [--selinux {permissive|enforcing|unchanged}] [--firewalld {enabled|disabled|unchanged}]

Options:
  --apply       Run real install/apply mode (default)
  --dry-run,-n  Preview actions without making changes
  --start       Enable & start systemd services after install (requires apply mode)
  --background-only Enable MCP units without starting them immediately; suppress foreground MCP actions (default)
  --eager-start Disable background-only mode and allow immediate MCP service/timer starts (use with --start)
  --venv        Keep Python venv inside the repo dir (default: under --base-dir)
  --yes         Don't prompt for confirmation when applying
  --yes         Also: when present, installer will auto-pull models listed in the OLLAMA_MODELS env var (or default model) non-interactively.
  --force       Overwrite existing install targets
  --verify      Run post-install verification checks (can be used standalone)
  --verify-json Run compact JSON verification output (implies --verify)
  --base-dir    Install root (default: /opt/mcp-rhel-manager)
  --user        Service account username (default: mcp)
  --home        Service account home directory (default: /var/lib/mcp)
  --selinux     SELinux target mode (default: permissive)
  --firewalld   firewalld target state (default: disabled)
  --no-rollback Disable automatic rollback on failure in --apply mode
  --dev-packages Install developer packages (ansible-core, ansible-lint, yamllint, git)
  --ai-workstation Install AI workstation packages (podman, GPU tooling, python libs)
  --precompute-embeds  Install embedding packages and precompute FAISS/embeds (optional)

Unified installer — replaces architect_genesis.sh (now a compat shim).
Apply mode by default; use --dry-run to preview. Idempotent & distro-agnostic.

Common workflows:
  Full system install:       ./scripts/install_system.sh --start --background-only --yes
  Eager immediate start:     ./scripts/install_system.sh --start --eager-start --yes
  Dry-run preview:           ./scripts/install_system.sh --dry-run
  In-repo venv (dev/user):   ./scripts/install_system.sh --venv --start
  Verify after install:      ./scripts/install_system.sh --verify
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply|-a) APPLY=1; APPLY_EXPLICIT=1; shift ;;
        --dry-run|-n) APPLY=0; APPLY_EXPLICIT=1; shift ;;
        --start|-s) START_SERVICES=1; shift ;;
        --background-only) BACKGROUND_ONLY=1; shift ;;
        --eager-start) BACKGROUND_ONLY=0; shift ;;
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
        --reconfigure) RECONFIGURE=1; shift ;;
        --precompute-embeds) PRECOMPUTE_EMBEDS=1; shift ;;
        --dev-packages) DEV_PACKAGES=1; shift ;;
        --ai-workstation) AI_WORKSTATION=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --preserve-data) PRESERVE_DATA=1; shift ;;
        --really-force) REALLY_FORCE=1; shift ;;
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
WROTE_UNIT_HEALTH=0
WROTE_UNIT_WATCH=0
WROTE_UNIT_AUTO_IMPROVE=0

rollback_on_error(){
    local rc=$?
    if [[ $ROLLBACK_ON_FAIL -ne 1 || $APPLY -ne 1 ]]; then
        exit "$rc"
    fi
    echo "ERROR: install failed (rc=$rc); starting best-effort rollback..." >&2
    if [[ $WROTE_UNIT_BRIDGE -eq 1 || $WROTE_UNIT_REMEDIATOR -eq 1 || $WROTE_UNIT_COLLECTOR -eq 1 || $WROTE_UNIT_TIMER -eq 1 || $WROTE_UNIT_WATCH -eq 1 ]]; then
      run systemctl disable --now mcp-bridge.service mcp-ai-remediator.service mcp-ai-collector.timer hal-watch.service >/dev/null 2>&1 || true
      [[ $WROTE_UNIT_BRIDGE -eq 1 ]] && run rm -f /etc/systemd/system/mcp-bridge.service >/dev/null 2>&1 || true
      [[ $WROTE_UNIT_REMEDIATOR -eq 1 ]] && run rm -f /etc/systemd/system/mcp-ai-remediator.service >/dev/null 2>&1 || true
      [[ $WROTE_UNIT_COLLECTOR -eq 1 ]] && run rm -f /etc/systemd/system/mcp-ai-collector.service >/dev/null 2>&1 || true
      [[ $WROTE_UNIT_TIMER -eq 1 ]] && run rm -f /etc/systemd/system/mcp-ai-collector.timer >/dev/null 2>&1 || true
      [[ $WROTE_UNIT_HEALTH -eq 1 ]] && run rm -f /etc/systemd/system/mcp-ai-llm-health.service >/dev/null 2>&1 || true
      [[ $WROTE_UNIT_WATCH -eq 1 ]] && run rm -f /etc/systemd/system/hal-watch.service >/dev/null 2>&1 || true
      [[ $WROTE_UNIT_AUTO_IMPROVE -eq 1 ]] && run rm -f /etc/systemd/system/hal-auto-improve.service >/dev/null 2>&1 || true
      [[ $WROTE_UNIT_AUTO_IMPROVE -eq 1 ]] && run rm -f /etc/systemd/system/hal-auto-improve.timer >/dev/null 2>&1 || true
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

# If user requested reconfiguration, run the interactive configure helper and exit.
if [[ $RECONFIGURE -eq 1 ]]; then
if command -v python3 >/dev/null 2>&1; then
python3 "$SCRIPT_DIR/scripts/configure_ansible_env.py" || {
    echo "Reconfiguration failed." >&2; exit 1
}
echo "Reconfiguration complete."; exit 0
else
echo "python3 not found; cannot run configure script" >&2; exit 1
fi
fi

[[ -d "$SCRIPT_DIR" ]] || fail "Script directory not found: $SCRIPT_DIR"
[[ "$BRIDGE_PORT" =~ ^[0-9]+$ ]] || fail "BRIDGE_PORT must be numeric (got: $BRIDGE_PORT)"
(( BRIDGE_PORT >= 1 && BRIDGE_PORT <= 65535 )) || fail "BRIDGE_PORT out of range: $BRIDGE_PORT"
[[ $START_SERVICES -eq 1 && $APPLY -ne 1 ]] && fail "--start requires --apply"
# Enforce that the current user is root or has passwordless sudo.
# This installer performs system changes; require passwordless sudo to avoid
# interactive password prompts during automation. If this requirement is not
# met, fail early and instruct the user to contact their administrator.
if [[ $EUID -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    fail "This installer requires passwordless sudo or running as root. 'sudo' is not available on PATH. Contact your administrator."
  fi
  # Non-interactive check for passwordless sudo
  if sudo -n true 2>/dev/null; then
    : # passwordless sudo available
  else
    fail "This installer requires passwordless sudo or running as root. Your sudo requires a password - contact your administrator or run this script as root."
  fi
fi
[[ $APPLY -ne 1 && $VERIFY_JSON -ne 1 ]] && echo "Running in dry-run mode. Use --apply to make changes."

if [[ $BACKGROUND_ONLY -eq 1 && $VERIFY_JSON -ne 1 ]]; then
echo "Background-only mode enabled: MCP units will be enabled without immediate start; foreground MCP actions are suppressed."
elif [[ $VERIFY_JSON -ne 1 ]]; then
echo "Eager-start mode enabled: MCP services/timers may start immediately when --start is used."
fi

if [[ $APPLY -eq 1 && $YES -ne 1 ]]; then
echo "About to perform actions on this host:"
echo " - create users: $MCP_USER, $AI_USER"
echo " - create $BASE_DIR and copy files"
echo " - create venv: $VENV_DIR and install python deps"
if [[ $PRECOMPUTE_EMBEDS -eq 1 ]]; then
echo " - precompute embeddings (sentence-transformers / optional FAISS)"
fi
echo " - place systemd units and sudoers snippets"
echo " - install HAL CLI to /usr/local/bin"
echo " - apply SELinux contexts and file permission hardening"
echo
if ! prompt_confirm "Proceed?"; then echo "Aborting."; exit 1; fi
fi

if [[ $UNINSTALL -eq 1 ]]; then
if [[ $APPLY -ne 1 ]]; then
echo "UNINSTALL dry-run: no changes will be made"
fi
if [[ $YES -ne 1 ]]; then
echo "About to uninstall mcp-rhel-manager from this host:"
echo " - disable & remove systemd units"
echo " - remove venv: $VENV_DIR"
echo " - remove base dir: $BASE_DIR"
echo " - remove users: $MCP_USER, $AI_USER"
if ! prompt_confirm "Proceed with uninstall?"; then echo "Aborting uninstall."; exit 1; fi
fi
uninstall_all(){
echo "Performing uninstall (dry-run=$((1-APPLY)) )"
# stop and disable systemd units
run systemctl disable --now mcp-bridge.service mcp-ai-remediator.service mcp-ai-collector.timer mcp-ai-dashboard.service mcp-ai-hal-brain.service || true
run systemctl daemon-reload || true
# remove systemd unit files
run rm -f /etc/systemd/system/mcp-bridge.service /etc/systemd/system/mcp-ai-remediator.service /etc/systemd/system/mcp-ai-collector.service /etc/systemd/system/mcp-ai-collector.timer /etc/systemd/system/mcp-ai-remediator.path /etc/systemd/system/mcp-ai-dashboard.service /etc/systemd/system/mcp-ai-hal-brain.service /etc/systemd/system/mcp-ai-indexer.service /etc/systemd/system/mcp-ai-indexer.timer /etc/systemd/system/mcp-ai-reindex.service /etc/systemd/system/mcp-ai-reindex.timer || true
# remove per-user systemd units
run rm -f "$INSTALL_HOME/.config/systemd/user/mcp-bridge.service" "$INSTALL_HOME/.config/systemd/user/mcp-sentinel.service" "$INSTALL_HOME/.config/systemd/user/mcp-ai-collector.service" "$INSTALL_HOME/.config/systemd/user/mcp-ai-collector.timer" || true
# remove sudoers snippets
run rm -f /etc/sudoers.d/mcp-ai /etc/sudoers.d/mcp-ai-runner || true
# remove venv
run rm -rf "$VENV_DIR" || true
# remove base dir (safe-guarded)
if [[ $FORCE -eq 1 ]]; then
    # Safety: only delete BASE_DIR if it contains a recognizable project marker
    marker_ok=0
    if [[ -f "$BASE_DIR/scripts/install_system.sh" || -d "$BASE_DIR/.git" || -f "$BASE_DIR/scripts/hal.py" ]]; then
        marker_ok=1
    fi
    if [[ $marker_ok -eq 1 || $REALLY_FORCE -eq 1 ]]; then
        run rm -rf "$BASE_DIR" || true
    else
        echo "Refusing to remove $BASE_DIR: no project marker found. Re-run with --really-force to override." >&2
    fi
else
    echo "Not removing $BASE_DIR (use --force to delete)"
fi
# remove users/groups (preserve data option keeps MCP_HOME)
if [[ $PRESERVE_DATA -eq 1 ]]; then
    echo "Preserving data under $MCP_HOME as requested (--preserve-data)"
else
    run rm -rf "$MCP_HOME" || true
fi
run userdel "$MCP_USER" 2>/dev/null || true
run userdel "$AI_USER" 2>/dev/null || true
run groupdel "$AI_USER" 2>/dev/null || true
echo "Uninstall steps complete."
}
uninstall_all
exit 0
fi

PKG_CMD=""
PKG_INSTALL_OPTS=""
if command -v dnf >/dev/null 2>&1; then
PKG_CMD="dnf"; PKG_INSTALL_OPTS="-y"
elif command -v apt-get >/dev/null 2>&1; then
PKG_CMD="apt-get"; PKG_INSTALL_OPTS="-y"
elif command -v yum >/dev/null 2>&1; then
PKG_CMD="yum"; PKG_INSTALL_OPTS="-y"
elif [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
PKG_CMD="brew"; PKG_INSTALL_OPTS=""
fi

# Enforce Linux distro policy: allow RHEL-family and Fedora-family only.
OS_ID=""
OS_ID_LIKE=""
if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  OS_ID="${ID:-}"
  OS_ID_LIKE="${ID_LIKE:-}"
fi

is_supported_linux=0
if [[ "$OS_ID" =~ ^(rhel|centos|rocky|almalinux|ol|fedora)$ ]]; then
  is_supported_linux=1
elif [[ "$OS_ID_LIKE" =~ (rhel|fedora) ]]; then
  is_supported_linux=1
elif [[ "$PKG_CMD" == "dnf" || "$PKG_CMD" == "yum" ]]; then
  # Container/derived images may have generic IDs but still provide compatible package managers.
  is_supported_linux=1
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ERROR: This installer targets Linux only (RHEL/Fedora family)."
  echo "Detected OS: $(uname -s)"
  exit 1
fi

if [[ $is_supported_linux -ne 1 ]]; then
  echo "ERROR: Unsupported Linux distribution."
  echo "Supported families: RHEL-compatible and Fedora."
  echo "Detected ID='${OS_ID:-unknown}' ID_LIKE='${OS_ID_LIKE:-unknown}'."
  exit 1
fi

if [[ "$PKG_CMD" != "dnf" && "$PKG_CMD" != "yum" ]]; then
  echo "ERROR: Supported distro detected but no compatible package manager found."
  echo "Detected package manager: ${PKG_CMD:-none}. Expected: dnf or yum."
  exit 1
fi

# Ensure system python has pip available (best-effort)
ensure_system_pip(){
  if python3 -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  # Try using ensurepip
  if python3 -m ensurepip --upgrade >/dev/null 2>&1; then
    return 0
  fi
  # Try package manager (RHEL/DNF/YUM)
  if [[ -n "$PKG_CMD" ]]; then
    if [[ "$PKG_CMD" == "dnf" || "$PKG_CMD" == "yum" ]]; then
      run "$PKG_CMD" install $PKG_INSTALL_OPTS python3-pip || true
    elif [[ "$PKG_CMD" == "apt-get" ]]; then
      run apt-get update || true
      run apt-get install -y python3-pip || true
    fi
    if python3 -m pip --version >/dev/null 2>&1; then
      return 0
    fi
  fi
  echo "WARNING: pip not available for system python3; some operations may fail." >&2
}

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
elif [[ "$PKG_CMD" == "brew" ]]; then
# macOS: install common utilities via Homebrew
run brew update || true
run brew install python jq git rsync ripgrep pandoc pypdf libmagic coreutils || true
# optional: clamav for malware scanning
run brew install clamav || true
echo "Note: macOS service management uses launchd; installer will not write systemd units."
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
run_as_install_user(){
  if [[ $APPLY -ne 1 ]]; then
    return 0
  fi
  if [[ $EUID -eq 0 && "$INSTALL_USER" != "root" ]]; then
    su - "$INSTALL_USER" -s /bin/bash -c "$*"
  else
    "$@"
  fi
}

# ===========================================================================
ensure_podman_and_registry_login(){
  local registry="registry.redhat.io"

  echo "Container policy: Podman-only runtime for this project"

  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would ensure podman is installed and verify login to $registry"
    return 0
  fi

  if ! command -v podman >/dev/null 2>&1; then
    [[ -n "$PKG_CMD" ]] || fail "podman is required but no supported package manager was detected"
    echo "podman not found; installing podman"
    run "$PKG_CMD" install $PKG_INSTALL_OPTS podman
  fi

  if run_as_install_user podman login --get-login "$registry" >/dev/null 2>&1; then
    echo "Podman login already present for $registry"
    return 0
  fi

  echo "A Red Hat login is required up front to pull images from $registry."
  if [[ $YES -ne 1 ]]; then
    if ! prompt_confirm "Log in to $registry now?"; then
      fail "Login to $registry is required for container operations"
    fi
  fi

  if [[ -n "${REGISTRY_REDHAT_USER:-}" && -n "${REGISTRY_REDHAT_PASSWORD:-}" ]]; then
    printf '%s\n' "$REGISTRY_REDHAT_PASSWORD" | run_as_install_user podman login --username "$REGISTRY_REDHAT_USER" --password-stdin "$registry"
  else
    echo "Opening interactive podman login for $registry as user $INSTALL_USER"
    run_as_install_user podman login "$registry"
  fi

  run_as_install_user podman login --get-login "$registry" >/dev/null 2>&1 || fail "podman login to $registry did not persist"
  echo "Podman login to $registry confirmed"
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
    if [[ ! -f "$BASE_DIR/scripts/install_system.sh" ]]; then
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
"$BASE_DIR/scripts/install_system.sh" \
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
# Ensure pip is present in the venv (some Python builds omit ensurepip)
if [[ $APPLY -eq 1 ]]; then
  run "$VENV_DIR/bin/python" -m ensurepip --upgrade 2>/dev/null || true
fi
if [[ $APPLY -eq 1 ]]; then
if [[ $EUID -ne 0 ]]; then sudo chmod +x "$VENV_DIR/bin"/*; else chmod +x "$VENV_DIR/bin"/*; fi
fi
 run "$VENV_DIR/bin/python" -m pip install -U pip setuptools wheel
  if [[ -f "$BASE_DIR/requirements.txt" ]]; then
echo "Installing Python requirements"
 run "$VENV_DIR/bin/python" -m pip install -r "$BASE_DIR/requirements.txt"
 run "$VENV_DIR/bin/python" -m pip install --upgrade "filelock>=3.24.2" 2>/dev/null || true
echo ""
echo "NOTE: aider-chat is NOT in requirements.txt (filelock conflict)."
echo "You can install optional AI/dev Python tools into the venv as part of install."
echo "  pip install --upgrade aider-chat virtualenv filelock transformers huggingface-hub tox tox-ansible ansible-dev-tools"
echo "  (aider-chat pins filelock==3.20.3; the one-liner upgrades all affected packages together)"

  # Optionally install AI/dev helper packages into the venv when requested (ai-workstation or dev-packages)
  if [[ $AI_WORKSTATION -eq 1 || $DEV_PACKAGES -eq 1 ]]; then
    echo "Installing optional AI/dev Python packages into venv"
    ai_pkgs=(aider-chat virtualenv filelock transformers huggingface-hub tox tox-ansible ansible-dev-tools)
    if [[ $APPLY -eq 1 ]]; then
      run "$VENV_DIR/bin/python" -m pip install --upgrade "${ai_pkgs[@]}" || echo "WARNING: some AI/dev packages failed to install; install manually: pip3 install --upgrade ${ai_pkgs[*]}"
    else
      echo "DRY-RUN: would install AI/dev Python packages into venv: ${ai_pkgs[*]}"
    fi
  fi
else
echo "WARNING: requirements.txt not found at $BASE_DIR/requirements.txt"
fi
if [[ $APPLY -eq 1 ]]; then
 "$VENV_DIR/bin/python" -m pip show ollama-mcp-bridge >/dev/null 2>&1 || run "$VENV_DIR/bin/python" -m pip install ollama-mcp-bridge
else
echo "DRY-RUN: would ensure ollama-mcp-bridge is present"
fi

# Optional: install embedding libs and precompute embeddings
if [[ $PRECOMPUTE_EMBEDS -eq 1 ]]; then
echo "Precompute embeddings requested: installing sentence-transformers and FAISS (if available)"
if [[ $APPLY -eq 1 ]]; then
    # Install sentence-transformers
    run "$VENV_DIR/bin/python" -m pip install -U sentence-transformers || true
    # Try faiss-cpu first; fall back to faiss
    run "$VENV_DIR/bin/python" -m pip install -U faiss-cpu 2>/dev/null || run "$VENV_DIR/bin/python" -m pip install -U faiss 2>/dev/null || echo "faiss install failed; continuing without faiss"
    # Run the precompute script
    if [[ -f "$BASE_DIR/mcp-ai/precompute_embeds.py" ]]; then
        run "$VENV_DIR/bin/python" "$BASE_DIR/mcp-ai/precompute_embeds.py" || echo "Precompute script failed (continuing)"
    else
        echo "Precompute script not found at $BASE_DIR/mcp-ai/precompute_embeds.py"
    fi
else
    echo "DRY-RUN: would install embedding packages and run precompute script"
fi
fi
}

# ===========================================================================
install_dev_packages(){
echo "Installing developer packages (ansible-core, ansible-lint, yamllint, git)"
local pkgs_dnf=(ansible-core ansible-lint yamllint git python3-virtualenv python3-pip)
local pkgs_apt=(ansible-core ansible-lint yamllint git python3-venv python3-pip)
# Allow operator to override default dev package lists via DEV_PKGS env (space-separated)
if [[ -n "${DEV_PKGS:-}" ]]; then
    IFS=' ' read -r -a override <<< "$DEV_PKGS"
    pkgs_dnf=("${override[@]}")
    pkgs_apt=("${override[@]}")
  fi
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would install dev packages: ${pkgs_dnf[*]} (or distro equivalents)"
    return 0
  fi
  if [[ -z "$PKG_CMD" ]]; then
    echo "No supported package manager found; cannot install dev packages." >&2
    return 1
  fi
  if [[ "$PKG_CMD" == "dnf" || "$PKG_CMD" == "yum" ]]; then
    run "$PKG_CMD" install -y "${pkgs_dnf[@]}" || true
  else
    run apt-get update || true
    # Include v4l2loopback-dkms on Debian/Ubuntu as a convenience package
    run apt-get install -y "${pkgs_apt[@]}" v4l2loopback-dkms || true
  fi
  # Install Python-side tools into venv as a convenience
  if [[ -n "$VENV_DIR" && -x "$VENV_DIR/bin/python" ]]; then
    run "$VENV_DIR/bin/python" -m pip install -U ansible-core ansible-lint yamllint || true
  fi
}

# ===========================================================================
install_ai_workstation(){
  echo "Installing AI workstation packages (podman, optional GPU tooling, python libs)"
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would install container runtime (podman), jq, curl, and optional GPU tooling"
    return 0
  fi
  if [[ -z "$PKG_CMD" ]]; then
    echo "No supported package manager found; cannot install AI workstation packages." >&2
    return 1
  fi
  if [[ "$PKG_CMD" == "dnf" || "$PKG_CMD" == "yum" ]]; then
    # Allow override of AI workstation pkgs via AI_PKGS env
    if [[ -n "${AI_PKGS:-}" ]]; then
      IFS=' ' read -r -a aipkgs <<< "$AI_PKGS"
      run "$PKG_CMD" install -y "${aipkgs[@]}" || true
    else
      run "$PKG_CMD" install -y podman buildah jq curl which || true
    fi
  else
    run apt-get update || true
    if [[ -n "${AI_PKGS:-}" ]]; then
      IFS=' ' read -r -a aipkgs <<< "$AI_PKGS"
      run apt-get install -y "${aipkgs[@]}" || true
    else
      run apt-get install -y podman jq curl which || true
    fi
  fi

  # GPU support: try to detect NVIDIA and install nvidia-container-toolkit if present
  if command -v nvidia-smi >/dev/null 2>&1 || lspci | grep -i NVIDIA >/dev/null 2>&1; then
    echo "NVIDIA GPU detected: attempting to install nvidia-container-toolkit (may require vendor repo)"
    if [[ "$PKG_CMD" == "dnf" || "$PKG_CMD" == "yum" ]]; then
      run "$PKG_CMD" install -y nvidia-container-toolkit || echo "nvidia-container-toolkit install failed; please follow vendor docs"
    else
      run apt-get install -y nvidia-container-toolkit || echo "nvidia-container-toolkit install failed; please follow vendor docs"
    fi
  fi

  # Install optional Python ML/LLM helper libs into venv (best-effort)
  if [[ -n "$VENV_DIR" && -x "$VENV_DIR/bin/python" ]]; then
    echo "Installing optional ML helper packages into venv (sentence-transformers, faiss-cpu)"
    run "$VENV_DIR/bin/python" -m pip install -U sentence-transformers faiss-cpu 2>/dev/null || run "$VENV_DIR/bin/python" -m pip install -U sentence-transformers || true
  fi
}

# ===========================================================================
install_v4l2loopback(){
  echo "Installing v4l2loopback DKMS build dependencies and registering module"
  if [[ $APPLY -ne 1 ]]; then
    echo "DRY-RUN: would install kernel headers/devel, make, gcc, dkms and attempt to build/register v4l2loopback"
    return 0
  fi
  # Ensure package manager available
  if [[ -z "$PKG_CMD" ]]; then
    echo "No supported package manager found; cannot install v4l2loopback build deps." >&2
    return 1
  fi

  # If DKMS already has v4l2loopback, skip
  if command -v dkms >/dev/null 2>&1 && dkms status 2>/dev/null | grep -q v4l2loopback; then
    echo "v4l2loopback already registered in DKMS; skipping"
    return 0
  fi

  echo "Installing build deps (dkms, make, gcc, kernel headers/devel)"
  if [[ "$PKG_CMD" == "dnf" || "$PKG_CMD" == "yum" ]]; then
    run "$PKG_CMD" install -y dkms make gcc curl tar git kernel-devel-$(uname -r) kernel-headers-$(uname -r) || true
  else
    run apt-get update || true
    run apt-get install -y dkms build-essential curl tar git linux-headers-$(uname -r) || true
  fi

  # Attempt to fetch and register v4l2loopback via DKMS
  V4L2_VER="0.15.3"
  V4L2_DIR="/usr/src/v4l2loopback-${V4L2_VER}"
  TMPDIR="$(mktemp -d)"
  trap 'rm -rf "$TMPDIR"' EXIT
  if curl -fsSL -o "$TMPDIR/v4l2loopback-v${V4L2_VER}.tar.gz" "https://github.com/umlaeute/v4l2loopback/archive/refs/tags/v${V4L2_VER}.tar.gz"; then
    tar -xzf "$TMPDIR/v4l2loopback-v${V4L2_VER}.tar.gz" -C "$TMPDIR"
    SRC_EXTRACT="$TMPDIR/v4l2loopback-${V4L2_VER}"
    if [[ -d "$SRC_EXTRACT" ]]; then
      run rm -rf "$V4L2_DIR" || true
      run cp -a "$SRC_EXTRACT" "$V4L2_DIR" || true
      run dkms add -m v4l2loopback -v "$V4L2_VER" || true
      if run dkms build -m v4l2loopback -v "$V4L2_VER" && run dkms install -m v4l2loopback -v "$V4L2_VER"; then
        echo "v4l2loopback DKMS built and installed"
      else
        echo "v4l2loopback DKMS build failed; attempting to install/update kernel devel/headers and retry"
        if [[ "$PKG_CMD" == "dnf" || "$PKG_CMD" == "yum" ]]; then
          run "$PKG_CMD" install -y kernel-devel-$(uname -r) kernel-headers-$(uname -r) make gcc || true
        else
          run apt-get install -y linux-headers-$(uname -r) build-essential || true
        fi
        if run dkms build -m v4l2loopback -v "$V4L2_VER" && run dkms install -m v4l2loopback -v "$V4L2_VER"; then
          echo "v4l2loopback DKMS built and installed after installing build deps"
        else
          echo "v4l2loopback DKMS rebuild failed; check /var/lib/dkms/v4l2loopback/${V4L2_VER}/build/make.log" >&2
        fi
      fi
    else
      echo "Failed to extract v4l2loopback source" >&2
    fi
  else
    echo "Failed to download v4l2loopback source" >&2
  fi
  trap - EXIT
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
  # If systemd is not present (macOS / other), skip unit installation
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not available on this host; skipping systemd unit installation"
    return 0
  fi
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
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$AI_USER
Group=$AI_USER
Environment=HOME=$MCP_HOME
Environment=PATH=$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RuntimeDirectory=mcp-ai
WorkingDirectory=$BASE_DIR/mcp-ai
# Pre-check Python deps to fail fast if venv or deps are missing
ExecStartPre=$VENV_DIR/bin/python $BASE_DIR/mcp-ai/dashboard.py --check-deps >/dev/null 2>&1 || true
ExecStart=$VENV_DIR/bin/python $BASE_DIR/mcp-ai/dashboard.py
Restart=on-failure
RestartSec=5
LimitNOFILE=4096
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

  # LLM health server (provides /health for llm endpoints)
  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=MCP AI LLM Health Server
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$AI_USER
Group=$AI_USER
Environment=HOME=$MCP_HOME
Environment=PATH=$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WorkingDirectory=$BASE_DIR/mcp-ai
ExecStart=$VENV_DIR/bin/python $BASE_DIR/mcp-ai/llm_health.py --port ${LLM_HEALTH_PORT:-18082}
Restart=on-failure
RestartSec=5
LimitNOFILE=4096
[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-llm-health.service && WROTE_UNIT_HEALTH=1 || true

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

  # Install hal-watch systemd unit (monitoring and auto-fix watcher)
  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=HAL System Watcher (mcp-rhel-manager)
After=network.target

[Service]
Type=simple
Environment=HOME=$MCP_HOME
WorkingDirectory=$BASE_DIR
ExecStart=$VENV_DIR/bin/python $BASE_DIR/scripts/watch_system_and_fix.py --daemon --check-smart --no-reboot
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/hal-watch.service || true
  WROTE_UNIT_WATCH=1 || true

  # Reindex embeddings service + timer (uses scripts/reindex_embeddings.py)
  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=MCP AI Reindex Embeddings (one-shot)
After=network.target
[Service]
Type=oneshot
User=$AI_USER
Group=$AI_USER
Environment=HOME=$MCP_HOME
ExecStart=$BASE_DIR/scripts/reindex_wrapper.sh --input-dir $MCP_HOME/training --out $MCP_HOME/training_index.jsonl --dim 64
TimeoutStartSec=1800
[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-reindex.service || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=Run MCP AI Reindex Embeddings daily
[Timer]
OnBootSec=15min
OnUnitActiveSec=1d
Persistent=true
[Install]
WantedBy=timers.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/mcp-ai-reindex.timer || true

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

  # HAL auto-improve service + timer (runs hal_auto_improve.py periodically)
  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=HAL Auto-Improve Runner (one-shot)
After=network.target

[Service]
Type=oneshot
User=$AI_USER
Group=$AI_USER
Environment=HOME=$MCP_HOME
Environment=HAL_ALLOW_AUTO_IMPROVE_APPLY=1
WorkingDirectory=$BASE_DIR
ExecStart=$VENV_DIR/bin/python $BASE_DIR/scripts/hal_auto_improve.py --root $BASE_DIR --apply --generate-patches --autonomous --max-files 200
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/hal-auto-improve.service && WROTE_UNIT_AUTO_IMPROVE=1 || true

  t="$(mktemp)"
  cat >"$t" <<UNITEOF
[Unit]
Description=Run HAL Auto-Improve periodically
[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true
[Install]
WantedBy=timers.target
UNITEOF
  _install_unit "$t" /etc/systemd/system/hal-auto-improve.timer && WROTE_UNIT_AUTO_IMPROVE=1 || true
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
  "allow_auto_fix": true,
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
  echo "Writing config/mcp-config.json, auto-fixer.sh, and gold seed"
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
    run mkdir -p "$BASE_DIR/config"
    if [[ $EUID -ne 0 ]]; then sudo install -m 644 -o root -g root "$tmp_cfg" "$BASE_DIR/config/mcp-config.json"
    else install -m 644 -o root -g root "$tmp_cfg" "$BASE_DIR/config/mcp-config.json"; fi
    rm -f "$tmp_cfg"; echo "Wrote $BASE_DIR/config/mcp-config.json"
  else
    echo "DRY-RUN: would write $BASE_DIR/config/mcp-config.json"; rm -f "$tmp_cfg"
  fi

  local tmp_af
  tmp_af="$(mktemp)"
  # Use a quoted heredoc to avoid expanding runtime variables and command substitutions now;
  # we'll replace intended placeholders with actual values below.
  cat >"$tmp_af" <<'AFEOF'
#!/bin/bash
# auto-fixer.sh — Architect Sentinel maintenance loop (generated by install_system.sh)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${VENV_DIR}/bin/python"
if [[ ! -x "$PYTHON" ]]; then PYTHON="python3"; fi
# Start watcher in background if not already running
if ! pgrep -f watch_system_and_fix.py >/dev/null 2>&1; then
  "$PYTHON" "$BASE_DIR/scripts/watch_system_and_fix.py" --daemon --check-smart --no-reboot >> "$BASE_DIR/scripts/watch_system_and_fix.log" 2>&1 &
fi
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
  # Substitute placeholders with current paths so the generated script uses concrete locations.
  vdir="${VENV_DIR:-/usr/bin}"
  sed -i 's|${VENV_DIR}|'"$vdir"'|g' "$tmp_af"
  sed -i 's|${BASE_DIR}|'"$BASE_DIR"'|g' "$tmp_af"
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
ExecStart=$VENV_DIR/bin/python -m ollama_mcp_bridge.main --config $BASE_DIR/config/mcp-config.json --port $BRIDGE_PORT
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
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
    if [[ $BACKGROUND_ONLY -eq 1 ]]; then
      systemctl --user enable mcp-ai-collector.timer 2>/dev/null || true
    else
      systemctl --user enable --now mcp-ai-collector.timer 2>/dev/null || true
    fi
    if [[ $START_SERVICES -eq 1 ]]; then
      if [[ $BACKGROUND_ONLY -eq 1 ]]; then
        systemctl --user enable mcp-bridge.service mcp-sentinel.service 2>/dev/null || true
      else
        systemctl --user enable --now mcp-bridge.service mcp-sentinel.service 2>/dev/null || true
      fi
      loginctl enable-linger "$INSTALL_USER" 2>/dev/null || true
    fi
  else
    echo "systemctl not available; skipping per-user systemd enable/start steps"
  fi
  echo "Per-user systemd services written."
}

# ===========================================================================
post_install(){
  echo "Running post-install steps"
  if [[ $APPLY -eq 1 ]]; then
    if command -v systemctl >/dev/null 2>&1; then
      run systemctl daemon-reload
      if [[ $START_SERVICES -eq 1 ]]; then
        if [[ $BACKGROUND_ONLY -eq 1 ]]; then
          run systemctl enable \
            mcp-ai-remediator.path \
            mcp-bridge.service \
            mcp-ai-collector.timer \
            mcp-ai-dashboard.service \
            mcp-ai-hal-brain.service \
            hal-watch.service || true
        else
          run systemctl enable \
            mcp-ai-remediator.path || true
          run systemctl enable --now \
            mcp-bridge.service \
            mcp-ai-collector.timer \
            mcp-ai-dashboard.service \
            mcp-ai-hal-brain.service \
            hal-watch.service || true
        fi
      fi
      # Enable HAL Auto-Improve timer by default (runs periodic self-improve)
      if [[ $BACKGROUND_ONLY -eq 1 ]]; then
        run systemctl enable hal-auto-improve.timer 2>/dev/null || true
      else
        run systemctl enable --now hal-auto-improve.timer 2>/dev/null || true
      fi
    else
      echo "systemctl not present; skipping system-level service enable/start steps"
    fi
    # Optionally trigger one-shot reindex in the background when requested
    if [[ "${HAL_REINDEX_ON_START:-0}" =~ ^(1|true|yes)$ ]]; then
      if [[ $BACKGROUND_ONLY -eq 1 ]]; then
        echo "HAL_REINDEX_ON_START enabled: background-only mode will enable periodic reindex timer instead of immediate run"
        run systemctl enable mcp-ai-reindex.timer || true
      else
        echo "HAL_REINDEX_ON_START enabled: scheduling initial reindex in background"
        run systemctl start mcp-ai-reindex.service || true
      fi
    fi
  else
    echo "DRY-RUN: would run systemctl daemon-reload and optionally enable/start services"
  fi
}

# ===========================================================================
configure_search_backends(){
  # Writes ~/.mcp-ai/search-env with API keys / URLs for optional search backends.
  # In --yes mode, skips interactive prompts (env vars can still be pre-set).
  # In --dry-run mode, just shows what would be written.

  local env_file="$HOME/.mcp-ai/search-env"

  # Collect current / pre-set values
  local brave_key="${BRAVE_API_KEY:-}"
  local tavily_key="${TAVILY_API_KEY:-}"
  local searxng_url="${SEARXNG_URL:-}"

  # Interactive prompts only when YES=0 and APPLY=1
  if [[ $APPLY -eq 1 && $YES -eq 0 ]]; then
    echo ""
    echo "══════════════════════════════════════════════════════"
    echo " Optional search backend configuration"
    echo " (press Enter to skip any backend)"
    echo "══════════════════════════════════════════════════════"
    echo ""

    read -rp "  Brave Search API key (https://api.search.brave.com, free 2k/mo): " _input
    [[ -n "$_input" ]] && brave_key="$_input"

    read -rp "  Tavily API key (https://app.tavily.com, free tier available):    " _input
    [[ -n "$_input" ]] && tavily_key="$_input"

    read -rp "  SearXNG URL (e.g. http://localhost:8888, leave blank to skip):    " _input
    [[ -n "$_input" ]] && searxng_url="$_input"

    echo ""
  fi

  if [[ $APPLY -eq 1 ]]; then
    mkdir -p "$(dirname "$env_file")"
    # Write env file — only emit lines for keys that are set
    {
      echo "# HAL search backend environment — sourced by shell profile"
      echo "# Generated by install_system.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo ""
      [[ -n "$brave_key"   ]] && echo "export BRAVE_API_KEY=\"$brave_key\""
      [[ -n "$tavily_key"  ]] && echo "export TAVILY_API_KEY=\"$tavily_key\""
      [[ -n "$searxng_url" ]] && echo "export SEARXNG_URL=\"$searxng_url\""
    } > "$env_file"
    chmod 600 "$env_file"

    # Source from ~/.bashrc if not already wired in
    local rc_file="$HOME/.bashrc"
    local source_line="# HAL search backends"$'\n'"[[ -f \"$env_file\" ]] && source \"$env_file\""
    if ! grep -qF "$env_file" "$rc_file" 2>/dev/null; then
      printf '\n%s\n' "$source_line" >> "$rc_file"
    fi

    # Also export into the current shell so HAL works immediately
    [[ -n "$brave_key"   ]] && export BRAVE_API_KEY="$brave_key"
    [[ -n "$tavily_key"  ]] && export TAVILY_API_KEY="$tavily_key"
    [[ -n "$searxng_url" ]] && export SEARXNG_URL="$searxng_url"

    # Report what was configured
    local configured=()
    [[ -n "$brave_key"   ]] && configured+=("Brave")
    [[ -n "$tavily_key"  ]] && configured+=("Tavily")
    [[ -n "$searxng_url" ]] && configured+=("SearXNG")
    if [[ ${#configured[@]} -gt 0 ]]; then
      echo "Search backends configured: ${configured[*]} -> $env_file"
    else
      echo "Search backends: no keys provided — only DuckDuckGo + Wikipedia will be used"
      echo "  (re-run install or edit $env_file to add keys later)"
    fi
  else
    echo "DRY-RUN: would write search backend keys to $env_file"
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

  # ── Bash tab-completion ──────────────────────────────────────────────────
  local comp_src="$BASE_DIR/completions/hal.bash"
  if [[ -f "$comp_src" ]]; then
    local comp_dest=""
    # System-wide completion dir (preferred when running as root)
    if [[ $EUID -eq 0 && -d /etc/bash_completion.d ]]; then
      comp_dest="/etc/bash_completion.d/hal"
    fi
    if [[ $APPLY -eq 1 ]]; then
      if [[ -n "$comp_dest" ]]; then
        run cp "$comp_src" "$comp_dest"
        echo "HAL bash completion: $comp_dest"
      else
        # Per-user install via ~/.bashrc sourcing
        local rc_file="$HOME/.bashrc"
        local source_line="# HAL tab-completion"$'\n'"source \"$comp_src\""
        if ! grep -qF "$comp_src" "$rc_file" 2>/dev/null; then
          printf '\n%s\n' "$source_line" >> "$rc_file"
          echo "HAL bash completion: sourced from $rc_file (source ~/.bashrc to activate)"
        else
          echo "HAL bash completion: already in $rc_file"
        fi
      fi
    else
      if [[ -n "$comp_dest" ]]; then
        echo "DRY-RUN: would install $comp_src -> $comp_dest"
      else
        echo "DRY-RUN: would add source $comp_src to ~/.bashrc"
      fi
    fi
  else
    echo "WARN: HAL completion script not found at $comp_src; skipping"
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
    "$BASE_DIR/scripts/install_system.sh" \
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

  # Check Podman and Red Hat registry login (helpful when running --verify)
  local registry="registry.redhat.io"
  if command -v podman >/dev/null 2>&1; then
    if podman login --get-login "$registry" >/dev/null 2>&1; then
      add_result ok podman_login "podman login present for $registry"; emit_line "OK: podman login present for $registry"
    else
      # If credentials are provided via env, attempt a non-persistent login test
      if [[ -n "${REGISTRY_REDHAT_USER:-}" && -n "${REGISTRY_REDHAT_PASSWORD:-}" ]]; then
        emit_line "Attempting temporary podman login to $registry using provided environment credentials (no persistence)"
        tmp_home="$(mktemp -d)"
        # Use a temporary HOME so podman's auth.json is not written to user's real config
        if HOME="$tmp_home" printf '%s\n' "$REGISTRY_REDHAT_PASSWORD" | podman login --username "$REGISTRY_REDHAT_USER" --password-stdin "$registry" >/dev/null 2>&1; then
          add_result ok podman_login_temp "provided credentials validated for $registry (temporary)"; emit_line "OK: provided Red Hat credentials validated (temporary)"
        else
          add_result warn podman_login_invalid "provided Red Hat credentials failed for $registry"; emit_line "WARN: provided Red Hat credentials failed for $registry"
        fi
        rm -rf "$tmp_home" || true
      else
        add_result warn podman_login "podman login missing for $registry"; emit_line "WARN: podman login missing for $registry (run 'podman login $registry' or use install_system.sh to log in)"
      fi
    fi
  else
    add_result warn podman_cmd "podman not installed"; emit_line "WARN: podman not installed"
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
  echo "Starting install (apply=$APPLY start=$START_SERVICES background_only=$BACKGROUND_ONLY venv=$VENV_MODE verify=$VERIFY_MODE force=$FORCE)"
  # Ensure ansible-vault secrets exist for the install user and load them into environment
  if [[ $APPLY -eq 1 ]]; then
    echo "Ensuring Ansible secrets for user: $INSTALL_USER"
    # Create ~/.ansible/conf and vault/env files as the install user when needed
    run_as_install_user "$SCRIPT_DIR/scripts/ansible_secrets/ensure_ansible_secrets.sh" "$INSTALL_HOME" || true
    # Load decrypted env.yml into current shell environment (temporary)
    if command -v python3 >/dev/null 2>&1; then
      eval "$(run_as_install_user python3 \"$SCRIPT_DIR/scripts/ansible_secrets/load_ansible_env.py\" \"$INSTALL_HOME\")" || true
    fi
  fi
  ensure_podman_and_registry_login
  install_prereqs
  # Ensure system pip is available (best-effort) after prereqs
  ensure_system_pip
  create_user_and_dirs
  deploy_files
  create_venv_and_install
  # Optional: install developer / AI workstation packages when requested
  if [[ $DEV_PACKAGES -eq 1 ]]; then
    install_dev_packages
    ASSIMILATE_FIXES=1
  fi
  if [[ $AI_WORKSTATION -eq 1 ]]; then
    install_ai_workstation
    # Attempt to auto-install v4l2loopback and register module for virtual webcam support
    install_v4l2loopback
    ASSIMILATE_FIXES=1
  fi
  setup_ai_user
  # Provision bundled fixes into user's home (useful when assimilating local fix scripts)
  if [[ $ASSIMILATE_FIXES -eq 1 ]]; then
    echo "Provisioning bundled fixes into user environment"
    if [[ $APPLY -eq 1 ]]; then
      if [[ $EUID -eq 0 ]]; then
        su - "$INSTALL_USER" -s /bin/bash -c "$BASE_DIR/mcp-ai/provision_fixes.sh --force" || true
      else
        "$BASE_DIR/mcp-ai/provision_fixes.sh" --force || true
      fi
    else
      echo "DRY-RUN: would run $BASE_DIR/mcp-ai/provision_fixes.sh --force as $INSTALL_USER"
    fi
  fi
  write_systemd_units
  write_sudoers
  write_mcp_config
  write_genesis_config
  setup_user_services
  post_install
  apply_runtime_baseline
  install_hal_cli
  configure_search_backends
  apply_selinux_hardening
    # Ensure Ollama runtime and configured models are present (install/pull if missing)
    ensure_ollama_and_models
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
