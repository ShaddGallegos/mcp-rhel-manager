#!/usr/bin/env bash
# Refresh Red Hat Insights access token using refresh token stored in ansible env.yml
# This script emphasizes diagnostics and safe logging; it does NOT print secrets.
set -euo pipefail

HOME_DIR="${HOME:-$(getent passwd $(id -un) | cut -d: -f6)}"
ANSIBLE_ENV_PATH="${ANSIBLE_ENV_PATH:-$HOME_DIR/.ansible/conf/env.yml}"
VAULT_PASS_FILE="${ANSIBLE_VAULT_PASSWORD_FILE:-$HOME_DIR/.ansible/conf/vaultpass.txt}"
LEGACY_VAULT_PASS_FILE="$HOME_DIR/.ansible/conf/.vaultpass.txt"
if [ ! -f "$VAULT_PASS_FILE" ] && [ -f "$LEGACY_VAULT_PASS_FILE" ]; then
  VAULT_PASS_FILE="$LEGACY_VAULT_PASS_FILE"
fi
LOG_DIR="$HOME_DIR/.mcp-ai/logs"
LOG_FILE="$LOG_DIR/refresh_insights_token.log"
DEBUG_MODE=0
MANUAL=0
if [ "${1:-}" = "--debug" ]; then
  DEBUG_MODE=1
elif [ "${1:-}" = "--manual" ]; then
  MANUAL=1
fi
mkdir -p "$LOG_DIR"

timestamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

redact() {
  # replace long token-like strings to avoid leaking secrets in logs
  sed -E 's/("?access_token"?:\s*")[^"]+"/\1[REDACTED]"/g; s/(refresh_token\s*[:=]\s*)[^[:space:]]+/\1[REDACTED]/g'
}

echo "$(timestamp) Refresh run starting" >> "$LOG_FILE"

if [ ! -f "$ANSIBLE_ENV_PATH" ]; then
  echo "$(timestamp) No ansible env file found at $ANSIBLE_ENV_PATH" | tee -a "$LOG_FILE"
  exit 2
fi

refresh_token=""
if head -c 16 "$ANSIBLE_ENV_PATH" 2>/dev/null | grep -q 'ANSIBLE_VAULT'; then
  if [ -n "${VAULT_PASS_FILE:-}" ] && command -v ansible-vault >/dev/null 2>&1; then
    # attempt to view encrypted file (will fail if password incorrect)
    body=$(ansible-vault view "$ANSIBLE_ENV_PATH" --vault-password-file "$VAULT_PASS_FILE" 2>/dev/null || true)
      if [ -n "$body" ]; then
        refresh_token=$(printf '%s' "$body" | python3 "$(dirname "$0")/extract_refresh_token.py")
      fi
  fi
else
  # parse plain YAML to extract redhat.refresh_token
    refresh_token=$(python3 "$(dirname "$0")/extract_refresh_token.py" "$ANSIBLE_ENV_PATH")
fi

if [ -z "$refresh_token" ]; then
  echo "$(timestamp) No refresh_token found in $ANSIBLE_ENV_PATH" | tee -a "$LOG_FILE"
  exit 3
fi

if [ "$MANUAL" -eq 1 ]; then
  cat <<'USAGE'
Manual refresh instructions:

1) Ensure your refresh token is stored in your ansible env file (~/.ansible/conf/env.yml) under one of:
   - redhat.refresh_token
   - RH_INSIGHTS_REFRESH_TOKEN
   - REFRESH_TOKEN

2) To perform a one-off refresh without the script, run (TOKEN via environment):

  REFRESH_TOKEN="<your-refresh-token>" \
  curl -sS -X POST "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode "grant_type=refresh_token" \
    --data-urlencode "refresh_token=${REFRESH_TOKEN}" \
    -w 'HTTP_STATUS:%{http_code}' -o -  

  The response will include an `access_token` (short-lived) and may include
  a rotated `refresh_token`. Do NOT paste tokens in public logs. To persist
  tokens, add them to `~/.ansible/conf/env.yml` and encrypt with `ansible-vault`.

3) To enable extra diagnostic logging, run this script with `--debug` to write
   a redacted full response to ~/.mcp-ai/logs/refresh_insights_token.debug.log

USAGE
  exit 0
fi

# Do not echo the token itself to logs; pass it directly to curl.
endpoint="https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
resp=$(curl -sS -w 'HTTP_STATUS:%{http_code}' -X POST "$endpoint" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=refresh_token" \
  --data-urlencode "refresh_token=$refresh_token" 2>&1 || true)


# Separate body and HTTP code
http_code=$(printf '%s' "$resp" | sed -n 's/.*HTTP_STATUS:\([0-9][0-9][0-9]\)$/\1/p' | tail -n1)
body=$(printf '%s' "$resp" | sed '/HTTP_STATUS:[0-9][0-9][0-9]$/d' )

# Log truncated/redacted body
echo "$(timestamp) HTTP $http_code" >> "$LOG_FILE"
printf '%s' "$body" | head -c 2000 | redact >> "$LOG_FILE" || true

if [ "$DEBUG_MODE" -eq 1 ]; then
  # In debug mode, also log headers and full body (still redacted) to a debug file
  DBG_FILE="$LOG_DIR/refresh_insights_token.debug.log"
  echo "$(timestamp) DEBUG: full response (redacted)" >> "$DBG_FILE"
  printf '%s' "$body" | redact >> "$DBG_FILE" || true
fi

if [ "${http_code:-}" != "200" ]; then
  echo "$(timestamp) Token refresh failed (HTTP ${http_code:-unknown}). See $LOG_FILE for details." | tee -a "$LOG_FILE"
  exit 4
fi

# Optionally parse access_token and update ansible env.yml (disabled by default)
echo "$(timestamp) Token refresh returned HTTP 200. Refresh response logged (redacted)." | tee -a "$LOG_FILE"
echo "$(timestamp) To update env.yml automatically, enable that behavior in your orchestration tool." >> "$LOG_FILE"

exit 0
