#!/usr/bin/env bash
set -euo pipefail
PY=python3
ROOT=$(dirname $(dirname "$0"))
# Create simulated entry
$PY "$ROOT/mcp-ai/simulate_failure.py" --name ci-test
# Write plan
$PY "$ROOT/mcp-ai/remediate.py" --latest --plan
# Approve
PLAN=$(ls -1 ~/.mcp-ai/fixes/plan-*.json | tail -n1)
$PY "$ROOT/mcp-ai/approve.py" --plan "$PLAN" --approve --approver ci
# Execute (requires ALLOW_AUTO_FIX=1 and sudoers configured for runner)
ALLOW_AUTO_FIX=1 $PY "$ROOT/mcp-ai/remediate.py" --latest --exec
echo "Simulation run complete"
