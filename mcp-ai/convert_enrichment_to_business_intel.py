#!/usr/bin/env python3
"""Convert public company enrichment documents into business_intel_account records.

This script scans ~/.mcp-ai/training for documents of type
`supplemental_document` with `subtype == 'company_public_enrichment'` and creates
`business_intel_account` records using the existing ingestion logic to ensure
consistent formatting and deduplication.

Usage:
  python3 convert_enrichment_to_business_intel.py [--apply]

By default the script runs in dry-run mode and will print what it would do.
Use `--apply` to actually perform imports.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
from pathlib import Path

HOME = os.path.expanduser("~")
TRAIN_DIR = Path(HOME) / ".mcp-ai" / "training"
BASE_DIR = Path(__file__).resolve().parents[1]


def find_enrichment_records() -> list[Path]:
    out = []
    if not TRAIN_DIR.exists():
        return out
    for fp in sorted(TRAIN_DIR.glob("*.json"), reverse=True):
        try:
            rec = json.loads(fp.read_text(encoding='utf-8'))
        except Exception:
            continue
        if rec.get('type') == 'supplemental_document' and rec.get('subtype') == 'company_public_enrichment':
            out.append(fp)
    return out


def existing_business_accounts() -> set[str]:
    s = set()
    if not TRAIN_DIR.exists():
        return s
    for fp in TRAIN_DIR.glob('*.json'):
        try:
            r = json.loads(fp.read_text(encoding='utf-8'))
        except Exception:
            continue
        if r.get('type') == 'business_intel_account':
            name = (r.get('account_name') or '').strip().lower()
            if name:
                s.add(name)
    return s


def convert(fp: Path, ingest_module) -> tuple[bool, str]:
    try:
        rec = json.loads(fp.read_text(encoding='utf-8'))
    except Exception as exc:
        return False, f'load-failed: {exc}'
    company = (rec.get('company') or '').strip()
    if not company:
        return False, 'no-company'

    # Build business_intel payload
    out = {}
    out['account_name'] = company
    mission = (rec.get('mission_statement') or '').strip()
    homepage_desc = (rec.get('homepage_description') or '').strip()
    short = mission or homepage_desc
    if not short:
        text_blob = rec.get('text', '') or ''
        m = re.search(r'## Wikipedia Summary\s*\n(.*?)(?:\nSource:|\Z)', text_blob, re.DOTALL)
        if m:
            short = m.group(1).strip()[:400]
        else:
            short = (text_blob.strip().splitlines()[0] if text_blob else '')[:400]
    out['short_summary'] = short
    if mission:
        out['primary_objective'] = mission
    elif homepage_desc:
        out['primary_objective'] = homepage_desc

    out['text'] = rec.get('text') or ''
    out['notable_news_headlines'] = [h.get('title') if isinstance(h, dict) else h for h in (rec.get('news_headlines') or [])]
    links = rec.get('links') or {}
    srcs = []
    if links.get('official_website'):
        srcs.append(links.get('official_website'))
    for r in rec.get('search_results', [])[:20]:
        if isinstance(r, dict) and r.get('url'):
            if r.get('url') not in srcs:
                srcs.append(r.get('url'))
    out['source_urls'] = srcs

    contacts = []
    for c in rec.get('contact_verification', []) or []:
        name = (c.get('name') or '').strip()
        email = (c.get('email') or '').strip()
        if email:
            contacts.append(f"{name} | {email}" if name else email)
    out['contacts'] = contacts

    # Import
    try:
        status, path_or_err = ingest_module.ingest_record(out, TRAIN_DIR, force=False)
        return (status == 'ok'), path_or_err
    except Exception as exc:
        return False, f'ingest-error: {exc}'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true', help='Actually write business_intel_account records')
    args = p.parse_args()

    enrichments = find_enrichment_records()
    existing = existing_business_accounts()
    print(f'Found {len(enrichments)} enrichment docs; {len(existing)} existing business_intel_account records')

    if not enrichments:
        return

    # Load ingest module
    spec = importlib.util.spec_from_file_location('ingest_business_intel', str(BASE_DIR / 'mcp-ai' / 'ingest_business_intel.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for fp in enrichments:
        try:
            rec = json.loads(fp.read_text(encoding='utf-8'))
        except Exception:
            print(fp.name, '-> load-failed')
            continue
        company = (rec.get('company') or '').strip()
        if not company:
            print(fp.name, '-> no company')
            continue
        if company.strip().lower() in existing:
            print(fp.name, '-> already has business_intel_account, skipping')
            continue

        if args.apply:
            ok, out = convert(fp, mod)
            if ok:
                print(fp.name, '-> imported', out)
                existing.add(company.strip().lower())
            else:
                print(fp.name, '-> failed:', out)
        else:
            print(fp.name, '-> would import for', company)


if __name__ == '__main__':
    main()
