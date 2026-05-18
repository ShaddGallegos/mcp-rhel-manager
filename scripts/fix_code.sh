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

  summary_total=$((summary_total+1))
done

echo "Finished. processed directories: $summary_total" | tee -a "$REPORT"
echo "Report saved: $REPORT"

exit 0
