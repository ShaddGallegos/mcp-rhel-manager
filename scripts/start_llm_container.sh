#!/usr/bin/env bash
set -euo pipefail

# Lightweight helper to run a GPU-enabled LLM container. This script is a
# convenience wrapper — adapt the image, model path, and command to your
# preferred local LLM runtime (LocalAI, Ollama, text-generation-webui, etc.).

: "${LLM_IMAGE:=localai/localai:latest}"
: "${MODEL_PATH:=/path/to/models}"
: "${CONTAINER_NAME:=local-llm}"
: "${LLM_PORT:=11434}"
: "${DRY_RUN:=0}"

echo "LLM image: ${LLM_IMAGE}"
echo "Models path: ${MODEL_PATH}"
echo "Container: ${CONTAINER_NAME}  Port: ${LLM_PORT}"

CMD=(docker run --rm --name "${CONTAINER_NAME}" --gpus all -p "${LLM_PORT}:11434" -v "${MODEL_PATH}:/models" "${LLM_IMAGE}")

# Example additional args for LocalAI or similar runtimes can be appended by
# setting the env var LLM_ARGS, for example:
#   export LLM_ARGS='--models /models --listen 0.0.0.0:11434'
if [ -n "${LLM_ARGS:-}" ]; then
  CMD+=( $LLM_ARGS )
fi

if [ "${DRY_RUN}" = "1" ]; then
  echo "DRY RUN: ${CMD[*]}"
  exit 0
fi

echo "Running: ${CMD[*]}"
exec "${CMD[@]}"
