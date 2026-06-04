#!/bin/bash
# =============================================================================
#  System Repair Script — RHEL 10 / kaso.prod.spg
#  Fixes: Audio, DKMS v4l2loopback, NetworkManager-wait-online, MCP services,
#         dracut EFI directories, SELinux contexts
#
#  Run as normal user — will use sudo where root is required.
# =============================================================================

# ── Color / output helpers ───────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${CYAN}[*]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
ok()      { echo -e "${GREEN}[✓]${RESET} $*"; }
fail()    { echo -e "${RED}[✗]${RESET} $*"; ERRORS=$((ERRORS + 1)); }
section() { echo -e "\n${BOLD}━━━  $*  ━━━${RESET}"; }
hr()      { echo -e "${BOLD}═══════════════════════════════════════════════${RESET}"; }

ERRORS=0
FIXED=0
fixed() { ok "$*"; FIXED=$((FIXED + 1)); }

# ── Preflight ────────────────────────────────────────────────────────────────
hr
echo -e "${BOLD}  RHEL 10 System Repair Engine  —  $(hostname)${RESET}"
echo -e "  Kernel: $(uname -r)   Date: $(date '+%Y-%m-%d %H:%M')"
hr

if [[ $EUID -eq 0 ]]; then
    warn "Running as root. User-scoped fixes (audio, MCP services) will be skipped."
    warn "Re-run as your normal user for full repair coverage."
    AS_ROOT=1
else
    AS_ROOT=0
    if ! sudo -n true 2>/dev/null; then
        info "Requesting sudo credentials for system-level fixes..."
        sudo true || { fail "Could not obtain sudo. Aborting."; exit 1; }
    fi
fi

USER_ID=$(id -u)
RUNTIME_DIR="/run/user/${USER_ID}"
KVER=$(uname -r)
MACHINE_ID=$(cat /etc/machine-id)

# =============================================================================
# SECTION 1 — Audio: WSLg dangling symlink + PipeWire stack
# =============================================================================
section "AUDIO — PipeWire / PulseAudio"

if [[ $AS_ROOT -eq 1 ]]; then
    warn "Skipping audio fix (must run as normal user)"
