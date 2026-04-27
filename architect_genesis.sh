#!/bin/bash
# ==============================================================================
# LENOVO ARCHITECT: GENESIS SCRIPT
# Role: Self-Creating, Self-Healing, Evolving AI Infrastructure
# Supports: RHEL 10 / Fedora / P-Series Mobile Workstations
# ==============================================================================

set -e

# --- Configuration & Paths ---
BASE_DIR="/home/sgallego/mcp-rhel-manager"
ANSIBLE_DIR="$BASE_DIR/ansible/roles/p_series_node"
SEED_DIR="/home/sgallego/.local/share/mcp-seed"
VENV_DIR="$BASE_DIR/venv"
BRIDGE_VENV="$BASE_DIR/venv-bridge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Require user to choose installation mode: venv (recommended) or globally
INSTALL_MODE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --venv)
      INSTALL_MODE="venv"; shift;;
    --globally|--global)
      INSTALL_MODE="global"; shift;;
    -h|--help)
      echo "Usage: $0 --venv|--globally"; exit 0;;
    *)
      echo "Unknown option: $1"; echo "Usage: $0 --venv|--globally"; exit 1;;
  esac
done

if [ -z "$INSTALL_MODE" ]; then
  echo "You must choose either --venv or --globally."
  echo "Usage: $0 --venv|--globally"
  exit 1
fi

echo "Starting Genesis Sequence (install mode: $INSTALL_MODE)..."

