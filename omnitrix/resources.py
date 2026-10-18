"""Bounded, paginated inventory; file names are leads, never evidence of a defect."""
import itertools
import os
from pathlib import Path

EXCLUDED = {'.git', '.omnitrix', '.venv', 'node_modules', '__pycache__', '.pytest_cache'}


def resource_kind(locator):
    path = Path(locator)
    name = path.name.lower()
    if path.suffix.lower() in {'.csv', '.prom'} or 'metric' in name:
        return 'metrics'
    if path.suffix.lower() in {'.log', '.jsonl'} or 'trace' in name:
        return 'logs'
    if path.suffix.lower() in {'.py', '.js', '.ts', '.tsx', '.go', '.rs', '.c', '.cpp', '.java', '.rb'}:
        return 'code'
    return 'document'


def list_resources(workspace, directory='.', offset=0, limit=40):
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError('Use offset >= 0 and limit between 1 and 100')
    root = Path(workspace).resolve()
    path = (root / directory).resolve()
    if not path.is_relative_to(root) or not path.is_dir() or EXCLUDED.intersection(path.relative_to(root).parts):
        raise ValueError('Not a research directory inside the workspace')
    # Bound filesystem work even on enormous generated directories. Report this limit.
    with os.scandir(path) as scan:
        children = list(itertools.islice(scan, 10001))
    scan_limited = len(children) > 10000
    entries = []
    for child in children[:10000]:
        if child.name in EXCLUDED or child.is_symlink():
            continue
        is_directory = child.is_dir(follow_symlinks=False)
        if not is_directory and not child.is_file(follow_symlinks=False):
            continue
        locator = str((path / child.name).relative_to(root))
        entries.append({'name': child.name, 'path': locator, 'directory': is_directory,
                        'kind': 'directory' if is_directory else resource_kind(locator),
                        'size_bytes': None if is_directory else child.stat(follow_symlinks=False).st_size})
    entries.sort(key=lambda e: e['name'])
    end = offset + limit
    directories = [e['path'] for e in entries if e['directory']]
    kinds = {kind: sum(e['kind'] == kind for e in entries) for kind in ('directory', 'code', 'metrics', 'logs', 'document')}
    return {'directory': str(path.relative_to(root)), 'entries': entries[offset:end],
            'next_offset': end if end < len(entries) else None, 'scan_limited': scan_limited,
            'kind_counts_in_scanned_directory': kinds,
            'subdirectories': directories[:40], 'subdirectories_truncated': len(directories) > 40,
            'warning': 'Directory exceeds 10000 entries; inventory is incomplete. Use a narrower catalog/export.' if scan_limited else None}
