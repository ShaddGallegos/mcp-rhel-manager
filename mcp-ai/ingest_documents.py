#!/usr/bin/env python3
"""Ingest local documents into HAL supplemental training data.

Supported types:
- Plain text: .txt, .md, .rst, .log, .ini, .cfg, .conf, .yaml, .yml, .json, .jsonl, .xml, .html, .htm
- Delimited data: .csv, .tsv
- Spreadsheets: .xlsx, .xls, .ods (requires pandas + compatible engine)
- Docs (best effort): .pdf (requires pypdf), .docx (requires python-docx), .doc (antiword/catdoc/libreoffice)

Each ingested source file becomes one JSON file in ~/.mcp-ai/training.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


HOME = os.path.expanduser("~")
TRAIN_DIR = os.path.join(HOME, ".mcp-ai", "training")

TEXT_EXTS = {
    ".txt", ".md", ".rst", ".log", ".ini", ".cfg", ".conf", ".yaml", ".yml",
    ".json", ".jsonl", ".xml", ".html", ".htm",
}
DELIMITED_EXTS = {".csv", ".tsv"}
SPREADSHEET_EXTS = {".xlsx", ".xls", ".ods"}
DOC_EXTS = {".pdf", ".docx", ".doc"}
DEFAULT_EXTS = TEXT_EXTS | DELIMITED_EXTS | SPREADSHEET_EXTS | DOC_EXTS


@dataclass
class IngestResult:
    source: str
    out_file: str | None
    status: str
    message: str = ""


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def read_text_file(path: Path, max_chars: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        # Last fallback for odd encodings.
        data = path.read_bytes()
        return data.decode("latin-1", errors="replace")[:max_chars]


def read_delimited(path: Path, delimiter: str, max_rows: int, max_chars: int) -> str:
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append(row)
    text = "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
    return text[:max_chars]


def read_spreadsheet(path: Path, max_rows: int, max_chars: int) -> str:
    ext = path.suffix.lower()

    # Prefer openpyxl for xlsx/xlsm to avoid pandas/numpy runtime coupling.
    if ext in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook
        except Exception as exc:
            raise RuntimeError(f"openpyxl is required for {ext} import: {exc}") from exc

        try:
            wb = load_workbook(filename=str(path), read_only=True, data_only=True)
            chunks: list[str] = []
            for ws in wb.worksheets:
                rows: list[str] = []
                for idx, row in enumerate(ws.iter_rows(values_only=True)):
                    if idx >= max_rows:
                        break
                    cells = ["" if c is None else str(c).strip() for c in row]
                    rows.append(" | ".join(cells).rstrip())
                chunks.append(f"# Sheet: {ws.title}\n" + "\n".join(rows))
            return "\n".join(chunks)[:max_chars]
        except Exception as exc:
            raise RuntimeError(f"failed to parse spreadsheet {path}: {exc}") from exc

    # Fallback for xls/ods via pandas when available.
    try:
        import pandas as pd
    except Exception as exc:
        raise RuntimeError(f"pandas is required for spreadsheet import: {exc}") from exc

    try:
        sheets = pd.read_excel(path, sheet_name=None)
    except Exception as exc:
        raise RuntimeError(f"failed to parse spreadsheet {path}: {exc}") from exc

    chunks: list[str] = []
    for sheet_name, frame in sheets.items():
        limited = frame.head(max_rows)
        chunk = f"# Sheet: {sheet_name}\n{limited.to_csv(index=False)}"
        chunks.append(chunk)
    combined = "\n".join(chunks)
    return combined[:max_chars]


def read_pdf(path: Path, max_chars: int) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise RuntimeError(f"pypdf is required for PDF import: {exc}") from exc

    try:
        reader = PdfReader(str(path))
        text_parts = []
        for page in reader.pages:
            text_parts.append(page.extract_text() or "")
        return "\n".join(text_parts)[:max_chars]
    except Exception as exc:
        raise RuntimeError(f"failed to parse pdf {path}: {exc}") from exc


def read_docx(path: Path, max_chars: int) -> str:
    try:
        from docx import Document
    except Exception as exc:
        raise RuntimeError(f"python-docx is required for DOCX import: {exc}") from exc

    try:
        doc = Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs)
        return text[:max_chars]
    except Exception as exc:
        raise RuntimeError(f"failed to parse docx {path}: {exc}") from exc


def _run_text_extractor(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
        if proc.returncode == 0:
            out = (proc.stdout or "").strip()
            return out or None
    except Exception:
        pass
    return None


def read_doc(path: Path, max_chars: int) -> str:
    # Prefer dedicated legacy .doc extractors when available.
    if shutil.which("antiword"):
        text = _run_text_extractor(["antiword", str(path)])
        if text:
            return text[:max_chars]

    if shutil.which("catdoc"):
        text = _run_text_extractor(["catdoc", str(path)])
        if text:
            return text[:max_chars]

    # Fallback: convert .doc to .txt with LibreOffice headless.
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        with tempfile.TemporaryDirectory(prefix="hal-doc-") as td:
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", "txt:Text", "--outdir", td, str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            if proc.returncode == 0:
                txt_path = Path(td) / f"{path.stem}.txt"
                if txt_path.exists():
                    return txt_path.read_text(encoding="utf-8", errors="replace")[:max_chars]

    raise RuntimeError(
        "failed to parse .doc file; install one of: antiword, catdoc, or libreoffice"
    )


def gather_files(paths: Iterable[str], recursive: bool, allowed_exts: set[str]) -> list[Path]:
    resolved: list[Path] = []
    for p in paths:
        raw = Path(os.path.expanduser(p)).resolve()
        if not raw.exists():
            continue
        if raw.is_file():
            if raw.suffix.lower() in allowed_exts:
                resolved.append(raw)
            continue
        if raw.is_dir():
            iterator = raw.rglob("*") if recursive else raw.glob("*")
            for child in iterator:
                if child.is_file() and child.suffix.lower() in allowed_exts:
                    resolved.append(child)
    # Stable dedupe while preserving order.
    deduped: list[Path] = []
    seen: set[str] = set()
    for f in resolved:
        key = str(f)
        if key not in seen:
            deduped.append(f)
            seen.add(key)
    return deduped


def ingest_file(path: Path, outdir: Path, max_chars: int, max_rows: int) -> IngestResult:
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            content = read_text_file(path, max_chars=max_chars)
            parser = "text"
        elif ext in DELIMITED_EXTS:
            delim = "\t" if ext == ".tsv" else ","
            content = read_delimited(path, delimiter=delim, max_rows=max_rows, max_chars=max_chars)
            parser = "delimited"
        elif ext in SPREADSHEET_EXTS:
            content = read_spreadsheet(path, max_rows=max_rows, max_chars=max_chars)
            parser = "spreadsheet"
        elif ext == ".pdf":
            content = read_pdf(path, max_chars=max_chars)
            parser = "pdf"
        elif ext == ".docx":
            content = read_docx(path, max_chars=max_chars)
            parser = "docx"
        elif ext == ".doc":
            content = read_doc(path, max_chars=max_chars)
            parser = "doc"
        else:
            return IngestResult(str(path), None, "skipped", f"unsupported extension {ext}")

        if not content.strip():
            return IngestResult(str(path), None, "skipped", "empty extracted content")

        ts = utc_ts()
        source = str(path)
        out_name = f"doc-{ts}-{sha12(source)}.json"
        out_file = outdir / out_name
        rec = {
            "type": "supplemental_document",
            "timestamp": ts,
            "source_path": source,
            "source_name": path.name,
            "source_ext": ext,
            "parser": parser,
            "content_length": len(content),
            "text": content,
        }
        out_file.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        return IngestResult(source, str(out_file), "ok")
    except Exception as exc:
        return IngestResult(str(path), None, "error", str(exc))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest local documents into HAL training data")
    p.add_argument("inputs", nargs="+", help="Files or directories to ingest")
    p.add_argument("--outdir", default=TRAIN_DIR, help="Output training directory")
    p.add_argument("--recursive", action="store_true", help="Recurse into directory inputs")
    p.add_argument("--max-chars", type=int, default=120000, help="Max extracted characters per file")
    p.add_argument("--max-rows", type=int, default=500, help="Max rows per delimited/spreadsheet source")
    p.add_argument(
        "--ext",
        action="append",
        default=[],
        help="Limit to specific extension(s), e.g. --ext .csv --ext .txt",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    outdir = Path(os.path.expanduser(args.outdir)).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    if args.ext:
        allowed_exts = {e if e.startswith(".") else f".{e}" for e in args.ext}
        allowed_exts = {e.lower() for e in allowed_exts}
    else:
        allowed_exts = set(DEFAULT_EXTS)

    files = gather_files(args.inputs, recursive=args.recursive, allowed_exts=allowed_exts)
    if not files:
        print("No matching files found to ingest.", file=sys.stderr)
        return 2

    print(f"Found {len(files)} file(s) to ingest")
    ok = 0
    skipped = 0
    errors = 0
    for f in files:
        result = ingest_file(f, outdir=outdir, max_chars=args.max_chars, max_rows=args.max_rows)
        if result.status == "ok":
            ok += 1
            print(f"OK: {result.source} -> {result.out_file}")
        elif result.status == "skipped":
            skipped += 1
            print(f"SKIP: {result.source} ({result.message})")
        else:
            errors += 1
            print(f"ERR: {result.source} ({result.message})", file=sys.stderr)

    print(f"Done. imported={ok} skipped={skipped} errors={errors}")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