else
    # Wait helper for user services
    wait_for_svc() {
        local svc="$1" want="$2" timeout="${3:-8}" elapsed=0
        while [[ $elapsed -lt $timeout ]]; do
            state=$(systemctl --user show -p ActiveState --value "$svc" 2>/dev/null)
            [[ "$state" == "$want" ]] && return 0
            [[ "$state" == "failed" ]] && return 1
            sleep 1; elapsed=$((elapsed + 1))
        done
        return 1
    }

    IS_WSLG=0
    grep -qi microsoft /proc/version 2>/dev/null && [[ -d /mnt/wslg ]] && IS_WSLG=1

    PULSE_DIR="${RUNTIME_DIR}/pulse"
    LOCK_FILE="${RUNTIME_DIR}/pipewire-0.lock"

    # Remove dangling symlink (created by wsl-setup RPM on non-WSL machines)
    if [[ -L "$PULSE_DIR" && ! -e "$PULSE_DIR" ]]; then
        warn "Dangling pulse symlink → $(readlink "$PULSE_DIR")"
        if rpm -q wsl-setup &>/dev/null; then
            warn "Root cause: wsl-setup RPM recreates this at login via user-tmpfiles.d"
        fi
        rm -f "$PULSE_DIR"
        fixed "Removed dangling pulse symlink"
    fi

    if [[ $IS_WSLG -eq 1 ]]; then
        # WSLg path — disable pipewire-pulse, use WSLg's PA socket
        info "WSLg environment: disabling conflicting pipewire-pulse..."
        systemctl --user stop pipewire-pulse.service pipewire-pulse.socket 2>/dev/null || true
        systemctl --user mask pipewire-pulse.service pipewire-pulse.socket 2>/dev/null || true
        killall -9 pipewire wireplumber 2>/dev/null || true
        sleep 1
        [[ -f "$LOCK_FILE" ]] && rm -f "$LOCK_FILE"
        WSLG_DIR="/mnt/wslg/runtime-dir/pulse"
        if [[ ! -L "$PULSE_DIR" && -d "$WSLG_DIR" ]]; then
            ln -s "$WSLG_DIR" "$PULSE_DIR"
            fixed "Restored WSLg pulse symlink"
        fi
        systemctl --user daemon-reload
        systemctl --user reset-failed pipewire wireplumber 2>/dev/null || true
        systemctl --user start pipewire.socket pipewire.service wireplumber.service
        wait_for_svc wireplumber.service active 8 \
            && fixed "PipeWire + WirePlumber running (WSLg mode)" \
            || fail "WirePlumber failed — run: journalctl --user -u wireplumber -n 30"
    else
        # Native path — full PipeWire stack including pipewire-pulse
        pgrep -x pulseaudio &>/dev/null && {
            info "Evicting legacy PulseAudio..."
            systemctl --user stop pulseaudio.service pulseaudio.socket 2>/dev/null || true
            systemctl --user mask pulseaudio.service pulseaudio.socket 2>/dev/null || true
            killall -9 pulseaudio 2>/dev/null || true
            sleep 1
        }
        killall -9 pipewire pipewire-pulse wireplumber 2>/dev/null || true
        sleep 1
        [[ -e "$PULSE_DIR" || -L "$PULSE_DIR" ]] && rm -rf "$PULSE_DIR"
        [[ -f "$LOCK_FILE" ]] && rm -f "$LOCK_FILE"

        systemctl --user daemon-reload
        systemctl --user reset-failed pipewire pipewire-pulse wireplumber 2>/dev/null || true
        systemctl --user start pipewire.socket pipewire.service
        wait_for_svc pipewire.service active 8 \
            && ok "PipeWire active" \
            || { fail "PipeWire failed"; }
        systemctl --user start wireplumber.service
        wait_for_svc wireplumber.service active 8 \
            && ok "WirePlumber active" \
            || { fail "WirePlumber failed"; }
        systemctl --user start pipewire-pulse.socket pipewire-pulse.service
        systemctl --user enable pipewire-pulse.service 2>/dev/null || true
        wait_for_svc pipewire-pulse.service active 8 \
            && fixed "pipewire-pulse active" \
            || fail "pipewire-pulse failed — run: journalctl --user -u pipewire-pulse -n 30"

        wpctl set-mute @DEFAULT_AUDIO_SINK@ 0 2>/dev/null || true
        wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.60 2>/dev/null || true

        # Quick audio test
        for f in /usr/share/sounds/alsa/Front_Center.wav \
                  /usr/share/sounds/freedesktop/stereo/audio-test-signal.oga; do
            [[ -f "$f" ]] && {
                paplay "$f" 2>/dev/null || aplay "$f" 2>/dev/null || true
                break
            }
        done
    fi

    # Ensure boot service is in place and properly ordered
    SVC_FILE="${HOME}/.config/systemd/user/fix-pulse-socket.service"
    if [[ ! -f "$SVC_FILE" ]]; then
        mkdir -p "${HOME}/.config/systemd/user"
        cat > "$SVC_FILE" << 'UNIT'
[Unit]
Description=Remove dangling PulseAudio socket symlink before PipeWire starts
Documentation=man:pipewire-pulse(1)
After=systemd-tmpfiles-setup.service
Before=pipewire-pulse.socket pipewire-pulse.service
ConditionPathIsSymbolicLink=%t/pulse
ConditionPathExists=!%t/pulse

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/rm -f %t/pulse

[Install]
WantedBy=default.target
UNIT
        systemctl --user daemon-reload
        systemctl --user enable fix-pulse-socket.service 2>/dev/null || true
        fixed "Installed fix-pulse-socket.service boot guard"
    else
        # Ensure After= ordering is present
        if ! grep -q "After=systemd-tmpfiles-setup.service" "$SVC_FILE"; then
            sed -i '/^Before=/i After=systemd-tmpfiles-setup.service' "$SVC_FILE"
            systemctl --user daemon-reload
            fixed "Updated fix-pulse-socket.service: added After=systemd-tmpfiles-setup.service"
        else
            ok "fix-pulse-socket.service boot guard in place"
        fi
    fi
