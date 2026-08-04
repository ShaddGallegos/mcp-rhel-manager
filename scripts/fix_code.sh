#!/usr/bin/env bash
set -euo pipefail

# fix_code.sh
# Scan a directory tree and apply safe automatic fixes where possible for:
# - Python: ruff (fix), isort, black
# - Shell: shfmt (format), shellcheck (report)
# - YAML/Ansible: yamllint, ansible-lint, ansible-playbook --syntax-check (when applicable)
# Usage: fix_code.sh [--dry-run] [path ...]

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" || "${1:-}" == "-n" ]]; then
  DRY_RUN=1
  shift || true
fi

if [[ $# -eq 0 ]]; then
  PATHS=("$(pwd)")
else
  PATHS=("$@")
fi

LOG_DIRS=(/var/log/mcp-code-fix /tmp/mcp-code-fix "$(pwd)/.mcp-code-fix-logs")
for d in "${LOG_DIRS[@]}"; do
  if mkdir -p "$d" 2>/dev/null; then
    LOGDIR="$d"
    break
  fi
done
LOGDIR=${LOGDIR:-/tmp}
TS=$(date -u +%Y%m%dT%H%M%SZ)
REPORT="$LOGDIR/fix-report-$TS.log"
echo "report: $REPORT"
touch "$REPORT"

run_cmd(){
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY-RUN: $*" | tee -a "$REPORT"
    return 0
  fi
  echo "CMD: $*" >>"$REPORT"
  "$@" >>"$REPORT" 2>&1 || echo "CMD FAILED: $*" >>"$REPORT"
}

has_cmd(){ command -v "$1" >/dev/null 2>&1; }

# script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# LLM suggestion configuration (disabled by default)
ENABLE_LLM=${ENABLE_LLM:-0}
LLM_EXPERT_CODE=${LLM_EXPERT_CODE:-code_fixer}
LLM_EXPERT_SHELL=${LLM_EXPERT_SHELL:-shell_fixer}
LLM_EXPERT_ANSIBLE=${LLM_EXPERT_ANSIBLE:-code_fixer}

run_llm_suggest(){
  local file="$1"
  local expert="$2"
  local outdir="$LOGDIR/suggestions"
  mkdir -p "$outdir"
  local outfile="$outdir/$(basename "$file").${expert}.json"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY-RUN: would run llm_suggest for $file as $expert -> $outfile" >>"$REPORT"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "skip: python3 not found for llm_suggest" >>"$REPORT"
    return 0
  fi
  echo "LLM: running suggestion for $file (expert=$expert)" >>"$REPORT"
  # Allow external config via LLM_EXPERTS_CONFIG env var; llm_suggest loads it
  env LLM_EXPERTS_CONFIG="${LLM_EXPERTS_CONFIG:-}" python3 "$SCRIPT_DIR/llm_suggest.py" --expert "$expert" --file "$file" --out "$outfile" >>"$REPORT" 2>&1 || echo "LLM suggestion failed for $file" >>"$REPORT"
}

format_python(){
  local path="$1"
  echo "--- python: $path" | tee -a "$REPORT"
  if has_cmd ruff; then
    run_cmd ruff "$path" --fix || true
  else
    echo "skip: ruff not installed" >>"$REPORT"
  fi
  if has_cmd isort; then
    run_cmd isort "$path" || true
  else
    echo "skip: isort not installed" >>"$REPORT"
  fi
  if has_cmd black; then
    run_cmd black "$path" || true
  else
    echo "skip: black not installed" >>"$REPORT"
  fi
}

format_shell(){
  local file="$1"
  echo "--- shell: $file" >>"$REPORT"
  if has_cmd shfmt; then
    run_cmd shfmt -w "$file"
  else
    echo "skip: shfmt not installed" >>"$REPORT"
  fi
  if has_cmd shellcheck; then
    run_cmd shellcheck -f gcc "$file" || true
  else
    echo "skip: shellcheck not installed" >>"$REPORT"
  fi
}

lint_yaml(){
  local file="$1"
  echo "--- yaml: $file" >>"$REPORT"
  if has_cmd yamllint; then
    run_cmd yamllint -c default "$file" || true
  else
    echo "skip: yamllint not installed" >>"$REPORT"
  fi
  if has_cmd ansible-lint; then
    # ansible-lint may expect playbooks/collections; run per-file to collect issues
    run_cmd ansible-lint "$file" || true
  else
    echo "skip: ansible-lint not installed" >>"$REPORT"
  fi
  # If it looks like a playbook, try ansible-playbook syntax-check
  if has_cmd ansible-playbook; then
    if grep -qE "(^|\n)\s*-\s*hosts:\s*" "$file" 2>/dev/null || grep -qE "^hosts:\s*" "$file" 2>/dev/null; then
      run_cmd ansible-playbook --syntax-check "$file" || true
    fi
  fi
}

summary_total=0
summary_changed=0

for p in "${PATHS[@]}"; do
  if [[ ! -e "$p" ]]; then
    echo "path not found: $p" | tee -a "$REPORT"
    continue
  fi
  echo "Processing: $p" | tee -a "$REPORT"

  # Python
  mapfile -t pyfiles < <(find "$p" -type f -name '*.py' -not -path '*/.venv/*' -print 2>/dev/null || true)
  if [[ ${#pyfiles[@]} -gt 0 ]]; then
    format_python "$p"
  fi

  # Shell scripts: files ending with .sh or files with a sh/bash shebang
  mapfile -t shfiles < <(find "$p" -type f \( -name '*.sh' -o -name '*.bash' \) -print 2>/dev/null || true)
  # also find files by shebang
  while IFS= read -r -d $'\0' f; do
    shfiles+=("$f")
  done < <(grep -IlR --exclude-dir=.git "^#!.*\(sh\|bash\)" "$p" 2>/dev/null | sed -z 's/\n/\0/g' || true)

  for sf in "${shfiles[@]:-}"; do
    [[ -f "$sf" ]] || continue
    format_shell "$sf"
  done

  # YAML / Ansible
  while IFS= read -r -d $'\0' yf; do
    [[ -f "$yf" ]] || continue
    lint_yaml "$yf"
  done < <(find "$p" -type f \( -name '*.yml' -o -name '*.yaml' \) -print0 2>/dev/null || true)

  # Optional: run LLM suggestions for files (controlled via ENABLE_LLM env var)
  if [[ "${ENABLE_LLM:-0}" -eq 1 ]]; then
    while IFS= read -r -d $'\0' f; do
      [[ -f "$f" ]] || continue
      case "$f" in
        *.py)
          e="$LLM_EXPERT_CODE" ;;
        *.sh|*.bash)
          e="$LLM_EXPERT_SHELL" ;;
        *.yml|*.yaml)
          e="$LLM_EXPERT_ANSIBLE" ;;
        *)
          e="$LLM_EXPERT_CODE" ;;
      esac
      run_llm_suggest "$f" "$e"
    done < <(find "$p" -type f \( -name '*.py' -o -name '*.sh' -o -name '*.bash' -o -name '*.yml' -o -name '*.yaml' \) -print0 2>/dev/null || true)
  fi

  summary_total=$((summary_total+1))
done

echo "Finished. processed directories: $summary_total" | tee -a "$REPORT"
echo "Report saved: $REPORT"

# Optionally create a PR from LLM suggestions if enabled
if [[ "${ENABLE_LLM:-0}" -eq 1 && "${ENABLE_AUTO_PR:-0}" -eq 1 ]]; then
  SUG_DIR="$LOGDIR/suggestions"
  if [[ -d "$SUG_DIR" ]]; then
    # Convert any LLM JSON outputs into .diff patches when possible
    shopt -s nullglob
    for jf in "$SUG_DIR"/*.json; do
      # extract 'output' field into a .diff file
      outp="${jf%.json}.diff"
      python3 - <<PY > "$outp" 2>/dev/null || true
import json,sys
try:
    j=json.load(open(sys.argv[1]))
    print(j.get('output',''))
except Exception:
    pass
PY
      # if file doesn't look like a diff, remove it
      if ! grep -qE "(^diff --git|^--- |^\+\+\+ )" "$outp" 2>/dev/null; then
        rm -f "$outp"
      fi
    done

    # If diffs exist, prefer to apply patches and create PRs
    mapfile -t DIFFS < <(find "$SUG_DIR" -type f -name '*.diff' -print 2>/dev/null || true)
    if [[ ${#DIFFS[@]} -gt 0 ]]; then
      if [[ -x "$(pwd)/scripts/apply_patches_and_create_pr.sh" || -f "$(pwd)/scripts/apply_patches_and_create_pr.sh" ]]; then
        echo "Applying patches and creating PR from LLM suggestions..."
        bash "$(pwd)/scripts/apply_patches_and_create_pr.sh" --repo "$(pwd)" --patch-dir "$SUG_DIR" --create-pr || true
      else
        echo "apply_patches_and_create_pr.sh not found; skipping patch apply"
      fi
    else
      if [[ -x "$(pwd)/scripts/create_pr_from_suggestions.sh" || -f "$(pwd)/scripts/create_pr_from_suggestions.sh" ]]; then
        echo "Creating PR from LLM suggestions..."
        bash "$(pwd)/scripts/create_pr_from_suggestions.sh" --repo "$(pwd)" --src-dir "$SUG_DIR" --create-pr || true
      else
        echo "create_pr_from_suggestions.sh not found; skipping PR creation"
      fi
    fi
    shopt -u nullglob
  fi
fi

exit 0
