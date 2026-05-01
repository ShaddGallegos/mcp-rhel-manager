#!/usr/bin/env python3
"""Import Business_Tools account intelligence JSONL data into HAL training.

Reads JSONL records from RH_BusinessIntel.py output and converts them into
structured HAL training records (type: business_intel_account) stored in
~/.mcp-ai/training/ for later search and report generation.

Usage:
    python3 mcp-ai/ingest_business_intel.py /path/to/Training_Data/
    python3 mcp-ai/ingest_business_intel.py file.jsonl another.jsonl
    HAL --import-business-intel /home/sgallego/GIT/Business_Tools/Training_Data/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TRAIN_DIR = Path(os.path.expanduser("~/.mcp-ai/training"))

# Fields considered core account intel (searchable, shown in reports)
INTEL_FIELDS = [
    "account_name",
    "date",
    "short_summary",
    "full_text",
    "text",
    "contacts",
    "tags",
    "redhat_focus_areas",
    "notable_news_headlines",
    "source_urls",
    "primary_objective",
    "use_case_questions",
    "processing_guidance",
    "key_company_attributes",
    "open_source_notes",
    "stock_price_snapshot",
    "territory_owner",
    "territory_name",
    "pod_name",
    "account_executive",
    "account_sa",
    "ticker",
    "weighted_score",
    "activity_score",
    "sales_guidance_level",
    "stack_signals",
    "use_case",
    "rhel_subscription_count",
    "aap_subscription_count",
    "openshift_subscription_count",
]


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def slugify(name: str) -> str:
    """Produce a filesystem-safe slug from an account name."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", (name or "unknown").lower()).strip("-")[:40]


def record_to_text(record: dict) -> str:
    """Build a searchable plaintext representation of an intel record."""
    parts = []
    account = record.get("account_name", "Unknown Account")
    parts.append(f"Account: {account}")

    if record.get("date"):
        parts.append(f"Date: {record['date']}")

    summary = record.get("short_summary") or record.get("full_text") or record.get("text") or ""
    if summary:
        parts.append(f"Summary: {summary[:2000]}")

    if record.get("contacts"):
        contacts = record["contacts"]
        if isinstance(contacts, list):
            parts.append("Contacts: " + ", ".join(str(c) for c in contacts[:30]))
        else:
            parts.append(f"Contacts: {contacts}")

    if record.get("tags"):
        parts.append("Tech Signals: " + ", ".join(str(t) for t in record["tags"]))

    if record.get("redhat_focus_areas"):
        parts.append("Red Hat Focus: " + " | ".join(str(f) for f in record["redhat_focus_areas"]))

    if record.get("notable_news_headlines"):
        headlines = record["notable_news_headlines"]
        if isinstance(headlines, list):
            parts.append("Headlines: " + " | ".join(str(h) for h in headlines[:10]))

    if record.get("primary_objective"):
        parts.append(f"Objective: {record['primary_objective']}")

    if record.get("stack_signals"):
        parts.append("Stack Signals: " + " | ".join(str(s) for s in record["stack_signals"][:10]))

    # Include normalized subscription summary when available.
    sub_parts = []
    if record.get("rhel_subscription_count") is not None:
        sub_parts.append(f"RHEL subscriptions: {record.get('rhel_subscription_count')}")
    if record.get("aap_subscription_count") is not None:
        sub_parts.append(f"AAP subscriptions: {record.get('aap_subscription_count')}")
    if record.get("openshift_subscription_count") is not None:
        sub_parts.append(f"OpenShift subscriptions: {record.get('openshift_subscription_count')}")
    if sub_parts:
        parts.append("Subscription Counts: " + " | ".join(sub_parts))

    return "\n".join(parts)


