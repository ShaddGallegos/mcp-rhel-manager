# MoE Router (Mixture-of-Experts) — README

This document describes the lightweight MoE router implemented in `mcp-ai/moe_router.py`.

Features

- Rule-based and embedding-based expert selection
- Optional FAISS/Sklearn/brute-force index for fast routing
- Per-expert retries, timeouts, and bounded concurrency
- Aggregator that synthesizes expert outputs
- Optional Prometheus metrics exposure (`MOE_PROMETHEUS=1`)
- Disk-backed profile index cached under `~/.mcp-ai/embeds`

Quick start

1. (Optional) Install dependencies for embeddings and FAISS:

   ```bash
   python -m pip install sentence-transformers scikit-learn prometheus_client || true
   # For FAISS (cpu):
   python -m pip install faiss-cpu || true
   ```

2. Build the profile index (creates `~/.mcp-ai/embeds`):

   ```bash
   python mcp-ai/build_moe_index.py --rebuild
   ```

3. Run unit tests:

   ```bash
   python -m unittest tests/test_moe_router.py -v
   ```

4. Add or remove profiles incrementally:

   ```bash
   python mcp-ai/build_moe_index.py --add-profile business "account company sales revenue"
   python mcp-ai/build_moe_index.py --remove-profile business
   ```

Integration tests

Integration tests are gated and will only run when `MOE_RUN_INTEGRATION=1` is set or in the manual GitHub Action workflow.

Metrics

Set `MOE_PROMETHEUS=1` and optionally `MOE_PROMETHEUS_PORT` to enable an HTTP metrics endpoint (default port 9321).
To push metrics to a pushgateway, set `MOE_PUSHGATEWAY_URL` (push support will use `prometheus_client.push_to_gateway` when available).

Files

- `mcp-ai/moe_router.py` — router implementation
- `mcp-ai/build_moe_index.py` — index build and incremental helpers
- `~/.mcp-ai/embeds` — persisted index and names

Local integration run

To run integration tests locally against a live model bridge (OLLAMA), set `OLLAMA_URL` and run the helper script:

```bash
export OLLAMA_URL='http://localhost:1776/api/chat'
./mcp-ai/run_local_integration.sh
```

The script installs dependencies (best-effort), rebuilds the MoE profile index, and runs the gated integration tests.

Security

The router uses local model bridges and respects environment variables for configuration. Keep machine-local models and bridges secure.
