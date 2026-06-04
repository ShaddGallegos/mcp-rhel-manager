cat << 'EOF' > ~/fix_mcp_bridge.sh
#!/bin/bash

echo "=== Phase 1: Cleaning up directory structure ==="
mkdir -p ~/mcp-rhel-manager ~/.config/systemd/user
cd ~/mcp-rhel-manager

echo "=== Phase 2: Rebuilding virtual environment & dependencies ==="
rm -rf venv-bridge
python3 -m venv venv-bridge
./venv-bridge/bin/pip install --upgrade pip
./venv-bridge/bin/pip install mcp ollama

echo "=== Phase 3: Writing python execution script (bridge.py) ==="
cat << 'PYEOF' > bridge.py
import asyncio
from mcp.server import Server

# Initialize a standard Model Context Protocol server instance
server = Server("ollama-bridge")

async def main():
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
PYEOF

echo "=== Phase 4: Recreating Sentinel bash script ==="
cat << 'BEOF' > auto-fixer.sh
#!/bin/bash
echo "MCP Sentinel Active"
exit 0
BEOF
chmod +x auto-fixer.sh

echo "=== Phase 5: Writing clean Systemd unit files ==="
# Bridge Unit
cat << 'SEOF' > ~/.config/systemd/user/mcp-bridge.service
[Unit]
Description=Ollama MCP Bridge
After=network.target

[Service]
Type=simple
ExecStart=/home/sgallego/mcp-rhel-manager/venv-bridge/bin/python /home/sgallego/mcp-rhel-manager/bridge.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
SEOF

# Sentinel Unit
cat << 'SEOF' > ~/.config/systemd/user/mcp-sentinel.service
[Unit]
Description=MCP Architect Sentinel
After=network.target

[Service]
Type=simple
ExecStart=/home/sgallego/mcp-rhel-manager/auto-fixer.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
SEOF

echo "=== Phase 6: Reloading and activating services ==="
systemctl --user daemon-reload
systemctl --user enable --now mcp-bridge.service mcp-sentinel.service

echo "=== Done! Verifying status... ==="
sleep 1
systemctl --user status mcp-bridge.service mcp-sentinel.service
EOF
chmod +x ~/fix_mcp_bridge.sh && ~/fix_mcp_bridge.sh