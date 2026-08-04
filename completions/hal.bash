#!/usr/bin/env bash
# HAL bash tab-completion
# Source this file or install it to /etc/bash_completion.d/hal
# to get tab-completion for the `HAL` (and `hal`) command.
#
# Installed automatically by install_system.sh.

_hal_complete() {
  local cur prev words cword
  _init_completion -n = 2>/dev/null || {
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    words=("${COMP_WORDS[@]}")
    cword=$COMP_CWORD
  }

  # ── flags that expect a directory path ──────────────────────────────────────
  local dir_flags=(
    --bundle-output-dir
    --studio-outdir
  )

  # ── flags that expect a file path (one or more) ─────────────────────────────
  local file_flags=(
    --import-docs
    --import-txt
    --import-business-intel
    --playbook-run
    --watch-log
    --code-review
    --diff-explain
  )

  # ── flags that expect a fixed set of values ──────────────────────────────────
  case "$prev" in
    --alias-add)
      # second word is freeform canonical name — complete nothing specific
      return 0 ;;

    --alias-remove)
      # complete from known alias keys in the alias file
      local alias_file="$HOME/.mcp-ai/account_aliases.json"
      if [[ -f "$alias_file" ]]; then
        local known_aliases
        known_aliases="$(python3 -c "
import json, sys
try:
    d=json.load(open('$alias_file'))
    print(' '.join(d.keys()))
except: pass
" 2>/dev/null)" || true
        [[ -n "$known_aliases" ]] && COMPREPLY=( $(compgen -W "$known_aliases" -- "$cur") ) && return 0
      fi
      return 0 ;;

    --unapprove-image)
      # complete from explicitly approved image names
      local img_file="$HOME/.mcp-ai/approved-podman-images.json"
      if [[ -f "$img_file" ]]; then
        local known_images
        known_images="$(python3 -c "
import json
try:
    d=json.load(open('$img_file'))
    print(' '.join(d.get('images', [])))
except Exception:
    pass
" 2>/dev/null)" || true
        [[ -n "$known_images" ]] && COMPREPLY=( $(compgen -W "$known_images" -- "$cur") ) && return 0
      fi
      return 0 ;;

    --studio-image-backend)
      COMPREPLY=( $(compgen -W "auto diffusers ollama-assisted poster" -- "$cur") )
      return 0 ;;

    --task-profile)
      COMPREPLY=( $(compgen -W "general codegen strategy business diagnostics" -- "$cur") )
      return 0 ;;

    --import-redhat-docs)
      # No curated Red Hat docsets shipped in this repo; complete nothing
      COMPREPLY=( $(compgen -W "" -- "$cur") )
      return 0 ;;

    --set-persona)
      # Try to get live list from HAL; fall back silently if unavailable
      local personas
      personas="$(HAL --personas 2>/dev/null | awk '/^  [a-z]/{print $1}' 2>/dev/null)" || true
      [[ -n "$personas" ]] && COMPREPLY=( $(compgen -W "$personas" -- "$cur") ) && return 0
      return 0 ;;

    --model|--model-pull|--model-delete|--model-info|--benchmark)
      # Try to complete against locally available Ollama models
      local models
      models="$(ollama list 2>/dev/null | awk 'NR>1{print $1}' 2>/dev/null)" || true
      [[ -n "$models" ]] && COMPREPLY=( $(compgen -W "$models" -- "$cur") ) && return 0
      return 0 ;;
  esac

  # ── directory-taking flags ───────────────────────────────────────────────────
  local df
  for df in "${dir_flags[@]}"; do
    if [[ "$prev" == "$df" ]]; then
      _filedir -d
      return 0
    fi
  done

  # ── file-taking flags ────────────────────────────────────────────────────────
  local ff
  for ff in "${file_flags[@]}"; do
    if [[ "$prev" == "$ff" ]]; then
      _filedir
      return 0
    fi
  done

  # For nargs='+' file flags the previous complete word could be a path too.
  # Detect that by checking whether one of those flags appeared earlier.
  local w nplus_active=0
  for ((w=1; w<cword; w++)); do
    local ww="${words[$w]}"
    for ff in "${file_flags[@]}"; do
      if [[ "$ww" == "$ff" ]]; then
        nplus_active=1
        break 2
      fi
    done
  done
  if (( nplus_active )) && [[ "$cur" != --* ]]; then
    _filedir
    return 0
  fi

  # ── depth flags (--1 through --9, alias for --import-url depth) ─────────────
  if [[ "$cur" =~ ^--[1-9]$ ]]; then
    COMPREPLY=( $(compgen -W "--1 --2 --3 --4 --5 --6 --7 --8 --9" -- "$cur") )
    return 0
  fi

  # ── all other completions: list flags ───────────────────────────────────────
  local all_flags=(
    --account
    --alias-add
    --alias-list
    --approve-image
    --approved-images-list
    --alias-remove
    --analytics
    --apply
    --auto-ingest
    --auto-pull
    --benchmark
    --brain-learn
    --brain-route
    --brain-status
    --bridge-check
    --bundle-encrypt
    --bundle-output-dir
    --cache-clear
    --cache-stats
    --chat-export
    --code-review
    --decrypt-training
    --diagnostics
    --diff-explain
    --encrypt-training
    --enrich-companies
    --enrich-from-training
    --ensemble
    --ensemble-models
    --exec
    --explain
    --explain-error
    --export-training-bundle
    --fact
    --feedback
    --find-install
    --generate-readme
    --hf-search
    --import-business-intel
    --import-docs
    --import-redhat-docs
    --import-section
    --import-txt
    --import-url
    --ingest-reset
    --ingest-status
    --insights-checkin
    --insights-status
    --intel-report
    --intel-report-all
    --intel-report-file
    --interactive
    --inventory
    --list-intents
    --max-enrich-companies
    --mcp-delete
    --mcp-list
    --mcp-publish
    --mcp-read
    --mcp-server-start
    --mcp-server-status
    --mcp-server-stop
    --metric-days
    --metrics
    --model
    --model-compare
    --model-delete
    --model-info
    --model-list
    --model-pull
    --motivate
    --news
    --notify
    --notify-slack-test
    --personas
    --pipe-analyze
    --playbook-run
    --predict
    --quiz
    --remediate
    --resource-cleanup
    --resource-install
    --resource-status
    --run-offline-tests
    --run-tests
    --section
    --section-text
    --session-stats
    --set-persona
    --setup-training-maintenance
    --signals-only
    --status
    --studio
    --studio-bpm
    --studio-image-backend
    --studio-outdir
    --studio-pipeline
    --studio-seconds
    --studio-voice
    --suggestions
    --summarize
    --sync-redhat-docs
    --sys-monitor
    --task-plan
    --task-profile
    --todo
    --training-maintenance
    --training-maintenance-apply
    --training-report
    --voice
    --voice-chat
    --voice-name
    --voice-rate
    --watch-log
    --web-search
    --word-of-day
    --unapprove-image
    --1 --2 --3 --4 --5 --6 --7 --8 --9
  )

  COMPREPLY=( $(compgen -W "${all_flags[*]}" -- "$cur") )
  return 0
}

complete -F _hal_complete HAL
complete -F _hal_complete hal
