# scripts/

This directory contains the canonical shell and Python helper scripts used by mcp-rhel-manager.

Guidelines:
- Keep scripts executable (`chmod +x`) and POSIX/Bash compatible.
- Top-level wrappers in the repo root forward to the canonical `scripts/` versions.
- Use `set -euo pipefail` in bash scripts and `"$@"` for forwarding args.

Run a quick shell lint locally:

```bash
shellcheck -x scripts/*.sh
```
