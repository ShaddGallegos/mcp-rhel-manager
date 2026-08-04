#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: scripts/repair_http_stack.sh [--python /path/to/python] [--dry-run]

Reinstalls a compatible HTTP client stack for HAL:
- requests>=2.32,<3
- urllib3>=2,<3
- chardet>=5,<6
- charset-normalizer>=3.3,<4

Examples:
  ./scripts/repair_http_stack.sh
  ./scripts/repair_http_stack.sh --python .venv/bin/python
  ./scripts/repair_http_stack.sh --dry-run
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

CMD=(
  "$PYTHON_BIN" -m pip install --upgrade --force-reinstall
  "requests>=2.32.0,<3.0.0"
  "urllib3>=2.0.0,<3.0.0"
  "chardet>=5.0.0,<6.0.0"
  "charset-normalizer>=3.3.0,<4.0.0"
)

if [[ "$DRY_RUN" == true ]]; then
  printf 'Dry run:'
  printf ' %q' "${CMD[@]}"
  printf '\n'
  exit 0
fi

printf 'Running:'
printf ' %q' "${CMD[@]}"
printf '\n'
"${CMD[@]}"

echo
"$PYTHON_BIN" - <<'PY'
import requests, urllib3, chardet
try:
    import charset_normalizer
    cn = getattr(charset_normalizer, '__version__', 'unknown')
except Exception as exc:
    cn = f'BROKEN: {exc}'
print('requests:', requests.__version__)
print('urllib3:', urllib3.__version__)
print('chardet:', chardet.__version__)
print('charset_normalizer:', cn)
PY
