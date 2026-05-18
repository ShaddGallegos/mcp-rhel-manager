#!/usr/bin/env python3
"""Ingest curated Red Hat product documentation into HAL training data.

This script targets a bounded set of Red Hat documentation roots and crawls only
the relevant product/version pages so the resulting training corpus is useful
for HAL without pulling in unrelated site footer/navigation content.

Currently no curated Red Hat doc sets are enabled for ingestion in this
repository.
"""

from __future__ import annotations

import argparse
import sys

from ingest_urls import crawl


DOCSETS = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Ingest curated Red Hat product documentation into HAL training data')
    parser.add_argument('docsets', nargs='*', help='Doc set keys to ingest (default: all supported doc sets)')
    parser.add_argument('--depth', type=int, default=1, help='Crawl depth for each seed URL (default: 1)')
    parser.add_argument('--max-pages', type=int, default=400, help='Maximum total pages to fetch across selected doc sets')
    parser.add_argument('--timeout', type=int, default=15, help='Per-request timeout seconds')
    parser.add_argument('--list-docsets', action='store_true', help='List supported doc set keys and exit')
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_docsets:
        for key, info in DOCSETS.items():
            print(f'{key:15} {info["label"]}')
        return 0

    selected = args.docsets or list(DOCSETS.keys())

    # Resolve URL arguments to their matching docset keys so that both
    # a docset key (e.g. "product-1.0") and a full seed URL map to the same docset.
    def _url_to_docset(item: str) -> str:
        if item in DOCSETS:
            return item
        if item.startswith('http://') or item.startswith('https://'):
            for key, info in DOCSETS.items():
                if any(item.rstrip('/') == u.rstrip('/') or item.rstrip('/').startswith(u.rstrip('/'))
                       for u in info['start_urls'] + info['allow_prefixes']):
                    return key
        return item

    selected = [_url_to_docset(s) for s in selected]

    unknown = [item for item in selected if item not in DOCSETS]
    if unknown:
        print('Unknown doc set(s): ' + ', '.join(unknown), file=sys.stderr)
        print('Supported doc sets: ' + ', '.join(sorted(DOCSETS.keys())), file=sys.stderr)
        return 2

    start_urls = []
    allow_prefixes = []
    for key in selected:
        info = DOCSETS[key]
        start_urls.extend(info['start_urls'])
        allow_prefixes.extend(info['allow_prefixes'])

    print('Ingesting Red Hat documentation into HAL training data:')
    for key in selected:
        print(f'  - {key}: {DOCSETS[key]["label"]}')
    print(f'  Seeds      : {len(start_urls)}')
    print(f'  Depth      : {args.depth}')
    print(f'  Max pages  : {args.max_pages}')
    print(f'  Timeout    : {args.timeout}s')
    print('')

    saved = crawl(
        start_urls,
        max_depth=args.depth,
        max_pages=args.max_pages,
        timeout=args.timeout,
        allow_prefixes=allow_prefixes,
        same_host_only=True,
    )

    print('')
    print(f'Completed. Saved {len(saved)} document page(s) into ~/.mcp-ai/training.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())