# --- 1. Environmental Sanitization ---
systemctl --user stop mcp-sentinel.service mcp-bridge.service 2>/dev/null || true
sudo chattr -i "$SEED_DIR"/* 2>/dev/null || true
mkdir -p "$BASE_DIR" "$ANSIBLE_DIR/tasks" "$ANSIBLE_DIR/vars" "$SEED_DIR"

# --- 2. Dependency Injection ---
echo "Building Python Environments..."
if [ "$INSTALL_MODE" = "venv" ]; then
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install mcp[cli] fastmcp psutil

  python3 -m venv "$BRIDGE_VENV"
  "$BRIDGE_VENV/bin/pip" install --upgrade pip
  "$BRIDGE_VENV/bin/pip" install ollama-mcp-bridge
else
  echo "Installing packages globally using sudo pip3 (this will modify system python packages)"
  sudo pip3 install --upgrade --ignore-installed mcp[cli] fastmcp psutil ollama-mcp-bridge
fi

echo "Installing OS packages required for diagnostics and remediation..."
sudo dnf install -y smartmontools lm_sensors pciutils sof-firmware audit audit-libs auditd firewalld fail2ban iproute || true
sudo systemctl enable --now auditd || true
sudo systemctl enable --now firewalld || true
sudo systemctl enable --now fail2ban || true

# --- 3. Script Generation (The Brain) ---
echo "Deploying server.py from repository template..."
if [ -f "$SCRIPT_DIR/server.py" ]; then
  SRC=$(realpath "$SCRIPT_DIR/server.py")
  DST=""
  if [ -f "$BASE_DIR/server.py" ]; then
    DST=$(realpath "$BASE_DIR/server.py")
  fi
  if [ "$SRC" != "$DST" ]; then
    sudo install -m 755 "$SCRIPT_DIR/server.py" "$BASE_DIR/server.py" || sudo cp -f "$SCRIPT_DIR/server.py" "$BASE_DIR/server.py"
    sudo chmod 755 "$BASE_DIR/server.py" || true
  else
    echo "server.py already deployed at $BASE_DIR/server.py; skipping copy"
  fi
else
  echo "Warning: $SCRIPT_DIR/server.py not found; please ensure server.py is present in the repo." >&2
fi

echo "Generating Ansible remediation task for p_series_node..."
mkdir -p "$ANSIBLE_DIR/tasks"
sudo tee "$ANSIBLE_DIR/tasks/main.yml" > /dev/null <<'YML'
---
# Remediation tasks for Lenovo ThinkPad, IdeaPad, and P and W-Series nodes (populated by architect_genesis.sh)
- name: Install required diagnostic and remediation packages
  become: true
  dnf:
    name:
      - smartmontools
      - lm_sensors
      - pciutils
      - sof-firmware
      - audit
      - audit-libs
      - auditd
      - firewalld
      - fail2ban
      - iproute
    state: present
    update_cache: yes

- name: Ensure auditd is enabled and started
  become: true
  service:
    name: auditd
    state: started
    enabled: yes

- name: Ensure firewalld is enabled and started
  become: true
  service:
    name: firewalld
    state: started
    enabled: yes

- name: Ensure fail2ban is enabled and started (if available)
  become: true
  service:
    name: fail2ban
    state: started
    enabled: yes
  ignore_errors: yes

- name: Deploy systemd drop-in for mcp-bridge to order after ollama
  become: true
  copy:
    dest: /etc/systemd/system/mcp-bridge.service.d/override.conf
    content: |
      [Unit]
      After=ollama.service
      [Service]
      Restart=on-failure

- name: Reload systemd daemon
  become: true
  command: systemctl daemon-reload
  changed_when: false
YML

echo "Creating project CHECKLIST and updating README with today's changes..."
sudo tee "$BASE_DIR/CHECKLIST.md" > /dev/null <<'CHK'
# MCP RHEL Manager - Checklist

- Run genesis (recommended venv): `./architect_genesis.sh --venv`
- Verify venvs: `ls -la venv venv-bridge`
- Ensure OS packages installed: `smartmontools lm_sensors pciutils sof-firmware auditd firewalld fail2ban`
- Start services: `systemctl --user status mcp-bridge.service mcp-sentinel.service` and `systemctl status auditd firewalld`
- Run diagnostics: `python3 -c "import server; print(server.hardware_diagnostics())"`
- Run security scan: `python3 -c "import server; print(server.security_diagnostics())"`
- Run full JSON diagnostics: `python3 -c "import server; print(server.full_diagnostics_json())"`
CHK

sudo tee -a "$BASE_DIR/README.md" > /dev/null <<'RD'

## (2026-04-25) Added diagnostics and remediation features

- `hardware_diagnostics()` now includes full `lspci` and full `dmesg` excerpts, refined temperature thresholds (Critical >=95C, High >=85C), and immediate-first remediation steps under each problem.
- Added `security_diagnostics()` to surface prioritized security issues (SSH brute-force, root logins, sudo failures, SELinux/firewall/auditd state) with concrete remediation commands.
- Added `full_diagnostics_json()` to return structured JSON for programmatic consumption by HAL.
- Created an Ansible remediation task at `ansible/roles/p_series_node/tasks/main.yml` that installs diagnostic packages and configures auditd/firewalld/fail2ban.
- The genesis script now deploys the server template from the repo and prepares the remediation playbook and CHECKLIST.

Run `./architect_genesis.sh --venv` to regenerate everything.
RD

# --- 3.5 AI Data Collection & Remediation ---
echo "Configuring MCP AI data directories in $HOME..."
AI_HOME="$HOME/.mcp-ai"
AI_RAW="$AI_HOME/raw-logs"
AI_TRAIN="$AI_HOME/training"
AI_FIXES="$AI_HOME/fixes"
AI_REPORTS="$AI_HOME/reports"
AI_CACHE="$AI_HOME/cache"
mkdir -p "$AI_RAW" "$AI_TRAIN" "$AI_FIXES" "$AI_REPORTS" "$AI_CACHE"
chmod 700 "$AI_HOME" || true

mkdir -p "$BASE_DIR/mcp-ai"
if [ -f "$BASE_DIR/mcp-ai/collector.py" ]; then
  chmod +x "$BASE_DIR/mcp-ai/collector.py" || true
fi
if [ -f "$BASE_DIR/mcp-ai/remediate.py" ]; then
  chmod +x "$BASE_DIR/mcp-ai/remediate.py" || true
fi

mkdir -p ~/.config/systemd/user
cat << EOF > ~/.config/systemd/user/mcp-ai-collector.service
[Unit]
Description=MCP AI Collector
After=default.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 $BASE_DIR/mcp-ai/collector.py --run
Restart=on-failure
KillMode=process

[Install]
WantedBy=default.target
EOF

cat << EOF > ~/.config/systemd/user/mcp-ai-collector.timer
[Unit]
Description=Periodic MCP AI Collector

[Timer]
OnBootSec=2min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload || true
systemctl --user enable --now mcp-ai-collector.timer || true

# Remediator service (oneshot, run on-demand)
cat << EOF > ~/.config/systemd/user/mcp-ai-remediator.service
[Unit]
Description=MCP AI Remediator (on-demand)

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 $BASE_DIR/mcp-ai/remediate.py --latest
TimeoutStartSec=600

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload || true

echo "AI collector service installed and timer enabled. Logs under $AI_RAW; training data under $AI_TRAIN."

# --- 3.6 AI system user, sudo, and container access ---
CURRENT_USER="${SUDO_USER:-$(whoami)}"
AI_USER="mcp-ai"
echo "Creating AI system user and configuring passwordless sudo (AI user: ${AI_USER})"

# create group and user if missing
if ! getent group "${AI_USER}" >/dev/null 2>&1; then
  sudo groupadd -r "${AI_USER}" || true
fi
if ! id -u "${AI_USER}" >/dev/null 2>&1; then
  sudo useradd -m -g "${AI_USER}" -s /bin/bash "${AI_USER}" || true
fi

# add current user to AI group so they can interact/run as AI
if id -nG "${CURRENT_USER}" | grep -qw "${AI_USER}"; then
  echo "${CURRENT_USER} already in group ${AI_USER}"
else
  sudo usermod -aG "${AI_USER}" "${CURRENT_USER}" || true
  echo "Added ${CURRENT_USER} to group ${AI_USER} (may require re-login)"
fi

# sudoers: lock down sudo to a validated runner script (mcp-ai-runner)
RUNNER_PATH="/usr/local/bin/mcp-ai-runner"
if [ -f "$BASE_DIR/mcp-ai/runner.py" ]; then
  sudo cp -f "$BASE_DIR/mcp-ai/runner.py" "$RUNNER_PATH" || true
  sudo chmod 755 "$RUNNER_PATH" || true
fi

SUDO_FILE="/etc/sudoers.d/mcp-ai"
sudo tee "${SUDO_FILE}" >/dev/null <<EOF
# mcp-ai: allow AI group and current user to execute only the mcp-ai-runner wrapper as root
%${AI_USER} ALL=(ALL) NOPASSWD: ${RUNNER_PATH}
${CURRENT_USER} ALL=(ALL) NOPASSWD: ${RUNNER_PATH}
EOF
sudo chmod 0440 "${SUDO_FILE}" || true

# Configure subuid/subgid for rootless containers for the AI user (if not present)
if ! grep -q "^${AI_USER}:" /etc/subuid 2>/dev/null; then
  echo "${AI_USER}:100000:65536" | sudo tee -a /etc/subuid >/dev/null || true
fi
if ! grep -q "^${AI_USER}:" /etc/subgid 2>/dev/null; then
  echo "${AI_USER}:100000:65536" | sudo tee -a /etc/subgid >/dev/null || true
fi

# Ensure podman is installed so the AI can pull/run containers if needed
if ! command -v podman >/dev/null 2>&1; then
  echo "podman not found; attempting to install via dnf"
  sudo dnf install -y podman || true
fi

# Write default AI config (auto-remediate enabled by default as requested)
cat > "$AI_HOME/config.json" <<CFG
{
  "ai_user": "${AI_USER}",
  "auto_remediate": true,
  "allow_auto_fix": true,
  "max_retries": 2
}
CFG
chmod 600 "$AI_HOME/config.json" || true

echo "AI user (${AI_USER}) created, sudoers configured, podman installed (if available)."
# --- 3.7 HAL CLI install ---
echo "Ensuring HAL CLI is executable and available in /usr/local/bin"
if [ -f "$BASE_DIR/hal.py" ]; then
  chmod +x "$BASE_DIR/hal.py" || true
fi
if [ -f "$BASE_DIR/HAL" ]; then
  chmod +x "$BASE_DIR/HAL" || true
  if sudo test -d /usr/local/bin >/dev/null 2>&1; then
    sudo ln -sf "$BASE_DIR/HAL" /usr/local/bin/HAL || sudo cp -f "$BASE_DIR/HAL" /usr/local/bin/HAL || true
    sudo chmod 755 /usr/local/bin/HAL || true
    # also create a lowercase alias 'hal' for convenience
    sudo ln -sf "$BASE_DIR/HAL" /usr/local/bin/hal || sudo cp -f "$BASE_DIR/HAL" /usr/local/bin/hal || true
    sudo chmod 755 /usr/local/bin/hal || true
  else
    echo "/usr/local/bin not present; HAL wrapper available at $BASE_DIR/HAL"
  fi
fi

# --- 3.8 Push to /opt and system-level installation ---
echo "Installing project to /opt/mcp-rhel-manager and configuring system-level services..."
OPT_DIR="/opt/mcp-rhel-manager"
sudo mkdir -p "$OPT_DIR"
# Copy repository to /opt (exclude git metadata)
sudo rsync -a --delete --exclude='.git' "$BASE_DIR/" "$OPT_DIR/" || sudo cp -a "$BASE_DIR/." "$OPT_DIR/" || true
sudo chown -R root:root "$OPT_DIR" || true

# Create a dedicated venv under /opt
if [ "$INSTALL_MODE" = "venv" ]; then
  echo "Creating venv at $OPT_DIR/venv"
  sudo python3 -m venv "$OPT_DIR/venv" || true
  sudo "$OPT_DIR/venv/bin/pip" install --upgrade pip setuptools wheel || true
  # Install project and mcp-ai requirements if present
  if [ -f "$OPT_DIR/requirements.txt" ]; then
    sudo "$OPT_DIR/venv/bin/pip" install -r "$OPT_DIR/requirements.txt" || true
  fi
  if [ -d "$OPT_DIR/mcp-ai" ] && [ -f "$OPT_DIR/mcp-ai/requirements.txt" ]; then
    sudo "$OPT_DIR/venv/bin/pip" install -r "$OPT_DIR/mcp-ai/requirements.txt" || true
  fi
  # Ensure minimal runtime deps for dashboard/bridge
  sudo "$OPT_DIR/venv/bin/pip" install fastmcp mcp[cli] psutil ollama-mcp-bridge flask || true
fi

# Prepare system-wide AI home and move existing training data there
MCP_SYS_HOME="/var/lib/mcp"
sudo mkdir -p "$MCP_SYS_HOME"
sudo chown root:root "$MCP_SYS_HOME"
sudo chmod 750 "$MCP_SYS_HOME"

# If we have per-user AI data, copy it into system home and make mcp-ai owner
if [ -d "$AI_HOME" ]; then
  sudo rsync -a "$AI_HOME/" "$MCP_SYS_HOME/" || true
fi

# Ensure mcp-ai user uses /var/lib/mcp as HOME for the service
if id -u "$AI_USER" >/dev/null 2>&1; then
  sudo usermod -d "$MCP_SYS_HOME" "$AI_USER" || true
  sudo chown -R "$AI_USER":"$AI_USER" "$MCP_SYS_HOME" || true
fi

# Deploy redact_training.py from the installed /opt tree into system AI home (owned by mcp-ai)
# Fallback to the repo copy in $BASE_DIR if /opt does not contain the script.
if [ -f "$OPT_DIR/mcp-ai/redact_training.py" ] || [ -f "$BASE_DIR/mcp-ai/redact_training.py" ]; then
  sudo mkdir -p "$MCP_SYS_HOME"
  if [ -f "$OPT_DIR/mcp-ai/redact_training.py" ]; then
    SRC="$OPT_DIR/mcp-ai/redact_training.py"
  else
    SRC="$BASE_DIR/mcp-ai/redact_training.py"
  fi
  sudo cp -f "$SRC" "$MCP_SYS_HOME/redact_training.py" || true
  sudo chown mcp-ai:mcp-ai "$MCP_SYS_HOME/redact_training.py" || true
  sudo chmod 0750 "$MCP_SYS_HOME/redact_training.py" || true
fi

# Ensure a system-level AI config exists (includes conversational toggle used by HAL)
sudo mkdir -p "$MCP_SYS_HOME/.mcp-ai"
sudo tee "$MCP_SYS_HOME/.mcp-ai/config.json" > /dev/null <<CFG
{
  "ai_user": "${AI_USER}",
  "auto_remediate": true,
  "allow_auto_fix": true,
  "max_retries": 2,
  "conversational": true
}
CFG
sudo chown -R mcp-ai:mcp-ai "$MCP_SYS_HOME/.mcp-ai" || true
sudo chmod 0640 "$MCP_SYS_HOME/.mcp-ai/config.json" || true

# Install system indexer script to system AI home so mcp-ai can run it
# Fallback to the repository copy in $BASE_DIR if /opt does not contain the script
if [ -f "$OPT_DIR/mcp-ai/indexer.py" ] || [ -f "$BASE_DIR/mcp-ai/indexer.py" ]; then
  sudo mkdir -p "$MCP_SYS_HOME/training"
  if [ -f "$OPT_DIR/mcp-ai/indexer.py" ]; then
    IDX_SRC="$OPT_DIR/mcp-ai/indexer.py"
  else
    IDX_SRC="$BASE_DIR/mcp-ai/indexer.py"
  fi
  sudo cp -f "$IDX_SRC" "$MCP_SYS_HOME/indexer.py" || true
  sudo chown mcp-ai:mcp-ai "$MCP_SYS_HOME/indexer.py" || true
  sudo chmod 750 "$MCP_SYS_HOME/indexer.py" || true
fi

# Install systemd unit files for collector, remediator and dashboard (system-level)
echo "Writing system-level systemd units for mcp-ai services"
sudo tee /etc/systemd/system/mcp-ai-collector.service > /dev/null <<'UNIT'
[Unit]
Description=MCP AI Collector (system)
After=network.target

[Service]
Type=simple
User=mcp-ai
Group=mcp-ai
Environment=HOME=/var/lib/mcp
ExecStart=/opt/mcp-rhel-manager/venv/bin/python /opt/mcp-rhel-manager/mcp-ai/collector.py --run
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/mcp-ai-remediator.service > /dev/null <<'UNIT'
[Unit]
Description=MCP AI Remediator (system, on-demand)
After=network.target

[Service]
Type=oneshot
User=mcp-ai
Group=mcp-ai
Environment=HOME=/var/lib/mcp
ExecStart=/opt/mcp-rhel-manager/venv/bin/python /opt/mcp-rhel-manager/mcp-ai/remediate.py --latest
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/mcp-ai-dashboard.service > /dev/null <<'UNIT'
[Unit]
Description=MCP AI Dashboard (Flask)
After=network.target

[Service]
Type=simple
User=mcp-ai
Group=mcp-ai
Environment=HOME=/var/lib/mcp
WorkingDirectory=/opt/mcp-rhel-manager/mcp-ai
ExecStart=/opt/mcp-rhel-manager/venv/bin/python /opt/mcp-rhel-manager/mcp-ai/dashboard.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

# System indexer service and timer: creates a system inventory JSONL for training
sudo tee /etc/systemd/system/mcp-ai-indexer.service > /dev/null <<'UNIT'
[Unit]
Description=MCP AI System Indexer
After=network.target

[Service]
Type=oneshot
User=mcp-ai
Group=mcp-ai
Environment=HOME=/var/lib/mcp
ExecStart=/opt/mcp-rhel-manager/venv/bin/python /var/lib/mcp/indexer.py --outdir /var/lib/mcp/training
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/mcp-ai-indexer.timer > /dev/null <<'UNIT'
[Unit]
Description=Run MCP AI System Indexer daily

[Timer]
OnBootSec=10min
OnUnitActiveSec=1d
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload || true
sudo systemctl enable --now mcp-ai-collector.service mcp-ai-dashboard.service || true

# Install a runner wrapper that uses the /opt venv python
echo "Installing runner wrapper at /usr/local/bin/mcp-ai-runner"
sudo tee /usr/local/bin/mcp-ai-runner > /dev/null <<'RUN'
#!/bin/bash
set -e
VENV_PY=/opt/mcp-rhel-manager/venv/bin/python
SCRIPT=/opt/mcp-rhel-manager/mcp-ai/mcp_rpc.py
if [ -x "$VENV_PY" ]; then
  exec "$VENV_PY" "$SCRIPT" "$@"
else
  exec /usr/bin/python3 "$SCRIPT" "$@"
fi
RUN
sudo chmod 0755 /usr/local/bin/mcp-ai-runner || true

# Record runner checksum for audit/integrity
if [ -f "$RUNNER_PATH" ]; then
  sudo mkdir -p "$MCP_SYS_HOME/.mcp-ai" || true
  RUNNER_SHA=$(sha256sum "$RUNNER_PATH" 2>/dev/null | awk '{print $1}') || true
  if [ -n "$RUNNER_SHA" ]; then
    echo "$RUNNER_SHA  $RUNNER_PATH" | sudo tee "$MCP_SYS_HOME/runner.sha256" >/dev/null || true
    sudo chown root:root "$MCP_SYS_HOME/runner.sha256" || true
    sudo chmod 0644 "$MCP_SYS_HOME/runner.sha256" || true
  fi
fi

# post-install hardening, SELinux contexts, auto-trigger and redaction
echo "Installed /opt deployment and system services. Applying hardening and auto-trigger..."

# Create a systemd .path to auto-run remediator when new fixes appear
sudo tee /etc/systemd/system/mcp-ai-remediator.path > /dev/null <<'PATHUNIT'
[Unit]
Description=Watch /var/lib/mcp/fixes for new fix plans and trigger remediator

[Path]
PathExistsGlob=/var/lib/mcp/fixes/*.json
Unit=mcp-ai-remediator.service

[Install]
WantedBy=multi-user.target
PATHUNIT

sudo systemctl daemon-reload || true
sudo systemctl enable --now mcp-ai-remediator.path || true

echo "Applying file permissions and SELinux file contexts..."
# Ensure safe permissions under /opt and /var/lib/mcp
sudo chown -R root:root /opt/mcp-rhel-manager || true
sudo find /opt/mcp-rhel-manager -type d -exec chmod 0755 {} + || true
sudo find /opt/mcp-rhel-manager -type f -exec chmod 0644 {} + || true
sudo chmod 0755 /opt/mcp-rhel-manager/architect_genesis.sh || true
sudo chmod 0755 /opt/mcp-rhel-manager/mcp-ai/*.py || true

sudo chown -R mcp-ai:mcp-ai /var/lib/mcp || true
sudo find /var/lib/mcp -type d -exec chmod 0750 {} + || true
sudo find /var/lib/mcp -type f -exec chmod 0640 {} + || true
sudo chmod 0750 /var/lib/mcp/*.sh 2>/dev/null || true

# Apply SELinux contexts if semanage is available (install if necessary)
if command -v semanage >/dev/null 2>&1; then
  sudo semanage fcontext -a -t var_lib_t "/var/lib/mcp(/.*)?" >/dev/null 2>&1 || true
  sudo restorecon -Rv /var/lib/mcp || true
else
  sudo dnf install -y policycoreutils-python-utils || true
  if command -v semanage >/dev/null 2>&1; then
    sudo semanage fcontext -a -t var_lib_t "/var/lib/mcp(/.*)?" >/dev/null 2>&1 || true
    sudo restorecon -Rv /var/lib/mcp || true
  fi
fi

# Run supplemental redaction if combined file exists
COMBINED="$(ls -1t /var/lib/mcp/training/supplemental-combined-*.jsonl 2>/dev/null | head -n1 || true)"
if [ -n "$COMBINED" ]; then
  REDACTED="/var/lib/mcp/training/$(basename "$COMBINED" .jsonl)-redacted-$(date -u +%Y%m%dT%H%M%SZ).jsonl"
  echo "Redacting combined training file: $COMBINED -> $REDACTED"
  # Prefer the system-local redact script if present; fall back to /opt copy
  if [ -x "$MCP_SYS_HOME/redact_training.py" ]; then
    RT="$MCP_SYS_HOME/redact_training.py"
  elif [ -f "$OPT_DIR/mcp-ai/redact_training.py" ]; then
    RT="$OPT_DIR/mcp-ai/redact_training.py"
  else
    RT=""
  fi

  if [ -n "$RT" ]; then
    if [ -x "$OPT_DIR/venv/bin/python" ]; then
      sudo -u mcp-ai "$OPT_DIR/venv/bin/python" "$RT" --infile "$COMBINED" --outfile "$REDACTED" || true
    elif [ -x "/opt/mcp-rhel-manager/venv/bin/python" ]; then
      sudo -u mcp-ai /opt/mcp-rhel-manager/venv/bin/python "$RT" --infile "$COMBINED" --outfile "$REDACTED" || true
    else
      sudo -u mcp-ai python3 "$RT" --infile "$COMBINED" --outfile "$REDACTED" || true
    fi
  else
    echo "No redact script available (checked $MCP_SYS_HOME and $OPT_DIR); skipping redaction"
  fi
fi


# --- 4. Systemd & Sentinel Agent ---
echo "Configuring Background Sentinel..."
sudo tee "$BASE_DIR/mcp-config.json" > /dev/null << EOF
{
  "mcpServers": {
    "architect": {
      "command": "$VENV_DIR/bin/python",
      "args": ["$BASE_DIR/server.py"]
    }
  }
}
EOF

sudo tee "$BASE_DIR/auto-fixer.sh" > /dev/null << 'EOF'
#!/bin/bash
while true; do
  curl -s -X POST http://localhost:1776/api/chat -H "Content-Type: application/json" -d '{
    "model": "qwen2.5-coder:7b",
    "messages": [
      {
        "role": "system", 
        "content": "You are the Architect Sentinel. 1. Run predict_failure_and_evacuate. 2. Run optimize_ai_performance. 3. Run sentinel_scan. 4. If all good, respond SYSTEM_EVOLVING."
      },
      {"role": "user", "content": "Execute maintenance."}
    ]
  }' >> /home/sgallego/mcp-rhel-manager/evolution.log
  sleep 3600
done
EOF
sudo chmod +x "$BASE_DIR/auto-fixer.sh"

# --- 5. Hardening & Persistence ---
echo "Locking the Gold Seed (Immutable)..."
cp "$BASE_DIR/server.py" "$SEED_DIR/server.py.gold"
sudo chattr +i "$SEED_DIR/server.py.gold"

# Service Creation
mkdir -p ~/.config/systemd/user/
if [ "$INSTALL_MODE" = "venv" ]; then
  BRIDGE_EXEC="$BRIDGE_VENV/bin/python"
else
  BRIDGE_EXEC="/usr/bin/python3"
fi
cat << EOF > ~/.config/systemd/user/mcp-bridge.service
[Unit]
Description=Ollama MCP Bridge
[Service]
ExecStart=$BRIDGE_EXEC -m ollama_mcp_bridge.main --config $BASE_DIR/mcp-config.json --port 1776
Restart=always
[Install]
WantedBy=default.target
EOF

cat << EOF > ~/.config/systemd/user/mcp-sentinel.service
[Unit]
Description=MCP Architect Sentinel
After=mcp-bridge.service
[Service]
ExecStart=$BASE_DIR/auto-fixer.sh
Restart=always
[Install]
WantedBy=default.target
EOF

# --- 6. Activation ---
sudo firewall-cmd --set-log-denied=all
systemctl --user daemon-reload
systemctl --user enable --now mcp-bridge.service mcp-sentinel.service
sudo loginctl enable-linger sgallego

echo "=============================================================================="
echo "GENESIS COMPLETE. The Architect is now living on this system."
echo "Hardware Detected: $(cat /sys/class/dmi/id/product_name)"
echo "Evolution Log: tail -f $BASE_DIR/evolution.log"
echo "=============================================================================="

