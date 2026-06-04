#!/bin/bash

# --- PipeWire Audio Repair Script for RHEL 10 (WSLg-aware) ---
# This script diagnoses and fixes common PipeWire/PulseAudio socket conflicts.
# Run this as your normal user (WITHOUT sudo).

USER_ID=$(id -u)
RUNTIME_DIR="/run/user/${USER_ID}"
LOCK_FILE="${RUNTIME_DIR}/pipewire-0.lock"

# Color helpers
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()  { echo -e "${CYAN}[*]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[!]${RESET} $*"; }
ok()    { echo -e "${GREEN}[✓]${RESET} $*"; }
fail()  { echo -e "${RED}[✗]${RESET} $*"; }
hr()    { echo -e "${BOLD}=============================================${RESET}"; }

# Wait for a user service to reach a target ActiveState, with a timeout
wait_for_service() {
    local svc="$1" want="$2" timeout="${3:-8}" elapsed=0
    while [ $elapsed -lt $timeout ]; do
        state=$(systemctl --user show -p ActiveState --value "$svc" 2>/dev/null)
        [ "$state" = "$want" ] && return 0
        [ "$state" = "failed" ] && return 1
        sleep 1; elapsed=$((elapsed + 1))
    done
    return 1
}

hr
echo -e "${BOLD} Starting PipeWire Audio Repair Engine...   ${RESET}"
hr

# Detect WSLg (Windows Subsystem for Linux GUI)
IS_WSLG=0
if grep -qi microsoft /proc/version 2>/dev/null && [ -d /mnt/wslg ]; then
    IS_WSLG=1
    ok "WSLg environment detected — audio routes through Windows via WSLg."
fi

# Detect dangling WSLg pulse symlink (created by wsl-setup RPM on non-WSL machines)
PULSE_DIR="${RUNTIME_DIR}/pulse"
if [ -L "$PULSE_DIR" ] && [ ! -e "$PULSE_DIR" ]; then
    warn "Dangling pulse symlink: $(readlink "$PULSE_DIR") (target does not exist)"
    # Identify the source so the user knows why it keeps coming back
    if rpm -q wsl-setup &>/dev/null; then
        warn "Root cause: 'wsl-setup' RPM is installed and recreates this symlink at every login"
        warn "         via /usr/share/user-tmpfiles.d/wsl-setup.conf"
        warn "         The boot service ~/.config/systemd/user/fix-pulse-socket.service handles this automatically."
    fi
    info "Removing dangling symlink so pipewire-pulse can create the socket..."
    rm -f "$PULSE_DIR"
fi

if [ "$IS_WSLG" -eq 1 ]; then
    # --- WSLg audio repair path ---
    # WSLg owns the pulse socket. pipewire-pulse must not conflict with it.

    # 1. Stop pipewire-pulse (it conflicts with WSLg's PulseAudio socket)
    info "Disabling conflicting pipewire-pulse (WSLg provides PulseAudio)..."
    systemctl --user stop pipewire-pulse.service pipewire-pulse.socket 2>/dev/null
    systemctl --user mask pipewire-pulse.service pipewire-pulse.socket 2>/dev/null

    # 2. Stop any stale pipewire/wireplumber instances
    info "Clearing dangling audio processes..."
    killall -9 pipewire wireplumber 2>/dev/null
    sleep 1

    # 3. Clear stale PipeWire lockfiles (but NOT the pulse symlink — that belongs to WSLg)
    if [ -f "$LOCK_FILE" ]; then
        info "Clearing stale PipeWire lockfile..."
        rm -f "$LOCK_FILE"
    fi

    # 4. Repair the WSLg pulse symlink if it was accidentally removed
    WSLG_PULSE_DIR="/mnt/wslg/runtime-dir/pulse"
    PULSE_LINK="${RUNTIME_DIR}/pulse"
    if [ ! -L "$PULSE_LINK" ] && [ -d "$WSLG_PULSE_DIR" ]; then
        info "Restoring WSLg pulse symlink..."
        rm -f "$PULSE_LINK"
        ln -s "$WSLG_PULSE_DIR" "$PULSE_LINK"
    elif [ -d "$PULSE_LINK" ] && [ ! -L "$PULSE_LINK" ]; then
        info "Removing stale pulse directory that blocks WSLg symlink..."
        rm -rf "$PULSE_LINK"
        ln -s "$WSLG_PULSE_DIR" "$PULSE_LINK"
    fi

    # 5. Restart PipeWire + WirePlumber
    info "Restarting PipeWire and WirePlumber..."
    systemctl --user daemon-reload
    systemctl --user reset-failed pipewire wireplumber 2>/dev/null
    systemctl --user start pipewire.socket pipewire.service
    systemctl --user start wireplumber.service

    wait_for_service wireplumber.service active 8 \
        && ok "WirePlumber active" \
        || { fail "WirePlumber failed to start — run: journalctl --user -u wireplumber -n 30"; exit 1; }

    # 6. Unmute and set volume
    wpctl set-mute @DEFAULT_AUDIO_SINK@ 0 2>/dev/null
    wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.60 2>/dev/null

    hr
    echo -e "${BOLD} Repair Complete — WSLg Audio Status ${RESET}"
    hr
    echo ""
    echo -e "${BOLD}Audio Sinks:${RESET}"
    wpctl status 2>/dev/null | awk '/^Audio/,/^Video/' | grep -E "Sinks:|^\s+\*|\s+[0-9]+\." | grep -v "Video\|Filters\|Streams\|Sources"
    echo ""
    echo -e "${BOLD}PulseAudio socket:${RESET} $(readlink -f "${RUNTIME_DIR}/pulse" 2>/dev/null || echo "${RED}MISSING — WSLg may not be running${RESET}")"
    echo ""
    echo -e "Test: ${CYAN}paplay /usr/share/sounds/alsa/Front_Center.wav${RESET}"

