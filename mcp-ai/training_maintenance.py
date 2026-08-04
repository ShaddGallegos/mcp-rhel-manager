#!/usr/bin/env python3
"""Training data maintenance utilities for HAL.

Features:
- Dry-run or apply duplicate cleanup for supplemental_document JSON records.
- Corpus health summary by record type.
- Optional docs.redhat unique-source summary.

By default this runs in dry-run mode and only reports what would be removed.
Use --apply to delete duplicate files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

HOME = os.path.expanduser('~')
TRAIN_DIR = Path(os.environ.get('HAL_TRAIN_DIR', os.path.join(HOME, '.mcp-ai', 'training')))


def _safe_load_json(path: Path):
    try:
        with path.open('r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _doc_key(record: dict) -> str:
    """Canonical key for supplemental documents."""
    source_url = str(record.get('source_url', '') or '').strip().lower()
    source = str(record.get('source', '') or '').strip().lower()
    source_name = str(record.get('source_name', '') or '').strip().lower()
    text = str(record.get('text', '') or record.get('extracted_text', '') or '').strip().lower()

    if source_url:
        return f'url:{source_url}'
    if source:
        return f'source:{source}'
    if source_name and text:
        h = hashlib.sha256(text[:5000].encode('utf-8')).hexdigest()[:16]
        return f'name-text:{source_name}:{h}'
    if text:
        h = hashlib.sha256(text[:5000].encode('utf-8')).hexdigest()[:16]
        return f'text:{h}'

    # Worst-case fallback for malformed records.
    return 'unknown'


def _sort_preference(path: Path):
    """Newest files are preferred when retaining duplicates."""
    try:
        stat = path.stat()
        return (stat.st_mtime, path.name)
    except Exception:
        return (0, path.name)


def run_maintenance(apply_changes: bool = False, top_n: int = 15) -> str:
    if not TRAIN_DIR.exists():
        return f'Training directory not found: {TRAIN_DIR}'

    json_files = sorted(TRAIN_DIR.glob('*.json'))
    jsonl_files = sorted(TRAIN_DIR.glob('*.jsonl'))

    type_counts = Counter()
    docs_redhat_sources = set()
    duplicate_groups = defaultdict(list)
    bad_json = 0

    for path in json_files:
        record = _safe_load_json(path)
        if not isinstance(record, dict):
            bad_json += 1
            continue

        rec_type = str(record.get('type', 'unknown'))
        type_counts[rec_type] += 1

        if rec_type == 'supplemental_document':
            source = str(record.get('source', '') or record.get('source_url', '') or '').strip()
            if 'docs.redhat.com' in source.lower():
                docs_redhat_sources.add(source.lower())
            key = _doc_key(record)
            duplicate_groups[key].append(path)

    # Select duplicates: keep newest file for each key, mark others for removal.
    to_remove = []
    dup_examples = []
    for key, paths in duplicate_groups.items():
        if len(paths) <= 1:
            continue
        keep = sorted(paths, key=_sort_preference, reverse=True)[0]
        extras = [p for p in paths if p != keep]
        to_remove.extend(extras)
        dup_examples.append((key, keep.name, [p.name for p in extras]))

    removed = []
    remove_errors = []
    if apply_changes:
        for p in to_remove:
            try:
                p.unlink()
                removed.append(p)
            except Exception as exc:
                remove_errors.append(f'{p.name}: {exc}')

    lines = []
    lines.append('HAL Training Maintenance Report')
    lines.append('=' * 72)
    lines.append(f'Training dir: {TRAIN_DIR}')
    lines.append(f'JSON files scanned: {len(json_files)}')
    lines.append(f'JSONL files scanned: {len(jsonl_files)}')
    lines.append(f'Malformed JSON files: {bad_json}')
    lines.append('')
    lines.append('Record counts by type:')
    if type_counts:
        for rec_type, cnt in type_counts.most_common():
            lines.append(f'- {rec_type}: {cnt}')
    else:
        lines.append('- none')

    lines.append('')
    lines.append(f'Unique docs.redhat sources: {len(docs_redhat_sources)}')
    lines.append(f'Duplicate supplemental_document groups: {sum(1 for v in duplicate_groups.values() if len(v) > 1)}')
    lines.append(f'Duplicate supplemental_document files: {len(to_remove)}')

    if dup_examples:
        lines.append('')
        lines.append(f'Top duplicate groups (up to {top_n}):')
        for key, keep_name, extras in dup_examples[:top_n]:
            lines.append(f'- key={key[:90]}')
            lines.append(f'  keep={keep_name}')
            lines.append(f'  remove={", ".join(extras[:6])}')

    lines.append('')
    if apply_changes:
        lines.append(f'Apply mode: removed {len(removed)} files')
        if remove_errors:
            lines.append(f'Removal errors: {len(remove_errors)}')
            for err in remove_errors[:top_n]:
                lines.append(f'- {err}')
    else:
        lines.append('Dry-run mode: no files were deleted')
        lines.append('Re-run with --apply to remove duplicate files')

    return '\n'.join(lines)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description='HAL training data maintenance and duplicate cleanup')
    ap.add_argument('--apply', action='store_true', help='Delete duplicate supplemental_document JSON files')
    ap.add_argument('--top', type=int, default=15, help='Number of duplicate groups/errors to show')
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    report = run_maintenance(apply_changes=args.apply, top_n=max(1, args.top))
    print(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
