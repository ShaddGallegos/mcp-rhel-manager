#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUTDIR="${SCRIPT_DIR}/mic-report-${TS}"
mkdir -p "$OUTDIR"

log() { printf '%s %s\n' "$(date -Is)" "$*"; }

log "Collecting microphone diagnostics into $OUTDIR"

# Basic tool outputs
command -v pactl >/dev/null 2>&1 && pactl info >"${OUTDIR}/pactl-info.txt" 2>&1 || true
command -v pactl >/dev/null 2>&1 && pactl list short sources >"${OUTDIR}/pactl-sources-short.txt" 2>&1 || true
command -v pactl >/dev/null 2>&1 && pactl list sources >"${OUTDIR}/pactl-sources.txt" 2>&1 || true
command -v pw-cli >/dev/null 2>&1 && pw-cli info >"${OUTDIR}/pw-cli-info.txt" 2>&1 || true
command -v pw-dump >/dev/null 2>&1 && pw-dump >"${OUTDIR}/pw-dump.json" 2>&1 || true
command -v arecord >/dev/null 2>&1 && arecord -l >"${OUTDIR}/arecord-l.txt" 2>&1 || true
command -v arecord >/dev/null 2>&1 && arecord -L >"${OUTDIR}/arecord-L.txt" 2>&1 || true
command -v amixer >/dev/null 2>&1 && amixer scontrols >"${OUTDIR}/amixer-scontrols.txt" 2>&1 || true
command -v amixer >/dev/null 2>&1 && amixer scontents >"${OUTDIR}/amixer-scontents.txt" 2>&1 || true

# Capture system logs related to pulseaudio/pipewire/alsa
journalctl --no-pager -n 200 -u pulseaudio.service 2>/dev/null >"${OUTDIR}/journal_pulseaudio.txt" || true
journalctl --no-pager -n 200 -u pipewire.service 2>/dev/null >"${OUTDIR}/journal_pipewire.txt" || true
journalctl --no-pager -n 200 -t pulseaudio 2>/dev/null >"${OUTDIR}/journal_tag_pulseaudio.txt" || true
journalctl --no-pager -n 200 -t pipewire 2>/dev/null >"${OUTDIR}/journal_tag_pipewire.txt" || true

# Attempt a short test recording if available
if command -v arecord >/dev/null 2>&1; then
  WAV="${OUTDIR}/mic_test.wav"
  log "Attempting 3s arecord test -> ${WAV}"
  timeout 6 arecord -d 3 -f cd -q "$WAV" || true
  if [ -f "$WAV" ]; then
    stat -c "%n %s bytes" "$WAV" >"${OUTDIR}/mic_test_size.txt" || true
  fi
else
  log "arecord not available; skipping test recording"
fi

tar -czf "${SCRIPT_DIR}/mic-report-${TS}.tar.gz" -C "$SCRIPT_DIR" "mic-report-${TS}"
log "Packaged mic report: ${SCRIPT_DIR}/mic-report-${TS}.tar.gz"

echo "${SCRIPT_DIR}/mic-report-${TS}.tar.gz"
