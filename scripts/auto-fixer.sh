#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/auto-fixer.log"
MIC_LOG="${SCRIPT_DIR}/auto-fixer-mic.log"
LLM_LOG="${SCRIPT_DIR}/evolution.log"

mkdir -p "${SCRIPT_DIR}"
touch "$LOG_FILE" "$MIC_LOG" "$LLM_LOG"

log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG_FILE"; }
mic_log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$MIC_LOG"; }

cmd_exists() { command -v "$1" >/dev/null 2>&1; }

start_watcher_if_needed() {
  if ! pgrep -f watch_system_and_fix.py >/dev/null 2>&1; then
    if cmd_exists python3; then
      log "Starting watch_system_and_fix.py daemon"
      python3 "${SCRIPT_DIR}/watch_system_and_fix.py" --daemon --check-smart --no-reboot >> "${SCRIPT_DIR}/watch_system_and_fix.log" 2>&1 &
    fi
  fi
}

detect_audio_backend() {
  if cmd_exists pactl; then
    echo pactl
  elif cmd_exists pw-cli || cmd_exists pw-dump; then
    echo pipewire
  elif cmd_exists pulseaudio; then
    echo pulseaudio
  elif cmd_exists arecord; then
    echo alsa
  else
    echo none
  fi
}

get_default_pactl_source() {
  local src
  src=$(pactl info 2>/dev/null | awk -F': ' '/Default Source/{print $2}' | tr -d '\r') || true
  if [ -n "$src" ]; then
    echo "$src"
    return 0
  fi
  # Fallback: choose first available source name
  pactl list short sources 2>/dev/null | awk '{print $2; exit}' || true
}

unmute_and_set_volume() {
  local src="$1"
  mic_log "Ensuring source '$src' is unmuted and at sensible volume"
  if pactl get-source-mute "$src" >/dev/null 2>&1; then
    local muted
    muted=$(pactl get-source-mute "$src" 2>/dev/null | awk -F': ' '{print $2}' | tr -d '\r') || true
    if [ "$muted" = "yes" ] || [ "$muted" = "1" ]; then
      mic_log "Unmuting source $src"
      pactl set-source-mute "$src" 0 || true
    fi
  fi

  # Ensure source volume is reasonable (>=50%)
  if pactl get-source-volume "$src" >/dev/null 2>&1; then
    local vol
    vol=$(pactl get-source-volume "$src" 2>/dev/null | awk 'NR==1{for(i=1;i<=NF;i++){if($i ~ /%/){gsub(/[^0-9]/,"",$i); print $i; exit}}}') || true
    vol=${vol:-0}
    if [ "$vol" -lt 50 ]; then
      mic_log "Raising source $src volume to 75% (was ${vol}%)"
      pactl set-source-volume "$src" 75% || true
    fi
  fi
}

attempt_amixer_fix() {
  if ! cmd_exists amixer; then
    mic_log "amixer not available; skipping ALSA mixer tweaks"
    return 1
  fi
  mic_log "Attempting to unmute/set capture controls via amixer"
  amixer scontrols | sed -nE "s/Simple mixer control '([^']+)'.*/\1/p" | while IFS= read -r ctrl; do
    if echo "$ctrl" | grep -Ei 'mic|capture' >/dev/null 2>&1; then
      mic_log "Setting control '$ctrl' to 80% and unmute"
      amixer sset "$ctrl" 80% unmute >/dev/null 2>&1 || true
    fi
  done
}

restart_audio_services() {
  mic_log "Restarting audio services (user-level)"
  # Try PipeWire stack first
  if systemctl --user status pipewire >/dev/null 2>&1; then
    systemctl --user restart pipewire pipewire-pulse wireplumber >/dev/null 2>&1 || true
    sleep 1
  fi
  # Try PulseAudio fallback
  if systemctl --user status pulseaudio >/dev/null 2>&1; then
    systemctl --user restart pulseaudio >/dev/null 2>&1 || true
  else
    # Kill/resurrect classic pulseaudio if running
    if cmd_exists pulseaudio; then
      pulseaudio -k >/dev/null 2>&1 || true
      pulseaudio --start >/dev/null 2>&1 || true
    fi
  fi
  sleep 1
}

