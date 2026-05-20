"""Helper wrapper to detect and invoke external GGUF conversion tools.

This module does not implement conversion itself — it looks for a known
converter binary in PATH and invokes it with the dataset path and target
GGUF output path. If no converter is found, callers can skip conversion.
"""
from __future__ import annotations

import shutil
import subprocess
import logging
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("gguf_converter")

# common converter executables we might encounter on user systems
CANDIDATES = [
    "gguf-convert",
    "gguf_convert",
    "convert-gguf",
    "llama_convert",
    "convert_llama_to_gguf",
]


def find_converter() -> Optional[str]:
    """Return the first converter executable found in PATH, or None."""
    for c in CANDIDATES:
        path = shutil.which(c)
        if path:
            LOG.info("Found gguf converter: %s", path)
            return path
    return None


def convert(dataset_path: str | Path, out_path: Optional[str | Path] = None, timeout: int = 3600) -> Optional[Path]:
    """Attempt conversion using an external tool.

    Returns the Path to the generated GGUF file on success, or None on failure.
    """
    dataset = Path(dataset_path)
    if out_path:
        out = Path(out_path)
    else:
        out = dataset.with_suffix('.gguf')

    tool = find_converter()
    if not tool:
        LOG.info("No GGUF converter found in PATH; skipping conversion")
        return None

    try:
        cp = subprocess.run([tool, str(dataset), str(out)], capture_output=True, text=True, timeout=timeout)
        if cp.returncode == 0 and out.exists():
            LOG.info("Conversion completed: %s", out)
            return out
        LOG.warning("Converter returned non-zero or did not produce output: %s", cp.stderr or cp.stdout)
    except Exception as exc:
        LOG.warning("GGUF conversion failed: %s", exc)
    return None


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: gguf_converter.py <dataset.jsonl> [out.gguf]")
        raise SystemExit(2)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else None
    res = convert(src, dst)
    if res:
        print(res)
        raise SystemExit(0)
    raise SystemExit(1)
