#!/usr/bin/env bash
set -euo pipefail

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

echo "\n--- ansible-galaxy --version ---"
podman run --rm "$IMG" ansible-galaxy --version || true

echo "\n--- ansible-runner --version ---"
podman run --rm "$IMG" ansible-runner --version || true

echo "\n--- python3 --version ---"
podman run --rm "$IMG" python3 --version || true

echo "\n--- /var/lib/mcp-llms listing ---"
podman run --rm "$IMG" sh -c 'ls -la /var/lib/mcp-llms || echo "(not present or empty)"' || true

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

echo "\nRunning ansible-runner smoke playbook inside container (as root)"
if podman run --rm -u 0 -v "$TMP":/runner:Z "$IMG" ansible-runner run /runner -p playbook.yml; then
  echo "Smoke test: ok"
  rm -rf "$TMP"
  exit 0
else
  echo "Smoke test: failed" >&2
  echo "Temporary project left at: $TMP" >&2
  exit 4
fi
