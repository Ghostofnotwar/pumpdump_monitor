"""Managed snapshot filenames and bounded disk retention; never delete arbitrary paths."""

import re
import time
from pathlib import Path

FILENAME = re.compile(r"[A-Z0-9][A-Z0-9_.-]{0,63}_[a-f0-9]{32}\.png\Z")


def snapshot_path(directory: Path, value: str | Path | None) -> Path | None:
    if not value:
        return None
    name = Path(value).name
    if not FILENAME.fullmatch(name):
        return None
    root = directory.resolve()
    path = root / name
    if path.is_symlink() or path.resolve().parent != root or not path.is_file():
        return None
    return path


def cleanup_snapshots(directory: Path, retention_days: int, max_mb: int) -> int:
    if not directory.is_dir():
        return 0
    files = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if not FILENAME.fullmatch(path.name) and not (
            path.suffix == ".tmp" and path.stat().st_mtime < time.time() - 86400
        ):
            continue
        stat = path.stat()
        files.append((stat.st_mtime, stat.st_size, path))
    cutoff = time.time() - retention_days * 86400
    keep, total, removed = [], 0, 0
    for modified, size, path in sorted(files):
        if modified < cutoff or (path.suffix == ".tmp" and modified < time.time() - 86400):
            path.unlink(missing_ok=True)
            removed += 1
        else:
            keep.append((modified, size, path))
            total += size
    for _, size, path in keep:
        if total <= max_mb * 1024 * 1024:
            break
        path.unlink(missing_ok=True)
        total -= size
        removed += 1
    return removed
