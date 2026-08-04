#!/usr/bin/env bash
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -euo pipefail

# Start the inference queue either directly or in a podman container with NVIDIA support
USE_CONTAINER=${USE_CONTAINER:-0}
CONTAINER_IMAGE=${CONTAINER_IMAGE:-mcp-inference-queue:latest}

if [[ "$USE_CONTAINER" =~ ^(1|true|yes)$ ]]; then
    # prefer podman
    if command -v podman >/dev/null 2>&1; then
        # verify nvidia runtime if requested
        if command -v nvidia-smi >/dev/null 2>&1; then
            podman run --rm -p ${MCP_INFERENCE_PORT:-8080}:8080 --env USE_REDIS=${USE_REDIS:-0} --device /dev/nvidiactl --device /dev/nvidia-uvm --device /dev/nvidia0 -v "$REPO_DIR":"/opt/mcp-rhel-manager" "$CONTAINER_IMAGE"
        else
            podman run --rm -p ${MCP_INFERENCE_PORT:-8080}:8080 --env USE_REDIS=${USE_REDIS:-0} -v "$REPO_DIR":"/opt/mcp-rhel-manager" "$CONTAINER_IMAGE"
        fi
    else
        echo "Container requested but podman not found. Falling back to system python."
        python3 "$REPO_DIR/scripts/mcp_inference_queue.py"
    fi
else
    python3 "$REPO_DIR/scripts/mcp_inference_queue.py"
fi
