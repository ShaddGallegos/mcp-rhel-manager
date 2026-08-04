#!/usr/bin/env bash
set -euo pipefail

if [ -z "${OLLAMA_URL:-}" ]; then
  echo "ERROR: OLLAMA_URL not set. Export OLLAMA_URL and re-run. Example:"
  echo "  export OLLAMA_URL='http://localhost:1776/api/chat'"
  exit 2
fi

echo "Installing (best-effort) Python deps..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || true
python -m pip install faiss-cpu scikit-learn || true

echo "Building MoE profile index..."
python mcp-ai/build_moe_index.py --rebuild

echo "Running gated integration tests against $OLLAMA_URL"
MOE_RUN_INTEGRATION=1 python -m unittest tests/test_moe_integration.py -v
