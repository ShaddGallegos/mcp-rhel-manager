#!/usr/bin/env python3
"""Enrich HAL training with public company intelligence.

This script fetches public, source-attributed company information and writes it
into ~/.mcp-ai/training as supplemental documents.

Data sources (best effort):
- Wikipedia summary
- DuckDuckGo web search snippets
- Google News RSS headlines
- Public profile links (company site, LinkedIn, X, Google Patents)

Contact handling:
- Extracts candidate contacts/emails for the company from local training docs
- Attempts lightweight public verification using name/company web matches
- Marks confidence explicitly (high/medium/low) for traceability
"""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = os.path.expanduser("~")
TRAIN_DIR = Path(HOME) / ".mcp-ai" / "training"
UA = "Mozilla/5.0 (X11; Linux x86_64) HAL-Enrichment/1.0"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def norm_company(value: str) -> str:
    v = (value or "").strip().lower()
    v = re.sub(r"\b(inc|inc\.|llc|ltd|corp|corporation|co|company|plc)\b", "", v)
    v = re.sub(r"[^a-z0-9\s]", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v


def from_training_companies(max_companies: int) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for fp in sorted(TRAIN_DIR.glob("*.json"), reverse=True):
        try:
            rec = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rec.get("type") != "supplemental_document":
            continue
        text = (rec.get("text") or "")
        if not text:
            continue

        for line in text.splitlines():
            if "|" not in line:
                continue
            cols = [c.strip() for c in line.split("|")]
            if not cols:
                continue
            first = cols[0]
            if not first or first.lower() in {
                "contact name", "account name", "company", "account", "name", "customer"
            }:
                continue
            if first.startswith(("#", "-", "*", ":")):
                continue
            if ":" in first and len(first.split()) > 4:
                continue
            if len(first) > 70:
                continue
            if not re.search(r"[A-Za-z]", first):
                continue
            # Skip rows that look like person records in first column.
            if len(first.split()) >= 2 and "@" not in first and len(cols) > 1 and "@" in line and "contact" in text.lower():
                continue
            key = norm_company(first)
            if not key or len(key) < 3:
                continue
            if key in seen:
                continue
            seen.add(key)
            names.append(first)
            if len(names) >= max_companies:
                return names
    return names


def ddg_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    q = urllib.parse.quote_plus(query)
    url = f"https://duckduckgo.com/html/?q={q}"
    try:
        body = http_get(url)
    except Exception:
        return []

    results: list[dict[str, str]] = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>|'
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in pattern.finditer(body):
        href = match.group(1) or match.group(4) or ""
        title_raw = match.group(2) or match.group(5) or ""
        snippet_raw = match.group(3) or match.group(6) or ""

        title = html.unescape(re.sub(r"<[^>]+>", "", title_raw)).strip()
        snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet_raw)).strip()

        if "duckduckgo.com/l/?" in href:
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
                href = urllib.parse.unquote(qs.get("uddg", [href])[0])
            except Exception:
                pass

        if not href.startswith("http"):
            continue
        if not title:
            continue
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break

    return results


def wiki_summary(company: str) -> dict[str, Any] | None:
    title = urllib.parse.quote(company.replace(" ", "_"))
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
    try:
        raw = http_get(url)
        data = json.loads(raw)
    except Exception:
        return None

    extract = (data.get("extract") or "").strip()
    if not extract:
        return None
    return {
        "title": data.get("title", company),
        "summary": extract,
        "content_urls": data.get("content_urls", {}),
        "source": url,
    }


