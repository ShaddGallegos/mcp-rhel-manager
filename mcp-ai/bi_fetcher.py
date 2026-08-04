#!/usr/bin/env python3
"""Lightweight BI fetcher: extract enrichment info from local training records.

This module intentionally does NOT execute external scrapers by default.
It provides a safe facade to pull structured contact/news/link data from
existing `company_public_enrichment` supplemental documents stored in
`~/.mcp-ai/training` and normalize them for downstream ingestion.

Usage (CLI):
  python3 mcp-ai/bi_fetcher.py --company "Centene" --out /tmp/centene.json

API (programmatic):
  from mcp_ai import bi_fetcher
  rec = bi_fetcher.fetch_company_from_enrichment('Centene')
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

HOME = os.path.expanduser("~")
TRAIN_DIR = Path(HOME) / ".mcp-ai" / "training"


def _norm_company_tokens(company: str) -> list[str]:
    return [t for t in re.findall(r"\b[a-z0-9]+\b", (company or "").lower()) if len(t) > 2]


def find_enrichment_records(company: str) -> list[tuple[Path, dict[str, Any]]]:
    """Return list of (path, record) supplemental enrichment records matching `company`.

    Matching is conservative: requires at least half of token matches (or one).
    """
    out: list[tuple[Path, dict[str, Any]]] = []
    if not TRAIN_DIR.exists() or not company:
        return out
    tokens = _norm_company_tokens(company)
    if not tokens:
        return out

    for fp in sorted(TRAIN_DIR.glob("*.json"), reverse=True):
        try:
            rec = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rec.get("type") != "supplemental_document" or rec.get("subtype") != "company_public_enrichment":
            continue
        company_field = (rec.get("company") or "").lower()
        score = sum(1 for t in tokens if t in company_field)
        if score >= max(1, len(tokens) // 2):
            out.append((fp, rec))
    return out


def fetch_company_from_enrichment(company: str) -> dict[str, Any]:
    """Aggregate contacts, headlines, links and basic metadata from enrichment records.

    Returns a normalized dict with keys: company, contacts, news_headlines, links,
    homepage_description, mission_statement, source_files.
    """
    recs = find_enrichment_records(company)
    result: dict[str, Any] = {
        "company": company,
        "contacts": [],
        "news_headlines": [],
        "links": {},
        "homepage_description": "",
        "mission_statement": "",
        "source_files": [],
    }

    seen_emails: set[str] = set()
    for fp, rec in recs:
        result["source_files"].append(str(fp))

        # Contacts
        for c in rec.get("contact_verification", []) or []:
            try:
                email = (c.get("email") or "").strip()
            except Exception:
                email = ""
            name = (c.get("name") or "").strip()
            if email:
                if email.lower() in seen_emails:
                    continue
                seen_emails.add(email.lower())
                result["contacts"].append({
                    "name": name,
                    "email": email,
                    "verification": c.get("verification", {}),
                    "source": str(fp),
                })

        # Headlines
        for h in rec.get("news_headlines", []) or []:
            if isinstance(h, dict):
                title = h.get("title") or h.get("link") or ""
            else:
                title = str(h or "")
            title = title.strip()
            if title and title not in result["news_headlines"]:
                result["news_headlines"].append(title)

        # Links
        links = rec.get("links") or {}
        for k, v in links.items():
            if not v:
                continue
            if isinstance(v, list):
                result["links"].setdefault(k, []).extend(v)
            else:
                result["links"].setdefault(k, []).append(v)

        # Descriptions
        if rec.get("homepage_description") and not result["homepage_description"]:
            result["homepage_description"] = rec.get("homepage_description") or ""
        if rec.get("mission_statement") and not result["mission_statement"]:
            result["mission_statement"] = rec.get("mission_statement") or ""

    # Deduplicate link lists
    for k, v in list(result["links"].items()):
        result["links"][k] = list(dict.fromkeys(v))

    return result


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Fetch company enrichment from local HAL training records")
    p.add_argument("--company", required=True, help="Company name to look up")
    p.add_argument("--out", help="Write JSON output to this file")
    args = p.parse_args()

    out = fetch_company_from_enrichment(args.company)
    payload = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding='utf-8')
        print("Wrote", args.out)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
