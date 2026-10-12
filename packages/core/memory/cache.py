"""Bounded file cache; stat each read so edits/deletion invalidate immediately."""

from __future__ import annotations

from functools import lru_cache
import os


@lru_cache(maxsize=256)
def _read_version(path: str, signature: tuple[int, ...]) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def read_memory_text(path: str) -> str | None:
    try:
        stat = os.stat(path)
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        # Large files are not retained in process memory.
        if stat.st_size > 256_000:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        return _read_version(os.path.abspath(path), signature)
    except FileNotFoundError:
        return None