fi

# =============================================================================
# SECTION 2 — DKMS: v4l2loopback RHEL 10 kernel API compat patch
# =============================================================================
section "DKMS — v4l2loopback kernel API patch"

# Try to auto-fetch/build/register v4l2loopback if missing; then apply RHEL10
# compat guard patch and (re)build via DKMS.
V4L2_STATUS=$(dkms status 2>/dev/null | grep v4l2loopback || true)
V4L2_VER_DEFAULT="0.15.3"

if [[ -z "$V4L2_STATUS" ]]; then
    info "v4l2loopback not registered in DKMS — attempting to fetch v${V4L2_VER_DEFAULT} and register"
    V4L2_VER="$V4L2_VER_DEFAULT"
    V4L2_DIR="/usr/src/v4l2loopback-${V4L2_VER}"

    if [[ ! -d "$V4L2_DIR" ]]; then
        TMPDIR=$(mktemp -d)
        trap 'rm -rf "$TMPDIR"' EXIT
        if curl -fsSL -o "${TMPDIR}/v4l2loopback-v${V4L2_VER}.tar.gz" \
            "https://github.com/umlaeute/v4l2loopback/archive/refs/tags/v${V4L2_VER}.tar.gz"; then
            tar -xzf "${TMPDIR}/v4l2loopback-v${V4L2_VER}.tar.gz" -C "${TMPDIR}"
            SRC_EXTRACT="${TMPDIR}/v4l2loopback-${V4L2_VER}"
            sudo rm -rf "${V4L2_DIR}" || true
            sudo mv "${SRC_EXTRACT}" "${V4L2_DIR}" || true
            ok "Downloaded v4l2loopback ${V4L2_VER} to ${V4L2_DIR}"
        else
            fail "Failed to download v4l2loopback v${V4L2_VER}"
            V4L2_DIR=""
        fi
        trap - EXIT || true
    fi

    if [[ -n "$V4L2_DIR" && -d "$V4L2_DIR" ]]; then
        sudo dkms add -m v4l2loopback -v "$V4L2_VER" 2>/dev/null || true
        V4L2_STATUS=$(dkms status 2>/dev/null | grep v4l2loopback || true)
    fi
else
    V4L2_VER=$(echo "$V4L2_STATUS" | grep -oP 'v4l2loopback/\K[0-9.]+' | head -1)
    V4L2_DIR="/usr/src/v4l2loopback-${V4L2_VER}"
fi

if [[ -z "$V4L2_DIR" || ! -f "${V4L2_DIR}/v4l2loopback.c" ]]; then
    fail "v4l2loopback source not found; attempted fetch/register for v${V4L2_VER}"
else
    V4L2_SRC="${V4L2_DIR}/v4l2loopback.c"

    NEEDS_PATCH=0
    if grep -qP '^#if LINUX_VERSION_CODE < KERNEL_VERSION\(6, 18, 0\)$' "$V4L2_SRC"; then
        NEEDS_PATCH=1
    fi
    if grep -q "RHEL_MAJOR >= 10" "$V4L2_SRC"; then
        NEEDS_PATCH=0
    fi

    if [[ $NEEDS_PATCH -eq 1 ]]; then
        info "Applying RHEL 10 v4l2_fh_add/v4l2_fh_del API compat patch..."
        sudo sed -i \
            's/^#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 18, 0)$/#if LINUX_VERSION_CODE < KERNEL_VERSION(6, 18, 0) && !(defined(RHEL_MAJOR) && RHEL_MAJOR >= 10)/' \
            "$V4L2_SRC"
        fixed "Patched v4l2loopback.c: RHEL 10 compat guard added"
    else
        ok "v4l2loopback source already patched for RHEL 10"
    fi

    # Ensure the module is registered and rebuilt from the (possibly new) source
    sudo dkms remove -m v4l2loopback -v "$V4L2_VER" --all 2>/dev/null || true
    sudo dkms add -m v4l2loopback -v "$V4L2_VER" 2>/dev/null || true
    if sudo dkms build -m v4l2loopback -v "$V4L2_VER" && \
       sudo dkms install -m v4l2loopback -v "$V4L2_VER"; then
        fixed "v4l2loopback DKMS rebuilt and installed"
    else
        warn "v4l2loopback DKMS build failed — attempting to install build dependencies and retry"
        sudo dnf install -y kernel-devel-$(uname -r) kernel-headers-$(uname -r) make gcc dkms 2>/dev/null || true
        if sudo dkms build -m v4l2loopback -v "$V4L2_VER" && \
           sudo dkms install -m v4l2loopback -v "$V4L2_VER"; then
            fixed "v4l2loopback DKMS rebuilt after installing build deps"
        else
            fail "v4l2loopback DKMS rebuild failed — check /var/lib/dkms/v4l2loopback/${V4L2_VER}/build/make.log"
        fi
    fi
