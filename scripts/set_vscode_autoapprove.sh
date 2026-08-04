#!/usr/bin/env bash
set -euo pipefail

# Create or update workspace VS Code settings to enable terminal auto-approve
# Writes to .vscode/settings.json in the repo root (preserves existing keys).

WORKSPACE_DIR=".vscode"
SETTINGS_FILE="$WORKSPACE_DIR/settings.json"

mkdir -p "$WORKSPACE_DIR"

python3 - <<'PY'
import json, os
p = os.path.join(os.getcwd(), '.vscode', 'settings.json')
data = {}
if os.path.exists(p):
    try:
        with open(p, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        data = {}

# Set auto-approve for all terminal tool calls using a pattern key
# This writes an object mapping regex paths to boolean approval, e.g. {"/.*/": true}
data['chat.tools.terminal.autoApprove'] = {"/.*/": True}

# Provide sensible defaults if not present (non-destructive)
defaults = {
    'redhat.telemetry.enabled': True,
    'python.analysis.typeCheckingMode': 'standard',
    'git.blame.editorDecoration.enabled': True,
    'security.workspace.trust.untrustedFiles': 'open',
}
for k, v in defaults.items():
    data.setdefault(k, v)

with open(p, 'w', encoding='utf-8') as fh:
    json.dump(data, fh, indent=4, ensure_ascii=False)

print(f'Updated: {p}')
PY

echo "Done. Run 'code .', or re-open the workspace in VS Code to pick up settings."
