"""File fingerprinting for incremental compilation."""

import hashlib
from pathlib import Path


def hash_file(path: Path) -> str:
    """SHA256 hash of a single file's contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_file_list(paths: list[Path]) -> str:
    """Combined SHA256 hash of multiple files, order-independent."""
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.read_bytes())
    return h.hexdigest()