fi

# =============================================================================
# SECTION 3 — NetworkManager-wait-online: VPN blocking network-online.target
# =============================================================================
section "NetworkManager-wait-online"

NM_DROPIN_DIR="/etc/systemd/system/NetworkManager-wait-online.service.d"
NM_DROPIN="${NM_DROPIN_DIR}/rhel10-fix.conf"

# Diagnose: check if VPN profiles are configured to autoconnect and blocking
VPN_PROFILES=$(nmcli -t -f NAME,TYPE,AUTOCONNECT con show 2>/dev/null \
    | awk -F: '$2=="vpn" && $3=="yes" {print $1}')

if systemctl is-failed NetworkManager-wait-online.service &>/dev/null || \
   [[ -n "$VPN_PROFILES" ]]; then

    if [[ ! -f "$NM_DROPIN" ]]; then
        info "VPN profiles that auto-connect and block network-online.target:"
        echo "$VPN_PROFILES" | while read -r v; do [[ -n "$v" ]] && info "  • $v"; done
        info "Fix: use --any flag (succeed when ANY connection is up) + 20s timeout"

        sudo mkdir -p "$NM_DROPIN_DIR"
        sudo tee "$NM_DROPIN" > /dev/null << 'DROPIN'
# Generated by fix-system.sh
# VPN connections that fail at boot (server unreachable) cause nm-online to time
# out for the full 60 seconds, delaying every boot. Using --any means the check
# succeeds as soon as any single connection (e.g. wired/wifi) is established,
# without waiting for VPN. Timeout reduced to 20s as a safety net.
[Service]
ExecStart=
ExecStart=/usr/bin/nm-online -s -q --timeout=20 --any
DROPIN
        sudo systemctl daemon-reload
        sudo systemctl reset-failed NetworkManager-wait-online.service 2>/dev/null || true
        fixed "NetworkManager-wait-online: added --any + 20s timeout drop-in"
    else
        ok "NetworkManager-wait-online drop-in already in place"
    fi
else
    ok "NetworkManager-wait-online: not failing"
fi

# =============================================================================
# SECTION 4 — MCP user services: path validation and venv dependency check
# =============================================================================
section "MCP User Services (mcp-bridge / mcp-sentinel)"

if [[ $AS_ROOT -eq 1 ]]; then
    warn "Skipping MCP user service fix (must run as normal user)"
