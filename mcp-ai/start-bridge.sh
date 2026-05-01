#!/bin/bash
# start-bridge.sh - Launch the MCP bridge for HAL

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$PROJECT_ROOT/.venv"

# Check if virtual environment exists
if [[ ! -f "$VENV/bin/python3" ]]; then
    echo "✗ Virtual environment not found at $VENV"
    echo "  Run: cd $PROJECT_ROOT && python3 -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

# Check if Ollama is running
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "✗ Ollama not running on port 11434"
    echo "  Start Ollama with: ollama serve"
    exit 1
fi

# Activate venv and start bridge
source "$VENV/bin/activate"

echo "🚀 Starting MCP Bridge..."
exec python3 "$SCRIPT_DIR/bridge.py" "$@"
