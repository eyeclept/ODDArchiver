"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Local cache for encrypted blobs mirroring disc content.
    Cache is a performance optimization only; correctness never depends on it.
"""
# Imports
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend

# Globals
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "oddarchiver"

# Functions


class CacheManager:
    """
    Input:  cache_dir — root directory for the cache (default ~/.cache/oddarchiver)
    Output: N/A (class)
    Details:
        Blobs keyed by (session, path). Cache miss falls back to disc read
        via backend.read_path(). Partial or missing cache treated as miss.
    """

    def __init__(self, cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
        self.cache_dir = cache_dir

    def get(self, path: str, session: int) -> bytes | None:
        """
        Input:  path    — relative file path
                session — session number the blob belongs to
        Output: encrypted blob bytes, or None on miss
        Details:
            Logs WARN on miss.
        """
        raise NotImplementedError

    def put(self, path: str, session: int, blob: bytes) -> None:
        """
        Input:  path    — relative file path
                session — session number
                blob    — encrypted blob bytes
        Output: None
        Details:
            Writes blob and updates cache manifest.
        """
        raise NotImplementedError

    def get_with_fallback(
        self, path: str, session: int, backend: "BurnBackend"
    ) -> bytes:
        """
        Input:  path    — relative file path
                session — session number
                backend — BurnBackend for disc read on cache miss
        Output: encrypted blob bytes
        Details:
            Returns cached blob if present; otherwise reads from disc/ISO,
            reconstructing via delta chain to produce current encrypted blob.
        """
        raise NotImplementedError


if __name__ == "__main__":
    pass
