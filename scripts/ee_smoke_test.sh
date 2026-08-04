#!/usr/bin/env bash
set -euo pipefail

echo "EE smoke test: check python and ansible imports (host)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2
else
  python3 - <<'PY'
import sys, platform
print('python:', platform.python_version())
if sys.version_info < (3,12):
    print('WARNING: python < 3.12 (required by some builds)')
try:
    import ansible
    print('ansible import OK')
except Exception as e:
    print('ansible import failed:', e)
try:
    import ansible_runner
    print('ansible_runner import OK')
except Exception as e:
    print('ansible_runner import failed:', e)
PY
  if command -v ansible-playbook >/dev/null 2>&1; then
    ansible-playbook --version || true
  fi
fi

usage() {
  cat <<EOF
Usage: $0 [IMAGE]

Run quick smoke tests against an execution-environment image.
If IMAGE is omitted the script selects the most-recent image matching 'mcp-ee'.
EOF
  exit 2
}

IMG="${1:-}"
if [[ -z "$IMG" ]]; then
  IMG=$(podman images --format "{{.Repository}}:{{.Tag}}" | grep mcp-ee | head -n1 || true)
fi
if [[ -z "$IMG" ]]; then
  echo "No mcp-ee image found" >&2
  exit 3
fi

echo "Using image: $IMG"

# Determine host model store to mount into the container. Prefer env MCP_LLMS,
# fall back to /var/lib/mcp-llms, then to the user-local store.
MODEL_DIR=${MCP_LLMS:-/var/lib/mcp-llms}
if [[ ! -d "$MODEL_DIR" && -d "$HOME/.local/share/mcp-llms" ]]; then
  MODEL_DIR="$HOME/.local/share/mcp-llms"
fi
MOUNT_ARGS=()
if [[ -d "$MODEL_DIR" ]]; then
  # Mount read-only by default to avoid modifying host models during tests
  MOUNT_ARGS+=("-v" "$MODEL_DIR:/var/lib/mcp-llms:Z,ro")
  echo "Mounting host model store into container: $MODEL_DIR -> /var/lib/mcp-llms"
else
  echo "Host model store not present; skipping mount" >&2
fi

printf "\n--- ansible-galaxy --version ---\n"
podman run --rm "${MOUNT_ARGS[@]}" "$IMG" ansible-galaxy --version || true

printf "\n--- ansible-runner --version ---\n"
podman run --rm "${MOUNT_ARGS[@]}" "$IMG" ansible-runner --version || true

printf "\n--- python3 --version ---\n"
podman run --rm "${MOUNT_ARGS[@]}" "$IMG" python3 --version || true

printf "\n--- /var/lib/mcp-llms listing ---\n"
podman run --rm "${MOUNT_ARGS[@]}" "$IMG" sh -c 'ls -la /var/lib/mcp-llms || echo "(not present or empty)"' || true

# Run a minimal ansible-runner job as root inside the container to avoid
# host-mount permission issues.
TMP=$(mktemp -d /tmp/ee-smoke-XXXX)
cat > "$TMP/playbook.yml" <<'YML'
- hosts: all
  gather_facts: false
  tasks:
    - debug:
        msg: "hello from ansible-runner smoke test"
YML
mkdir -p "$TMP/inventory"
cat > "$TMP/inventory/hosts" <<'INV'
[all]
localhost ansible_connection=local
INV

printf "\nRunning ansible-runner smoke playbook inside container (as root)\n"
RUN_MOUNTS=("-v" "$TMP:/runner:Z")
if [[ -d "$MODEL_DIR" ]]; then
  RUN_MOUNTS+=("-v" "$MODEL_DIR:/var/lib/mcp-llms:Z,ro")
fi
if podman run --rm -u 0 "${RUN_MOUNTS[@]}" "$IMG" ansible-runner run /runner -p playbook.yml; then
  echo "Smoke test: ok"
  rm -rf "$TMP"
  exit 0
else
  echo "Smoke test: failed" >&2
  echo "Temporary project left at: $TMP" >&2
  exit 4
fi