def _extract_numeric_subscription_count(record: dict, product: str) -> int | None:
    """Best-effort extraction of numeric subscription count for a product.

    Searches both structured keys and unstructured text. Returns int when found,
    otherwise None.
    """
    product = product.lower()
    product_tokens = {
        "rhel": ("rhel", "red hat enterprise linux", "enterprise linux"),
        "aap": ("aap", "ansible automation platform", "ansible"),
        "openshift": ("openshift", "ocp"),
    }[product]

    count_tokens = ("subscription", "subscriptions", "subs", "entitlement", "entitlements", "count", "qty", "quantity")

    # 1) Structured key/value pass first.
    for k, v in record.items():
        lk = str(k).lower()
        if not any(pt in lk for pt in product_tokens):
            continue
        if not any(ct in lk for ct in count_tokens):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            n = int(v)
            if 0 <= n <= 1_000_000:
                return n
        if isinstance(v, str):
            m = re.search(r"\b(\d{1,9})\b", v)
            if m:
                return int(m.group(1))

    # 2) Unstructured text pass.
    blob_parts = [
        str(record.get("short_summary", "")),
        str(record.get("full_text", "")),
        str(record.get("text", "")),
        str(record.get("processing_guidance", "")),
        str(record.get("open_source_notes", "")),
        str(record.get("key_company_attributes", "")),
        json.dumps(record.get("tags", []), ensure_ascii=False),
        json.dumps(record.get("stack_signals", []), ensure_ascii=False),
        json.dumps(record.get("redhat_focus_areas", []), ensure_ascii=False),
    ]
    blob = "\n".join(blob_parts).lower()
    token_pat = "|".join(re.escape(t) for t in product_tokens)

    patterns = [
        rf"(?:{token_pat})[^0-9]{{0,60}}(?:subscriptions?|subs?|entitlements?)[^0-9]{{0,12}}(\d+)",
        rf"(?:subscriptions?|subs?|entitlements?)[^0-9]{{0,12}}(\d+)[^a-z]{{0,25}}(?:{token_pat})",
        rf"(?:{token_pat})[^0-9]{{0,30}}(?:count|qty|quantity)[^0-9]{{0,12}}(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, blob)
        if m:
            n = int(m.group(1))
            if 0 <= n <= 1_000_000:
                return n

    return None


def enrich_subscription_counts(record: dict) -> None:
    """Populate normalized subscription count fields when discoverable."""
    for product, out_key in (
        ("rhel", "rhel_subscription_count"),
        ("aap", "aap_subscription_count"),
        ("openshift", "openshift_subscription_count"),
    ):
        if record.get(out_key) is not None:
            continue
        count = _extract_numeric_subscription_count(record, product)
        if count is not None:
            record[out_key] = count


def ingest_record(record: dict, out_dir: Path, force: bool = False) -> tuple[str, str]:
    """Convert a single business intel record to a HAL training file.

    Returns (status, out_path_or_error): status is 'ok', 'skip', or 'error'.
    """
    account = (record.get("account_name") or "unknown").strip('"').strip()
    if not account:
        return "skip", "no account_name"

    # Normalize structured fields before we generate searchable text/hash.
    enrich_subscription_counts(record)

    ts = utc_ts()
    slug = slugify(account)
    text = record_to_text(record)
    content_hash = sha12(text)
    out_path = out_dir / f"intel-{slug}-{ts}-{content_hash}.json"

    # Deduplication: skip if a file with same hash already exists
    if not force:
        existing = list(out_dir.glob(f"intel-{slug}-*-{content_hash}.json"))
        if existing:
            return "skip", str(existing[0])

    hal_record = {
        "type": "business_intel_account",
        "timestamp": ts,
        "account_name": account,
        "source_file": str(record.get("_source_file", "")),
        "text": text,
    }

    # Preserve all structured intel fields for report generation
    for field in INTEL_FIELDS:
        val = record.get(field)
        if val is not None and val != "" and val != [] and val != {}:
            hal_record[field] = val

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(hal_record, fh, indent=2, ensure_ascii=False)
        return "ok", str(out_path)
    except Exception as exc:
        return "error", str(exc)


def gather_jsonl_files(paths: list[str]) -> list[Path]:
    """Expand paths (files or directories) into a list of .jsonl files."""
    result = []
    for p in paths:
        fp = Path(p)
        if fp.is_file() and fp.suffix in (".jsonl", ".json"):
            result.append(fp)
        elif fp.is_dir():
            result.extend(sorted(fp.rglob("*.jsonl")))
            result.extend(sorted(fp.rglob("*.json")))
        else:
            print(f"  WARN: path not found or unsupported: {p}", file=sys.stderr)
    return result


def ingest_file(jsonl_path: Path, out_dir: Path, force: bool = False) -> tuple[int, int, int]:
    """Ingest all records from a JSONL or JSON file. Returns (ok, skip, error) counts."""
    ok = skip = err = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8") as fh:
            content = fh.read().strip()
    except Exception as exc:
        print(f"  ERR reading {jsonl_path}: {exc}", file=sys.stderr)
        return 0, 0, 1

    records = []
    # Try JSONL (one JSON object per line)
    if "\n" in content:
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    # Fall back to a single JSON array or object
    if not records:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                records = parsed
            elif isinstance(parsed, dict):
                records = [parsed]
        except json.JSONDecodeError as exc:
            print(f"  ERR parsing {jsonl_path}: {exc}", file=sys.stderr)
            return 0, 0, 1

    for record in records:
        if not isinstance(record, dict):
            skip += 1
            continue
        record["_source_file"] = str(jsonl_path)
        status, detail = ingest_record(record, out_dir, force=force)
        if status == "ok":
            ok += 1
            print(f"  OK  {record.get('account_name', '?')!r} → {detail}")
        elif status == "skip":
            skip += 1
        else:
            err += 1
            print(f"  ERR {record.get('account_name', '?')!r}: {detail}", file=sys.stderr)

    return ok, skip, err


def parse_args():
    ap = argparse.ArgumentParser(
        description="Import Business_Tools account intelligence into HAL training data"
    )
    ap.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="JSONL file(s) or directory containing .jsonl files to ingest",
    )
    ap.add_argument(
        "--out-dir",
        default=str(DEFAULT_TRAIN_DIR),
        metavar="DIR",
        help=f"HAL training output directory (default: {DEFAULT_TRAIN_DIR})",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest even if identical record already exists",
    )
    return ap.parse_args()


def main():
    args = parse_args()
    out_dir = Path(args.out_dir)
    files = gather_jsonl_files(args.paths)

    if not files:
        print("No JSONL files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(files)} file(s) to ingest into {out_dir}")

    total_ok = total_skip = total_err = 0
    for f in files:
        print(f"\n→ {f}")
        ok, skip, err = ingest_file(f, out_dir, force=args.force)
        total_ok += ok
        total_skip += skip
        total_err += err

    print(f"\nDone. imported={total_ok} skipped={total_skip} errors={total_err}")
    sys.exit(1 if total_err and not total_ok else 0)


if __name__ == "__main__":
    main()
