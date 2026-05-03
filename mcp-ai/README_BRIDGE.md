# MCP Bridge — HAL's Connection to the LLM

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

The MCP Bridge is a lightweight HTTP proxy that connects HAL to your local Ollama language model.

## Quick Start

### Prerequisites

- Ollama installed and running (`ollama serve` in a terminal)
- HAL configured (already done if you've cloned this repo)

### Start the Bridge

```bash
cd /home/sgallego/GIT/mcp-rhel-manager
bash mcp-ai/start-bridge.sh
```

The bridge will:

- Check that Ollama is running on port 11434
- Start listening on port 1776
- Display startup logs

### Verify It's Working

```bash
HAL --bridge-check
```

This shows:

- Ollama status and available models
- Bridge connectivity
- Selected model for queries

### Use HAL Normally

```bash
HAL "what needs attention?"
HAL "summarize the migration plan"
HAL --intel-report centene
```

## Architecture

```
HAL (hal.py)
    ↓ HTTP POST /api/chat
MCP Bridge (bridge.py on :1776)
    ↓ HTTP POST /api/chat
Ollama (llama service on :11434)
    ↓ LLM Processing
    ↑ Response
MCP Bridge
    ↑ Response
HAL (displays result)
```

## Features

### Automatic Model Selection

The bridge automatically detects installed Ollama models and selects the best one:

1. Preferred: `qwen2.5-coder:7b` (optimized for code/technical queries)
2. Fallback priority: `llama4:scout`, `llama3.2:3b`, `ansible-*` models, `neural-chat`, `mistral`
3. Final fallback: First available model

If a model becomes unavailable, HAL switches to the next best model.

### Graceful Offline Mode

If the bridge is unavailable, HAL automatically falls back to searching training data:

```bash
HAL "query"
# If bridge is down:
# ERR: Connection refused
# (falls back to searching training data)
# Found in your training data:
# 1. ...
```

### Error Handling

- Connection timeouts: Automatic retry up to 3 times
- Model errors: Auto-select fallback model
- Ollama offline: Shows helpful error with recovery steps

## Installation & Running

### Option 1: Manual Start (Recommended for Development)

```bash
# Terminal 1: Start Ollama
ollama serve

# Terminal 2: Start Bridge
bash mcp-ai/start-bridge.sh

# Terminal 3: Use HAL
HAL "your question here"
```

### Option 2: systemd Service (Recommended for Production)

Create `/etc/systemd/system/hal-bridge.service`:

```ini
[Unit]
Description=HAL MCP Bridge
After=network.target

[Service]
Type=simple
User=sgallego
WorkingDirectory=/home/sgallego/GIT/mcp-rhel-manager
ExecStart=/bin/bash -c 'source .venv/bin/activate && python3 mcp-ai/bridge.py'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start:

```bash
sudo systemctl enable hal-bridge.service
sudo systemctl start hal-bridge.service
sudo systemctl status hal-bridge.service
```

### Option 3: Background tmux/screen Session

```bash
tmux new-session -d -s hal-bridge 'cd /path/to/repo && bash mcp-ai/start-bridge.sh'
tmux list-sessions
tmux kill-session -t hal-bridge
```

## Configuration

### Custom Port

```bash
python3 mcp-ai/bridge.py --port 8888
```

### Custom Ollama Host

```bash
python3 mcp-ai/bridge.py --ollama-url http://192.168.1.100:11434/api/chat
```

### Verbose Logging

```bash
python3 mcp-ai/bridge.py --verbose
```

## Troubleshooting

### "Connection refused" on port 1776

The bridge isn't running. Start it with:

```bash
bash mcp-ai/start-bridge.sh
```

### "Ollama not running" error

Start Ollama in another terminal:

```bash
ollama serve
```

### "Model not found" error

Check available models:

```bash
ollama list
```

Install missing model:

```bash
ollama pull qwen2.5-coder:7b
```

### Bridge crashes or hangs

Check logs:

```bash
python3 mcp-ai/bridge.py --verbose
```

If timeouts occur, increase timeout in bridge.py:

```python
urlopen(req, timeout=600)  # 10 minutes
```

### Slow responses

- Check CPU: `top -p $(pgrep ollama)`
- Check memory: `free -h`
- Check network: `netstat -tuln | grep 1776`
- Consider running on faster hardware

## Performance

- Bridge: <10ms latency per request (overhead)
- Ollama: 5-120 seconds per query (depends on model size and hardware)
- Model sizes:
  - qwen2.5-coder:7b: 4.4 GB (fast, good for technical)
  - llama3.2:3b: 1.9 GB (fastest, small)
  - llama4:scout: 62.8 GB (slowest, most capable)

## Security Considerations

The bridge currently listens on `localhost:1776` only, making it safe for local use.

For remote access, add:

1. Authentication (HTTP Basic Auth)
2. HTTPS with self-signed cert
3. Firewall rules
4. API key requirement

Example with authentication:

```python
from http import HTTPStatus
import base64

def do_POST(self):
    auth_header = self.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        self.send_error(HTTPStatus.UNAUTHORIZED)
        return
    token = auth_header[7:]
    if not verify_token(token):
        self.send_error(HTTPStatus.FORBIDDEN)
        return
    # ... rest of handler
```

## Integration with HAL

HAL automatically:

1. Detects available models via bridge
2. Selects best model based on priority
3. Sends requests with proper JSON format
4. Falls back to training data if bridge unavailable
5. Records all interactions in training data

No additional configuration needed!

## Monitoring

Check bridge health:

```bash
curl -s http://localhost:1776/health | jq .
# {"status": "ok"}

curl -s http://localhost:1776/api/chat -X POST \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-coder:7b","messages":[{"role":"user","content":"hi"}]}' | head -20
```

## Development

Bridge source code: [mcp-ai/bridge.py](bridge.py)

Key components:

- `MCPBridgeHandler`: HTTP request handler
- `do_POST()`: Proxies chat requests to Ollama
- `do_GET()`: Health check endpoint
- `main()`: Server startup and argument parsing

To extend:

- Add `/api/models` endpoint for model management
- Add request/response caching
- Add metrics collection
- Add load balancing to multiple Ollama instances

## Related

- HAL CLI: [hal.py](../hal.py)
- Training Data: [mcp-ai/ingest_documents.py](ingest_documents.py)
- Auto-Ingestion: [mcp-ai/auto_ingest_training.py](auto_ingest_training.py)
- Help: `HAL help bridge`

## License

Same as HAL project.
