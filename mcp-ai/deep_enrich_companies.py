#!/usr/bin/env python3
"""Deep enrichment: tuned heuristics for partner/vendor extraction and procurement contacts.

Usage:
  python3 mcp-ai/deep_enrich_companies.py Centene

This is best-effort and polite (small sleeps). It reads existing public enrichment
via bi_fetcher when available, then follows likely partner/press pages, LinkedIn
company pages, and other candidate URLs to extract vendor names and procurement
contact emails with context. Results are written as a `supplemental_document`
record into the same training directory used by `mcp-ai/enrich_companies.py`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit


def load_module_from_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def normalize_vendor_name(n: str) -> str:
    return re.sub(r"\s+", " ", (n or "").strip())


def run_deep_enrich(company: str, max_pages: int = 40, timeout: int = 12) -> Path | None:
    base_dir = Path(__file__).resolve().parent
    ec = load_module_from_path(base_dir / "enrich_companies.py", "enrich_companies")
    bf = load_module_from_path(base_dir / "bi_fetcher.py", "bi_fetcher")

    # Try to fetch existing supplemental enrichment
    ext = None
    try:
        ext = bf.fetch_company_from_enrichment(company)
    except Exception:
        ext = None

    # If no enrichment exists, trigger a light enrichment run
    if not ext:
        try:
            ec.enrich_one_company(company)
            ext = bf.fetch_company_from_enrichment(company)
        except Exception:
            ext = None

    links = (ext.get("links") if ext else {}) or {}
    search_results = (ext.get("search_results") if ext else []) or []

    candidates = []
    # Add known profile links
    for k, v in links.items():
        if not v:
            continue
        if isinstance(v, list):
            for u in v:
                candidates.append(u)
        else:
            candidates.append(v)

    # Add search result urls
    for r in search_results:
        u = r.get("url")
        if u:
            candidates.append(u)

    # Derive likely partner/press paths from official site
    official = links.get("official_website") or ""
    root_netloc = ""
    if official:
        try:
            s = urlsplit(official)
            root = f"{s.scheme}://{s.netloc}"
            root_netloc = s.netloc
            suffixes = [
                "news",
                "newsroom",
                "press",
                "press-releases",
                "media",
                "about-us/partners",
                "partners",
                "partner-directory",
                "partner-program",
                "investors/news",
                "partnerships",
                "alliances",
                "solutions/partners",
                "vendor",
                "suppliers",
                "procurement",
                "supplier-registration",
            ]
            for sfx in suffixes:
                candidates.append(urljoin(root, sfx))
        except Exception:
            pass

    # De-duplicate and limit
    seen = set()
    filtered = []
    for u in candidates:
        if not u or not str(u).lower().startswith("http"):
            continue
        if u in seen:
            continue
        seen.add(u)
        filtered.append(u)
        if len(filtered) >= max_pages:
            break

    anchor_pat = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{1,300})</a>', re.I)
    partner_section_pat = re.compile(r'(?s)<h[1-6][^>]*>[^<]{0,120}partners?[^<]*</h[1-6]>.*?(<ul.*?>.*?</ul>|<div[^>]*>.*?</div>)', re.I)

    vendors = {}
    proc_contacts = []
    links_checked = []

    stopwords = {"help center", "terms of service", "privacy policy", "cookie policy", "imprint", "ads info", "contact", "contact us", "about us", "careers", "blog", "search", "login", "signup", "subscribe"}

    for url in filtered:
        try:
            body = ec.http_get(url, timeout=timeout)
        except Exception:
            continue
        links_checked.append(url)

        # find partner section snippets
        partner_snips = []
        m = partner_section_pat.search(body)
        if m:
            partner_snips.append(m.group(1))

        # anchors
        for m in anchor_pat.finditer(body):
            href = m.group(1).strip()
            at = re.sub(r"\s+", " ", m.group(2).strip())
            if not at:
                continue
            if len(at) < 2 or len(at) > 120:
                continue
            # skip obvious nav links
            if at.lower() in stopwords:
                continue
            # normalize href
            try:
                full = urljoin(url, href)
            except Exception:
                full = href
            # skip javascript and anchors
            if full.startswith("javascript:") or full.startswith("#"):
                continue

            # heuristics: prefer external links (not same domain as official)
            try:
                net = urlsplit(full).netloc
            except Exception:
                net = ""

            accept = False
            if root_netloc and net and net != root_netloc:
                # skip social links
                if any(social in net for social in ("twitter.com", "x.com", "facebook.com", "linkedin.com", "youtube.com", "instagram.com")):
                    # still collect if anchor text looks like company
                    if re.search(r'[A-Z][a-z]+\s+[A-Z][a-z]+', at) or len(at.split()) <= 4:
                        accept = True
                else:
                    accept = True
            else:
                # if anchor appears inside a partner section or nearby 'partner' keyword
                ctx = body[max(0, m.start()-200):m.end()+200].lower()
                if 'partner' in ctx or 'partner' in url.lower():
                    accept = True

            if not accept:
                continue

            name = normalize_vendor_name(at)
            if not name or len(name) < 2:
                continue
            if re.search(r'click here|read more|learn more|more', name, flags=re.I):
                continue
            vendors.setdefault(name, []).append({'source': full, 'snippet': body[m.start():m.start()+300]})

        # partner snippets anchors
        for snip in partner_snips:
            for m in anchor_pat.finditer(snip):
                at = re.sub(r"\s+", " ", m.group(2).strip())
                if not at:
                    continue
                name = normalize_vendor_name(at)
                if len(name) < 2:
                    continue
                vendors.setdefault(name, []).append({'source': url, 'snippet': snip[:300]})

        # procurement-specific detection: look for procurement-related pages or keywords
        if re.search(r'procure|procurement|vendor|supplier|purchas|sourcing', url.lower()) or re.search(r'procure|procurement|vendor|supplier|purchas|sourcing', body.lower()):
            emails = set(ec.EMAIL_RE.findall(body))
            for e in emails:
                pos = body.find(e)
                window = body[max(0, pos-200):pos+200]
                nm = re.search(r'([A-Z][a-z]+\s+[A-Z][a-z]+)', window)
                name = nm.group(0) if nm else ''
                title = ''
                mtitle = re.search(r'([A-Za-z ,]{1,80}(?:Procurement|Purchasing|Sourcing|Vendor|Supplier|Vendor Management|Supplier Management)[A-Za-z ,]*)', window)
                if mtitle:
                    title = mtitle.group(0).strip()
                proc_contacts.append({'name': name, 'email': e, 'title': title, 'source': url, 'confidence': 'high' if 'procure' in window.lower() else 'medium'})

        # small polite pause
        time.sleep(0.35)

    # compact and sort vendors by evidence
    clean_vendors = []
    for name, evs in vendors.items():
        if not name or len(name) < 2:
            continue
        clean_vendors.append({'vendor': name, 'evidence': evs})

    clean_vendors.sort(key=lambda x: len(x.get('evidence', [])), reverse=True)

    # deduplicate procurement contacts by email
    seen_em = set()
    dedup_proc = []
    for p in proc_contacts:
        em = (p.get('email') or '').lower()
        if not em:
            continue
        if em in seen_em:
            continue
        seen_em.add(em)
        dedup_proc.append(p)

    # compose record
    text_lines = [f"Deep enrichment for {company}", '', 'Vendors/partners found:']
    for v in clean_vendors[:200]:
        text_lines.append(f"- {v['vendor']}  (evidence: {', '.join(e['source'] for e in v['evidence'][:3])})")
    text_lines.append('')
    text_lines.append('Procurement contacts:')
    for p in dedup_proc[:200]:
        text_lines.append(f"- {p.get('name') or ''} | {p.get('email')} | {p.get('title') or ''} | {p.get('source')}")

    rec = {
        'type': 'supplemental_document',
        'subtype': 'company_deep_enrichment',
        'timestamp': ec.utc_ts(),
        'source_name': f'deep-enrichment:{company}',
        'source_path': 'public-web',
        'parser': 'deep_enrichment',
        'company': company,
        'vendors': clean_vendors,
        'procurement_contacts': dedup_proc,
        'links_checked': links_checked,
        'text': '\n'.join(text_lines),
    }

    out = ec.write_training_record(company, rec)
    return out


def parse_args():
    p = argparse.ArgumentParser(description='Deep enrich company for vendor/procurement evidence')
    p.add_argument('companies', nargs='+')
    p.add_argument('--max-pages', type=int, default=40)
    return p.parse_args()


def main():
    args = parse_args()
    ok = 0
    for c in args.companies:
        try:
            out = run_deep_enrich(c, max_pages=args.max_pages)
            if out:
                print('ok:', c, '->', out)
                ok += 1
            else:
                print('no results for', c)
        except Exception as exc:
            print('error for', c, '->', exc)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