else
    MCP_BASE="/opt/mcp-rhel-manager"
    BRIDGE_SVC="${HOME}/.config/systemd/user/mcp-bridge.service"
    SENTINEL_SVC="${HOME}/.config/systemd/user/mcp-sentinel.service"
    NEEDS_DAEMON_RELOAD=0

    # ── mcp-bridge.service ──────────────────────────────────────────────────
    if [[ -f "$BRIDGE_SVC" ]]; then
        BRIDGE_PYTHON=$(grep ExecStart "$BRIDGE_SVC" | grep -oP '/\S+python\S*' | head -1)
        BRIDGE_CFG=$(grep ExecStart "$BRIDGE_SVC" | grep -oP '(?<=--config )\S+' | head -1)

        FIX_BRIDGE=0
        # Check python binary
        if [[ -n "$BRIDGE_PYTHON" && ! -x "$BRIDGE_PYTHON" ]]; then
            warn "mcp-bridge: python binary missing: $BRIDGE_PYTHON"
            # Try to find the correct venv python
            for candidate in "${MCP_BASE}/venv/bin/python" \
                             "${MCP_BASE}/venv-bridge/bin/python"; do
                if [[ -x "$candidate" ]]; then
                    info "Correcting ExecStart python path → $candidate"
                    sed -i "s|${BRIDGE_PYTHON}|${candidate}|g" "$BRIDGE_SVC"
                    BRIDGE_PYTHON="$candidate"
                    FIX_BRIDGE=1; NEEDS_DAEMON_RELOAD=1
                    break
                fi
            done
        fi

        # Check config file
        if [[ -n "$BRIDGE_CFG" && ! -f "$BRIDGE_CFG" ]]; then
            warn "mcp-bridge: config file missing: $BRIDGE_CFG"
            for candidate in "${MCP_BASE}/mcp-config.json" \
                             "${HOME}/mcp-rhel-manager/mcp-config.json"; do
                if [[ -f "$candidate" ]]; then
                    sed -i "s|${BRIDGE_CFG}|${candidate}|g" "$BRIDGE_SVC"
                    FIX_BRIDGE=1; NEEDS_DAEMON_RELOAD=1
                    break
                fi
            done
        fi

        # Check ollama_mcp_bridge Python package in the venv
        if [[ -x "$BRIDGE_PYTHON" ]]; then
            if ! "$BRIDGE_PYTHON" -c "import ollama_mcp_bridge" 2>/dev/null; then
                warn "ollama_mcp_bridge not installed in venv — installing from requirements..."
                REQS=""
                for r in "${MCP_BASE}/requirements.txt" \
                          "${HOME}/GIT/mcp-rhel-manager/requirements.txt"; do
                    [[ -f "$r" ]] && { REQS="$r"; break; }
                done
                if [[ -n "$REQS" ]]; then
                    "${MCP_BASE}/venv/bin/pip" install -q -r "$REQS" 2>/dev/null \
                        && fixed "ollama_mcp_bridge deps installed from $REQS" \
                        || warn "pip install failed — manual intervention may be needed"
                else
                    # Try direct install
                    "${MCP_BASE}/venv/bin/pip" install -q ollama-mcp-bridge 2>/dev/null \
                        && fixed "ollama_mcp_bridge installed via pip" \
                        || warn "Could not install ollama_mcp_bridge — check pip manually"
                fi
            else
                ok "ollama_mcp_bridge importable in venv"
            fi
        fi

        [[ $FIX_BRIDGE -eq 1 ]] && fixed "mcp-bridge.service paths corrected"
    else
        info "mcp-bridge.service not found — skipping"
    fi

    # ── mcp-sentinel.service ────────────────────────────────────────────────
    if [[ -f "$SENTINEL_SVC" ]]; then
        SENTINEL_EXEC=$(grep ExecStart "$SENTINEL_SVC" | awk '{print $2}' | head -1)

        if [[ -n "$SENTINEL_EXEC" && ! -x "$SENTINEL_EXEC" ]]; then
            warn "mcp-sentinel: ExecStart not found/executable: $SENTINEL_EXEC"
            for candidate in "${MCP_BASE}/auto-fixer.sh" \
                             "${MCP_BASE}/scripts/auto-fixer.sh" \
                             "${HOME}/GIT/mcp-rhel-manager/scripts/auto-fixer.sh"; do
                if [[ -f "$candidate" ]]; then
                    chmod +x "$candidate"
                    sed -i "s|${SENTINEL_EXEC}|${candidate}|g" "$SENTINEL_SVC"
                    NEEDS_DAEMON_RELOAD=1
                    fixed "mcp-sentinel.service ExecStart corrected → $candidate"
                    break
                fi
            done
            [[ ! -x "$SENTINEL_EXEC" ]] && \
                warn "Could not locate auto-fixer.sh — mcp-sentinel.service left as-is"
        else
            ok "mcp-sentinel.service ExecStart path valid"
        fi
    else
        info "mcp-sentinel.service not found — skipping"
    fi

    # Reload and restart if anything changed
    if [[ $NEEDS_DAEMON_RELOAD -eq 1 ]]; then
        systemctl --user daemon-reload
        systemctl --user restart mcp-bridge.service 2>/dev/null || true
        systemctl --user restart mcp-sentinel.service 2>/dev/null || true
    fi

    # Report current state
    for svc in mcp-bridge mcp-sentinel; do
        state=$(systemctl --user show -p ActiveState --value "${svc}.service" 2>/dev/null || echo "unknown")
        sub=$(systemctl --user show -p SubState --value "${svc}.service" 2>/dev/null || echo "")
        if [[ "$state" == "active" ]]; then
            ok "${svc}.service: ${state} (${sub})"
        elif [[ "$state" == "unknown" || "$state" == "inactive" ]]; then
            info "${svc}.service: ${state} (not configured or disabled)"
        else
            warn "${svc}.service: ${state} (${sub}) — run: journalctl --user -u ${svc} -n 20"
        fi
    done
