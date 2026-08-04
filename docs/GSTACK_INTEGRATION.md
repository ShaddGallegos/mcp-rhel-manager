Integration notes: gstack imports

What I added

- `mcp-ai/ingest_common.py`:
  - Added `INJECTION_PATTERNS`, `has_injection()`, `first_injection_match()` for prompt-injection detection.
  - Added `append_jsonl(path, obj)` for atomic single-line JSONL append (POSIX O_APPEND).
  - Added `read_jsonl(path)` tolerant JSONL reader that skips malformed lines.
  - Added `_gstack_redact_executable()` and `call_gstack_redact_json()` to optionally call the bundled `external/gstack/bin/gstack-redact` CLI when `bun` is present.

How to use

- Use `append_jsonl(path, obj)` when you need atomic append semantics for shared JSONL logs.
- Use `read_jsonl(path)` to load JSONL stores robustly.
- `has_injection(text)` helps reject potential prompt-injection content before persisting free-text fields.
- If `bun` is installed and the cloned `external/gstack` is present, `call_gstack_redact_json()` will invoke the gstack redaction engine and return its JSON findings.

Next steps

- Optionally prefer `append_jsonl()` in places that currently write to project-wide JSONL stores (e.g., HAL learnings). I can update those call sites on request.
- Integrate gstack's redaction into `mcp-ai/redact_training.py` to use the richer engine when available.

Notes

- `external/gstack` is cloned under the workspace root; no code was removed from the original. The added helpers are lightweight ports and wrappers to make selected gstack utilities usable from existing Python code.
