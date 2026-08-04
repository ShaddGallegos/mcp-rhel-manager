Minimal MCP LLM runtime helpers
================================

This small helper package provides lightweight tools to manage host-side
LLM model files under `/var/lib/mcp-llms` so the execution environment images
remain minimal (no baked model weights).

Files:
- `runtime.py` - CLI and library to list, download, and install model archives
  and LoRA adapters into the host model store.

Usage examples:

```
# List available models
python3 mcp-ai/runtime/runtime.py list

# Download and extract a model archive into /var/lib/mcp-llms/<name>
python3 mcp-ai/runtime/runtime.py download <url> <model-name>

# Apply a LoRA archive to a model
python3 mcp-ai/runtime/runtime.py apply-lora <adapter-archive> <model-name>
```

These helpers are intentionally small and shell-friendly; they do not
implement format-specific conversions for every runtime (ollama/llama.cpp).
They provide a safe host-side location and extraction routines that the
container runtime can mount at `/var/lib/mcp-llms`.