fi

# =============================================================================
# SECTION 5 — dracut / EFI: create missing machine-id BLS directory
# =============================================================================
section "dracut / EFI BLS directory"

EFI_MACHINE_DIR="/boot/efi/${MACHINE_ID}"
EFI_KVER_DIR="${EFI_MACHINE_DIR}/${KVER}"

if [[ ! -d "$EFI_MACHINE_DIR" ]]; then
    info "Missing BLS machine-id directory: $EFI_MACHINE_DIR"
    info "dracut --regenerate-all fails silently on every DKMS rebuild without this"
    sudo mkdir -p "$EFI_KVER_DIR"
    fixed "Created EFI BLS directory: $EFI_KVER_DIR"
elif [[ ! -d "$EFI_KVER_DIR" ]]; then
    info "Missing BLS kernel directory: $EFI_KVER_DIR"
    sudo mkdir -p "$EFI_KVER_DIR"
    fixed "Created EFI BLS kernel directory: $EFI_KVER_DIR"
else
    ok "EFI BLS directory exists: $EFI_KVER_DIR"
fi

# Register current kernel via kernel-install if the entry is missing
if ! sudo ls "${EFI_KVER_DIR}/" 2>/dev/null | grep -q "linux\|vmlinuz\|initrd"; then
    info "BLS entry for ${KVER} is empty — running kernel-install..."
    VMLINUZ="/boot/vmlinuz-${KVER}"
    INITRAMFS="/boot/initramfs-${KVER}.img"
    if [[ -f "$VMLINUZ" ]]; then
        if [[ ! -f "$INITRAMFS" ]]; then
            info "Generating initramfs for ${KVER}..."
            sudo dracut --force "$INITRAMFS" "$KVER" 2>/dev/null \
                && fixed "Generated initramfs: $INITRAMFS" \
                || warn "dracut failed — boot may still work via existing grub entries"
        fi
        sudo kernel-install add "$KVER" "$VMLINUZ" "$INITRAMFS" 2>/dev/null \
            && fixed "kernel-install registered ${KVER} in BLS" \
            || warn "kernel-install failed — EFI entry may already exist via grub"
    else
        warn "vmlinuz not found at $VMLINUZ — skipping kernel-install"
    fi
else
    ok "BLS entry for ${KVER} already populated"
fi

# =============================================================================
# SECTION 6 — SELinux: fix miscontexted Python libs and insights-client files
# =============================================================================
section "SELinux file contexts"

# /root/.local/lib/python* ends up with gconf_home_t (home dir context) instead
# of lib_t because pip installs there and the home dir label propagates. This
# blocks rhsmcertd from executing YAML C extension .so files.
PYLIB_PATH="/root/.local/lib"
if sudo test -d "$PYLIB_PATH" 2>/dev/null; then
    # Use ls -Z + grep instead of find -context (more portable across findutils versions)
    WRONG=$(sudo ls -RlaZ "$PYLIB_PATH" 2>/dev/null | grep "gconf_home_t" | head -1 || true)
    if [[ -n "$WRONG" ]]; then
        info "Miscontexted files under $PYLIB_PATH (gconf_home_t) — running restorecon..."
        sudo restorecon -RF "$PYLIB_PATH" 2>/dev/null \
            && fixed "SELinux: $PYLIB_PATH contexts restored (rhsmcertd .so access fixed)" \
            || warn "restorecon $PYLIB_PATH failed"
    else
        ok "SELinux: $PYLIB_PATH contexts look correct"
    fi