test_microphone_recording() {
  # Returns 0 on success, 1 on failure, 2 if no test tool found
  if cmd_exists arecord; then
    local tmp; tmp="/tmp/af_mic_test_$$.wav"
    mic_log "Recording 3s test via arecord -> $tmp"
    timeout 6 arecord -d 3 -f cd -q "$tmp" >/dev/null 2>&1 || {
      rm -f "$tmp" >/dev/null 2>&1 || true
      return 1
    }
    local size; size=0
    if [ -f "$tmp" ]; then
      size=$(stat -c%s "$tmp" 2>/dev/null || echo 0)
      rm -f "$tmp" >/dev/null 2>&1 || true
    fi
    mic_log "Test recording size: ${size} bytes"
    if [ "$size" -ge 2000 ]; then
      return 0
    else
      return 1
    fi
  fi
  mic_log "No arecord available to test microphone"
  return 2
}

check_and_fix_mic() {
  mic_log "Starting microphone diagnostics"
  local backend; backend=$(detect_audio_backend)
  mic_log "Detected audio backend: ${backend}"

  if [ "$backend" = "none" ]; then
    mic_log "No audio tools found (pactl/arecord); cannot auto-diagnose"
    return 1
  fi

  if [ "$backend" = "pactl" ]; then
    local src
    src=$(get_default_pactl_source || true)
    if [ -z "$src" ]; then
      mic_log "No pactl sources found; listing hardware via arecord -l"
      if cmd_exists arecord; then
        arecord -l 2>&1 | tee -a "$MIC_LOG"
      fi
      restart_audio_services
      src=$(get_default_pactl_source || true)
    fi

    if [ -n "$src" ]; then
      unmute_and_set_volume "$src"
      if test_microphone_recording; then
        mic_log "Microphone recording test succeeded"
        return 0
      else
        mic_log "Initial test failed"
      fi
    fi
  fi

  # Attempt low-level ALSA tweaks
  attempt_amixer_fix || true

  # Restart services and try again
  restart_audio_services

  # Retry detection and fix once more
  if [ "$backend" = "pactl" ]; then
    local src2; src2=$(get_default_pactl_source || true)
    if [ -n "$src2" ]; then
      unmute_and_set_volume "$src2"
      if test_microphone_recording; then
        mic_log "Microphone recording test succeeded after restart"
        return 0
      else
        mic_log "Retry test failed"
      fi
    fi
  else
    if test_microphone_recording; then
      mic_log "Microphone recording test succeeded after restart"
      return 0
    else
      mic_log "Retry test failed"
    fi
  fi

  # Group membership hint
  if ! groups "$(id -un)" | grep -qw audio >/dev/null 2>&1; then
    mic_log "User not in 'audio' group. Consider: sudo usermod -aG audio $(id -un) (requires relogin)."
  fi

  mic_log "Microphone diagnostics completed (issues may remain)"
  return 1
}

start_watcher_if_needed

while true; do
  # Run mic diagnostics before contacting the bridge
  check_and_fix_mic || log "Microphone checks did not resolve all issues"

  # Contact the LLM bridge to perform higher-level maintenance
  curl -s -X POST http://localhost:1776/api/chat -H "Content-Type: application/json" -d '{
    "model": "qwen2.5-coder:7b",
    "messages": [
      {
        "role": "system",
        "content": "You are the Architect Sentinel. 1. Run predict_failure_and_evacuate. 2. Run optimize_ai_performance. 3. Run sentinel_scan. 4. If all good, respond SYSTEM_EVOLVING."
      },
      {"role": "user", "content": "Execute maintenance."}
    ]
  }' >> "$LLM_LOG" 2>&1 || log "LLM bridge call failed"

  # Run hourly
  sleep 3600
done