else
    # --- Native (non-WSL) audio repair path ---

    # 1. Evict legacy PulseAudio if it snuck back in
    if pgrep -x "pulseaudio" > /dev/null; then
        warn "Legacy PulseAudio is running. Evicting..."
        systemctl --user stop pulseaudio.service pulseaudio.socket 2>/dev/null
        systemctl --user mask pulseaudio.service pulseaudio.socket 2>/dev/null
        killall -9 pulseaudio 2>/dev/null
        sleep 1
    fi

    # 2. Kill zombie PipeWire processes
    info "Clearing dangling audio processes..."
    killall -9 pipewire pipewire-pulse wireplumber 2>/dev/null
    sleep 1

    # 3. Remove stale socket path (handles real dirs and dangling symlinks)
    if [ -e "$PULSE_DIR" ] || [ -L "$PULSE_DIR" ]; then
        info "Removing blocked/corrupted socket path: $PULSE_DIR"
        rm -rf "$PULSE_DIR"
    fi

    if [ -f "$LOCK_FILE" ]; then
        info "Clearing stale PipeWire lockfile..."
        rm -f "$LOCK_FILE"
    fi

    # 4. Reset systemd user service states
    info "Resetting systemd user service states..."
    systemctl --user daemon-reload
    systemctl --user reset-failed pipewire pipewire-pulse wireplumber 2>/dev/null

    # 5. Start PipeWire stack in order
    info "Activating PipeWire services..."
    systemctl --user start pipewire.socket pipewire.service
    wait_for_service pipewire.service active 8 \
        && ok "PipeWire active" \
        || { fail "PipeWire failed to start — run: journalctl --user -u pipewire -n 30"; exit 1; }

    systemctl --user start wireplumber.service
    wait_for_service wireplumber.service active 8 \
        && ok "WirePlumber active" \
        || { fail "WirePlumber failed — run: journalctl --user -u wireplumber -n 30"; exit 1; }

    info "Activating PulseAudio compatibility layer..."
    systemctl --user start pipewire-pulse.socket pipewire-pulse.service
    systemctl --user enable pipewire-pulse.service 2>/dev/null
    wait_for_service pipewire-pulse.service active 8 \
        && ok "pipewire-pulse active" \
        || { fail "pipewire-pulse failed — run: journalctl --user -u pipewire-pulse -n 30"; exit 1; }

    # 6. Unmute and set volume
    info "Unmuting default sink and setting volume to 60%..."
    wpctl set-mute @DEFAULT_AUDIO_SINK@ 0 2>/dev/null
    wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.60 2>/dev/null

    hr
    echo -e "${BOLD} Repair Complete — Audio Status ${RESET}"
    hr
    echo ""
    echo -e "${BOLD}Audio Sinks:${RESET}"
    wpctl status 2>/dev/null | awk '/^Audio/,/^Video/' | grep -E "Sinks:|^\s+\*|\s+[0-9]+\." | grep -v "Filters\|Streams\|Sources"
    echo ""
    echo -e "${BOLD}Active PulseAudio clients:${RESET}"
    wpctl status 2>/dev/null | grep -E "pipewire-pulse|Clients:" | head -5
    echo ""
    echo -e "${BOLD}Default sink volume:${RESET} $(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null)"
    echo ""

    # 7. Quick audio test
    TEST_SOUND=""
    for f in /usr/share/sounds/alsa/war_games_play_a_game.wav \
              /usr/share/sounds/freedesktop/stereo/audio-test-signal.oga \
              /usr/share/sounds/Oxygen-Sys-Log-In.ogg; do
        [ -f "$f" ] && { TEST_SOUND="$f"; break; }
    done

    if [ -n "$TEST_SOUND" ]; then
        info "Playing test sound: $TEST_SOUND"
        if paplay "$TEST_SOUND" 2>/dev/null || aplay "$TEST_SOUND" 2>/dev/null; then
            ok "Test sound played — if you heard it, audio is working!"
        else
            warn "Could not play test sound (device may still be initializing)."
            warn "Try manually: paplay $TEST_SOUND"
        fi
    else
        info "No test sound file found. To test manually: speaker-test -t wav -c 2"
    fi
fi