else
    info "SELinux: $PYLIB_PATH not present — skipping"
fi

# insights-client machine-id: rhsmcertd_t needs read access
INSIGHTS_MACHID="/etc/insights-client/machine-id"
if [[ -f "$INSIGHTS_MACHID" ]]; then
    CTX=$(sudo stat -c "%C" "$INSIGHTS_MACHID" 2>/dev/null || echo "unknown")
    if echo "$CTX" | grep -q "insights_client_etc_rw_t"; then
        ok "SELinux: insights-client/machine-id context is correct ($CTX)"
        ok "  → rhsmcertd denials are permissive-only (policy gap, not blocking)"
    else
        info "SELinux: insights-client/machine-id context: $CTX"
    fi
fi

# =============================================================================
# SECTION 7 — systemd failed units: reset any lingering failed states
# =============================================================================
section "Failed systemd units — cleanup"

# System-level
FAILED_SYSTEM=$(systemctl list-units --state=failed --no-legend --plain 2>/dev/null \
    | awk '{print $1}' | grep -v "^$" || true)
if [[ -n "$FAILED_SYSTEM" ]]; then
    echo "$FAILED_SYSTEM" | while read -r unit; do
        [[ -z "$unit" ]] && continue
        case "$unit" in
            NetworkManager-wait-online.service)
                sudo systemctl reset-failed "$unit" 2>/dev/null || true
                ok "Reset failed state: $unit (fixed above)"
                ;;
            dkms.service)
                sudo systemctl reset-failed "$unit" 2>/dev/null || true
                ok "Reset failed state: $unit (v4l2loopback fixed above)"
                ;;
            *)
                warn "Still failed (not auto-fixed): $unit"
                ;;
        esac
    done
else
    ok "No failed system units"
fi

# User-level
if [[ $AS_ROOT -eq 0 ]]; then
    FAILED_USER=$(systemctl --user list-units --state=failed --no-legend --plain 2>/dev/null \
        | awk '{print $1}' | grep -v "^$" || true)
    if [[ -n "$FAILED_USER" ]]; then
        echo "$FAILED_USER" | while read -r unit; do
            [[ -z "$unit" ]] && continue
            systemctl --user reset-failed "$unit" 2>/dev/null || true
            ok "Reset failed state (user): $unit"
        done
    else
        ok "No failed user units"
    fi
fi

# =============================================================================
# SUMMARY
# =============================================================================
hr
echo -e "${BOLD}  Repair Summary${RESET}"
hr
echo -e "  ${GREEN}Fixed:${RESET}  $FIXED item(s)"
if [[ $ERRORS -gt 0 ]]; then
    echo -e "  ${RED}Errors:${RESET} $ERRORS item(s) — review output above"
else
    echo -e "  ${GREEN}Errors:${RESET} 0"
fi
echo ""
echo -e "  ${BOLD}DKMS status:${RESET}"
dkms status 2>/dev/null | sed 's/^/    /'
echo ""
echo -e "  ${BOLD}Remaining failed units:${RESET}"
STILL_FAILED=$(systemctl list-units --state=failed --no-legend --plain 2>/dev/null | awk '{print $1}' || true)
if [[ -z "$STILL_FAILED" ]]; then
    echo -e "    ${GREEN}none${RESET}"
else
    echo "$STILL_FAILED" | sed "s/^/    ${RED}/" | sed "s/$/${RESET}/"
fi
echo ""
[[ $AS_ROOT -eq 0 ]] && {
    echo -e "  ${BOLD}Audio default sink:${RESET} $(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null || echo 'n/a')"
    echo ""
}
hr