def google_news_headlines(company: str, max_items: int = 8) -> list[dict[str, str]]:
    q = urllib.parse.quote_plus(company)
    url = f"https://news.google.com/rss/search?q={q}"
    try:
        xml_text = http_get(url)
    except Exception:
        return []

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []

    out: list[dict[str, str]] = []
    for item in root.findall(".//item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source = ""
        source_node = item.find("source")
        if source_node is not None and source_node.text:
            source = source_node.text.strip()
        if title and link:
            out.append({"title": title, "link": link, "published": pub, "source": source})
    return out


def classify_links(search_results: list[dict[str, str]]) -> dict[str, Any]:
    official = ""
    linkedin: list[str] = []
    x_urls: list[str] = []
    patents: list[str] = []

    for r in search_results:
        u = (r.get("url") or "").lower()
        if "linkedin.com/company" in u:
            linkedin.append(r["url"])
        if "x.com/" in u or "twitter.com/" in u:
            x_urls.append(r["url"])
        if "patents.google.com" in u:
            patents.append(r["url"])

    for r in search_results:
        u = r.get("url", "")
        ul = u.lower()
        # Skip common non-official hosts for website inference.
        if any(x in ul for x in ("wikipedia.org", "linkedin.com", "x.com", "twitter.com", "news.google.com", "patents.google.com")):
            continue
        official = u
        break

    return {
        "official_website": official,
        "linkedin": sorted(set(linkedin))[:5],
        "x": sorted(set(x_urls))[:5],
        "google_patents": sorted(set(patents))[:5],
    }


def extract_contacts_for_company(company: str, max_contacts: int = 50) -> list[dict[str, str]]:
    company_l = company.lower()
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    for fp in sorted(TRAIN_DIR.glob("*.json"), reverse=True):
        try:
            rec = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rec.get("type") != "supplemental_document":
            continue
        text = (rec.get("text") or "")
        if not text:
            continue

        for line in text.splitlines():
            ll = line.lower()
            if company_l not in ll or "@" not in line:
                continue
            emails = sorted(set(e.lower() for e in EMAIL_RE.findall(line)))
            if not emails:
                continue
            cols = [c.strip() for c in line.split("|")]
            name = cols[0] if cols else ""
            if name.lower() in {"contact name", "name", "account name", "company"}:
                name = ""
            for email in emails:
                if email in seen:
                    continue
                seen.add(email)
                out.append({"name": name, "email": email, "evidence_line": line.strip()})
                if len(out) >= max_contacts:
                    return out
    return out


def domain_from_url(url: str) -> str:
    try:
        netloc = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def verify_contacts_publicly(company: str, contacts: list[dict[str, str]], official_site: str) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    official_domain = domain_from_url(official_site)
    company_token = norm_company(company).replace(" ", "")

    for c in contacts[:30]:
        email = c.get("email", "")
        name = c.get("name", "")
        email_domain = email.split("@", 1)[1] if "@" in email else ""

        domain_match = bool(official_domain and email_domain and official_domain.endswith(email_domain))
        token_match = bool(company_token and company_token in email_domain.replace(".", ""))

        public_hits = []
        if name and len(name.split()) >= 2:
            query = f'"{name}" "{company}"'
            results = ddg_search(query, max_results=3)
            for r in results:
                blob = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
                if norm_company(company) in blob:
                    public_hits.append({"title": r.get("title", ""), "url": r.get("url", "")})

        score = 0
        if domain_match:
            score += 1
        if token_match:
            score += 1
        if public_hits:
            score += 1

        if score >= 3:
            confidence = "high"
        elif score == 2:
            confidence = "medium"
        else:
            confidence = "low"

        verified.append({
            "name": name,
            "email": email,
            "verification": {
                "confidence": confidence,
                "domain_match_official_site": domain_match,
                "domain_token_match": token_match,
                "public_hits": public_hits,
            },
        })

        # Be polite to public services.
        time.sleep(0.15)

    return verified


def fetch_homepage_description(url: str, timeout: int = 12) -> str:
    """Fetch the company homepage and return a short description.

    Tries in order:
    1. <meta name="description"> content
    2. <meta property="og:description"> content
    3. First <p> tag with >= 60 chars and no HTML
    Returns an empty string on any failure.
    """
    if not url or not url.startswith("http"):
        return ""
    try:
        body = http_get(url, timeout=timeout)
    except Exception:
        return ""

    # meta description
    for pat in (
        r'<meta\s+name=["\']description["\'][^>]*content=["\'](.*?)["\']',
        r'<meta\s+content=["\'](.*?)["\'][^>]*name=["\']description["\']',
        r'<meta\s+property=["\']og:description["\'][^>]*content=["\'](.*?)["\']',
        r'<meta\s+content=["\'](.*?)["\'][^>]*property=["\']og:description["\']',
    ):
        m = re.search(pat, body, re.IGNORECASE | re.DOTALL)
        if m:
            text = html.unescape(m.group(1)).strip()
            if len(text) >= 30:
                return text[:400]

    # first substantial <p> paragraph
    for m in re.finditer(r'<p[^>]*>(.*?)</p>', body, re.IGNORECASE | re.DOTALL):
        text = html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip()
        # skip nav/cookie/legal noise
        if len(text) >= 60 and len(text) <= 600 and '\n' not in text[:60]:
            if not re.search(r'\b(cookie|privacy|copyright|accept|terms)\b', text, re.IGNORECASE):
                return text[:400]

    return ""


def fetch_mission_statement(company: str, links: dict[str, Any], search_results: list[dict[str, str]], timeout: int = 12) -> str:
    """Try to discover a company's mission statement from public pages.

    Strategy:
    - Probe common 'about' URLs on the official site.
    - Use DuckDuckGo search results for "<company> mission statement" and inspect hits.
    - Look for headings or paragraphs containing 'mission' / 'our mission' / 'mission statement'.
    Returns short cleaned text or empty string.
    """
    candidates: list[str] = []
    official = (links.get("official_website") or "").rstrip("/")
    if official and official.startswith("http"):
        for suffix in ("/about", "/about-us", "/about/", "/company/about", "/about-us/mission", "/mission"):
            candidates.append(official + suffix)

    # Add search-derived candidates that likely contain mission content
    try:
        mission_search = ddg_search(f"{company} mission statement", max_results=6)
    except Exception:
        mission_search = []
    for r in mission_search:
        u = r.get("url")
        if u and u not in candidates:
            candidates.append(u)

    # Also inspect combined search results (if supplied)
    for r in (search_results or []):
        u = r.get("url")
        if u and u not in candidates:
            if any(k in u.lower() for k in ("about", "mission", "company", "about-us")):
                candidates.append(u)

    seen: set[str] = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            body = http_get(url, timeout=timeout)
        except Exception:
            continue

        # 1) Look for a heading that mentions mission and return the following paragraph
        m = re.search(r'(?s)<h[1-6][^>]*>[^<]{0,60}mission[^<]*</h[1-6]>.*?<p[^>]*>(.*?)</p>', body, re.IGNORECASE)
        if m:
            text = html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip()
            if len(text) >= 30:
                return text[:500]

        # 2) Look for inline mentions like 'Our mission' and capture surrounding paragraph
        m2 = re.search(r'(?i)(?:our\s+mission|mission\s+statement|mission:|purpose:|our purpose)([\s\S]{0,400})', body)
        if m2:
            txt = html.unescape(re.sub(r'<[^>]+>', '', m2.group(1))).strip()
            txt = re.sub(r'\s+', ' ', txt)
            if len(txt) >= 30:
                return txt[:500]

        # 3) Meta description fallback
        for pat in (
            r'<meta\s+name=["\']description["\'][^>]*content=["\'](.*?)["\']',
            r'<meta\s+property=["\']og:description["\'][^>]*content=["\'](.*?)["\']',
        ):
            mm = re.search(pat, body, re.IGNORECASE | re.DOTALL)
            if mm:
                txt = html.unescape(mm.group(1)).strip()
                if len(txt) >= 30:
                    return txt[:500]

    return ""


def build_text_blob(company: str, wiki: dict[str, Any] | None, links: dict[str, Any], headlines: list[dict[str, str]], verified_contacts: list[dict[str, Any]], search_results: list[dict[str, str]], homepage_desc: str = "", mission_statement: str = "") -> str:
    lines: list[str] = []
    lines.append(f"# Company Public Enrichment: {company}")
    lines.append("")

    if wiki:
        lines.append("## Wikipedia Summary")
        lines.append(wiki.get("summary", ""))
        lines.append(f"Source: {wiki.get('source', '')}")
        lines.append("")

    if mission_statement:
        lines.append("## Company Mission Statement")
        lines.append(mission_statement)
        lines.append("")

    if homepage_desc:
        lines.append("## Company Homepage Description")
        lines.append(homepage_desc)
        lines.append(f"Source: {links.get('official_website', '')}")
        lines.append("")

    lines.append("## Public Profiles")
    lines.append(f"Official website: {links.get('official_website', '') or 'not found'}")
    for l in links.get("linkedin", []):
        lines.append(f"LinkedIn: {l}")
    for x in links.get("x", []):
        lines.append(f"X: {x}")
    for p in links.get("google_patents", []):
        lines.append(f"Google Patents: {p}")
    lines.append("")

    lines.append("## Recent Headlines")
    if headlines:
        for h in headlines:
            src = f" ({h['source']})" if h.get("source") else ""
            lines.append(f"- {h.get('title', '')}{src}")
            if h.get("link"):
                lines.append(f"  {h['link']}")
    else:
        lines.append("- none collected")
    lines.append("")

    lines.append("## Contact Verification")
    if verified_contacts:
        for c in verified_contacts:
            v = c.get("verification", {})
            lines.append(f"- {c.get('name', '').strip() or 'Unknown'} | {c.get('email', '')} | confidence={v.get('confidence', 'low')}")
            hits = v.get("public_hits") or []
            for hit in hits[:2]:
                lines.append(f"  public-hit: {hit.get('title', '')} -> {hit.get('url', '')}")
    else:
        lines.append("- no contact records found for this company in local training")
    lines.append("")

    lines.append("## Search Evidence")
    for r in search_results[:12]:
        lines.append(f"- {r.get('title', '')}")
        lines.append(f"  {r.get('url', '')}")
        if r.get("snippet"):
            lines.append(f"  snippet: {r.get('snippet', '')}")

    return "\n".join(lines).strip()


def write_training_record(company: str, payload: dict[str, Any]) -> Path:
    TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    ts = utc_ts()
    out_name = f"doc-{ts}-{sha12(company + ts)}.json"
    out = TRAIN_DIR / out_name
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def enrich_one_company(company: str) -> tuple[str, Path | None, str]:
    company = company.strip()
    if not company:
        return "", None, "skipped-empty"

    generic_results = ddg_search(company, max_results=10)
    official_results = ddg_search(f"{company} official website", max_results=8)
    linkedin_results = ddg_search(f"site:linkedin.com/company {company}", max_results=5)
    x_results = ddg_search(f"site:x.com {company}", max_results=5)
    patents_results = ddg_search(f"site:patents.google.com {company}", max_results=5)

    combined_results = generic_results + official_results + linkedin_results + x_results + patents_results
    links = classify_links(combined_results)
    wiki = wiki_summary(company)
    headlines = google_news_headlines(company, max_items=8)

    contacts = extract_contacts_for_company(company)
    verified = verify_contacts_publicly(company, contacts, links.get("official_website", ""))

    # Optionally augment with locally collected enrichment/scraper outputs
    try:
        if os.environ.get('HAL_ENRICH_USE_SCRAPERS', '').lower() in ('1', 'true', 'yes'):
            bf_path = Path(__file__).resolve().parents[0] / 'bi_fetcher.py'
            if bf_path.exists():
                spec = importlib.util.spec_from_file_location('bi_fetcher', str(bf_path))
                bf = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(bf)
                ext = bf.fetch_company_from_enrichment(company)
                # merge contacts (avoid duplicates)
                if ext.get('contacts'):
                    emails_seen = { (c.get('email') or '').lower() for c in verified }
                    for c in ext.get('contacts'):
                        try:
                            em = (c.get('email') or '').strip()
                        except Exception:
                            em = ''
                        if not em:
                            continue
                        if em.lower() in emails_seen:
                            continue
                        emails_seen.add(em.lower())
                        verified.append(c)
                # merge headlines
                if ext.get('news_headlines'):
                    existing_titles = { (h.get('title') if isinstance(h, dict) else str(h)).strip() for h in headlines }
                    for h in ext.get('news_headlines'):
                        if h and h not in existing_titles:
                            headlines.append({'title': h})
                # merge links (concat lists)
                for k, v in (ext.get('links') or {}).items():
                    if not v:
                        continue
                    old = links.get(k) or []
                    if isinstance(old, list):
                        links[k] = sorted(dict.fromkeys(old + (v if isinstance(v, list) else [v])))
                    else:
                        links[k] = v
    except Exception:
        # best-effort augmentation; failures should not abort enrichment
        pass

    homepage_desc = fetch_homepage_description(links.get("official_website", ""))
    mission_statement = fetch_mission_statement(company, links, combined_results)
    text_blob = build_text_blob(company, wiki, links, headlines, verified, combined_results, homepage_desc=homepage_desc, mission_statement=mission_statement)

    rec = {
        "type": "supplemental_document",
        "subtype": "company_public_enrichment",
        "timestamp": utc_ts(),
        "source_name": f"public-enrichment:{company}",
        "source_path": "public-web",
        "source_ext": "web",
        "parser": "public_enrichment",
        "content_length": len(text_blob),
        "company": company,
        "links": links,
        "homepage_description": homepage_desc,
        "mission_statement": mission_statement,
        "news_headlines": headlines,
        "contact_verification": verified,
        "search_results": combined_results[:40],
        "text": text_blob,
    }

    out = write_training_record(company, rec)
    return company, out, "ok"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Enrich company intel from public sources into HAL training")
    p.add_argument("companies", nargs="*", help="Company names to enrich")
    p.add_argument("--from-training", action="store_true", help="Discover company names from current training docs")
    p.add_argument("--max-companies", type=int, default=10, help="Limit when using --from-training")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    companies = [c.strip() for c in args.companies if c.strip()]
    if args.from_training:
        discovered = from_training_companies(max_companies=max(1, args.max_companies))
        for d in discovered:
            if d not in companies:
                companies.append(d)

    if not companies:
        print("No companies provided. Use names or --from-training.", file=sys.stderr)
        return 2

    print(f"Enriching {len(companies)} compan(ies) from public sources...")

    ok = 0
    errors = 0
    for c in companies:
        try:
            company, out, status = enrich_one_company(c)
            if status == "ok" and out:
                ok += 1
                print(f"ok: {company} -> {out}")
            else:
                errors += 1
                print(f"error: {company} -> {status}", file=sys.stderr)
        except Exception as exc:
            errors += 1
            print(f"error: {c} -> {exc}", file=sys.stderr)

    print(f"done: ok={ok} errors={errors}")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
