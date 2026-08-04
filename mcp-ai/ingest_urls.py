#!/usr/bin/env python3
"""Ingest URLs into MCP AI training data.

Reads a list of URLs from stdin (one per line) or as CLI args, crawls up to a specified
depth (default 3), extracts page text, and saves JSON training entries under
~/.mcp-ai/training.

Usage:
  ingest_urls.py                # read URLs from stdin
  ingest_urls.py https://foo.com https://bar/  # pass URLs on the command line
  ingest_urls.py --depth 2 --max-pages 200    # control crawling
"""
import sys
import os
import time
import json
import hashlib
import argparse
from datetime import datetime, timezone
from urllib.parse import urljoin, urldefrag, urlparse

try:
    import requests
except Exception:
    requests = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

HOME = os.path.expanduser('~')
TRAIN_DIR = os.path.join(HOME, '.mcp-ai', 'training')
os.makedirs(TRAIN_DIR, exist_ok=True)


def normalize_url(u):
    # remove fragment
    try:
        u = urldefrag(u)[0]
    except Exception:
        pass
    # ensure scheme
    p = urlparse(u)
    if not p.scheme:
        u = 'http://' + u
    return u


def extract_text(html, url=None):
    if not html:
        return ''
    if BeautifulSoup:
        soup = BeautifulSoup(html, 'html.parser')
        # remove scripts/styles
        for s in soup(['script', 'style', 'noscript', 'header', 'footer', 'svg']):
            s.decompose()
        text = soup.get_text(separator=' ', strip=True)
        return text
    # fallback: very small heuristic strip
    import re
    text = re.sub(r'<(script|style)[\s\S]*?>[\s\S]*?<\/\1>', ' ', html, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def find_links(html, base_url):
    links = set()
    if BeautifulSoup:
        soup = BeautifulSoup(html, 'html.parser')
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.startswith('mailto:') or href.startswith('tel:'):
                continue
            try:
                absolute = urljoin(base_url, href)
                absolute = normalize_url(absolute)
                links.add(absolute)
            except Exception:
                continue
    else:
        import re
        for m in re.finditer(r'href=["\']?([^"\'>\s]+)', html, flags=re.I):
            href = m.group(1)
            if href.startswith('mailto:') or href.startswith('tel:'):
                continue
            try:
                absolute = urljoin(base_url, href)
                absolute = normalize_url(absolute)
                links.add(absolute)
            except Exception:
                continue
    return links


def save_page(url, depth, status, text, html_len):
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    h = hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]
    fname = os.path.join(TRAIN_DIR, f'url-{h}.json')
    parsed = urlparse(url)
    entry = {
        'type': 'supplemental_document',
        'timestamp': ts,
        'source': url,
        'source_url': url,
        'source_name': url,
        'source_host': parsed.netloc,
        'parser': 'html' if html_len else 'text',
        'url': url,
        'depth': depth,
        'status': status,
        'content_length': len(text) if text else 0,
        'text_length': len(text) if text else 0,
        'html_length': html_len,
    }
    # store text separately to keep JSON readable
    entry['text'] = text[:100000] if text else ''
    with open(fname, 'w', encoding='utf-8') as fh:
        json.dump(entry, fh, indent=2)
    return fname


def _is_allowed_url(url, allow_prefixes=None, allowed_hosts=None):
    if not url:
        return False

    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return False

    if allowed_hosts and parsed.netloc not in allowed_hosts:
        return False

    if allow_prefixes and not any(url.startswith(prefix) for prefix in allow_prefixes):
        return False

    return True


def crawl(start_urls, max_depth=3, max_pages=500, timeout=10, allow_prefixes=None, same_host_only=False):
    visited = set()
    results = []
    queue = []
    allowed_hosts = {urlparse(normalize_url(u)).netloc for u in start_urls} if same_host_only else None
    for u in start_urls:
        nu = normalize_url(u)
        if _is_allowed_url(nu, allow_prefixes=allow_prefixes, allowed_hosts=allowed_hosts):
            queue.append((nu, 0))

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            if requests is None:
                print('requests library is required; please install requests', file=sys.stderr)
                break
            print(f'Fetching (depth {depth}): {url}')
            r = requests.get(url, timeout=timeout, headers={'User-Agent': 'MCP-URL-Ingest/1.0'})
            status = r.status_code
            content_type = r.headers.get('content-type', '')
            html_len = len(r.text) if r.text else 0
            text = ''
            links = set()
            if 'text/html' in content_type or url.lower().endswith(('.html', '/')):
                text = extract_text(r.text, url)
                if depth < max_depth:
                    links = find_links(r.text, url)
            else:
                # non-html, treat as plain text
                text = r.text if r.text else ''

            saved = save_page(url, depth, status, text, html_len)
            results.append(saved)

            # enqueue links
            if depth < max_depth:
                for l in links:
                    if not _is_allowed_url(l, allow_prefixes=allow_prefixes, allowed_hosts=allowed_hosts):
                        continue
                    if l not in visited and len(visited) + len(queue) < max_pages:
                        queue.append((l, depth + 1))

        except Exception as e:
            print(f'Fetch failed {url}: {e}', file=sys.stderr)
            saved = save_page(url, depth, 'ERR', '', 0)
            results.append(saved)
            continue

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('urls', nargs='*', help='URLs to ingest (optional)')
    ap.add_argument('--depth', type=int, default=3, help='Crawl depth (default 3)')
    ap.add_argument('--max-pages', type=int, default=500, help='Maximum pages to fetch')
    ap.add_argument('--timeout', type=int, default=10, help='Per-request timeout seconds')
    ap.add_argument('--allow-prefix', action='append', default=[], help='Only crawl URLs under these absolute URL prefixes')
    ap.add_argument('--same-host-only', action='store_true', help='Restrict crawls to the same host(s) as the starting URLs')
    args = ap.parse_args()

    urls = []
    if args.urls:
        urls.extend(args.urls)
    else:
        # read from stdin
        data = sys.stdin.read()
        for l in data.splitlines():
            l = l.strip()
            if not l:
                continue
            urls.append(l)

    if not urls:
        print('No URLs provided on command line or stdin; exiting.', file=sys.stderr)
        sys.exit(2)

    start = time.time()
    saved = crawl(
        urls,
        max_depth=args.depth,
        max_pages=args.max_pages,
        timeout=args.timeout,
        allow_prefixes=args.allow_prefix or None,
        same_host_only=args.same_host_only,
    )
    took = time.time() - start
    print('\nIngest complete: fetched %d pages, wrote %d files in %.1fs' % (len(saved), len(saved), took))
    for f in saved:
        print('  -', f)


if __name__ == '__main__':
    main()
