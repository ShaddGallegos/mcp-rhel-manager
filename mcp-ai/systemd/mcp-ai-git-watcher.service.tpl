[Unit]
Description=MCP AI Git Watcher (user service)
After=network.target

[Service]
Type=simple
# WorkingDirectory is set at install-time
WorkingDirectory=@REPO_ROOT@
# Use a venv python if available; installer will replace @VENV_PY@
ExecStart=/bin/bash -lc 'cd "@REPO_ROOT@" && exec "@VENV_PY@" mcp-ai/cli.py git watch --roots "$HOME/GIT" --interval 30'
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
