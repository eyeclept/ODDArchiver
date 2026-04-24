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

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend

# Globals
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "oddarchiver"
_log = logging.getLogger(__name__)


# Functions


def _blob_path(cache_dir: Path, path: str, session: int) -> Path:
    """
    Input:  cache_dir — cache root
            path      — relative file path
            session   — session number
    Output: Path for the blob file in the cache
    Details:
        Layout: <cache_dir>/blobs/<session>/<path>.blob
    """
    return cache_dir / "blobs" / str(session) / (path + ".blob")


def _manifest_path(cache_dir: Path) -> Path:
    """
    Input:  cache_dir — cache root
    Output: Path to the cache manifest JSON file
    """
    return cache_dir / "cache_manifest.json"


def _load_manifest(cache_dir: Path) -> dict:
    """
    Input:  cache_dir — cache root
    Output: dict of cache manifest entries, empty if missing or corrupt
    Details:
        Treats missing or invalid JSON as empty manifest (cache miss semantics).
    """
    mp = _manifest_path(cache_dir)
    try:
        return json.loads(mp.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_manifest(cache_dir: Path, manifest: dict) -> None:
    """
    Input:  cache_dir — cache root
            manifest  — dict to persist
    Output: None
    Details:
        Writes atomically via .tmp + os.replace.
    """
    mp = _manifest_path(cache_dir)
    mp.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, sort_keys=True))
    os.replace(tmp, mp)


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
            Logs WARN on miss. Treats partially written blobs as miss.
        """
        bp = _blob_path(self.cache_dir, path, session)
        manifest = _load_manifest(self.cache_dir)
        key = f"{session}:{path}"

        if key not in manifest:
            _log.warning("cache miss: %s session %d (not in manifest)", path, session)
            return None

        try:
            data = bp.read_bytes()
        except FileNotFoundError:
            _log.warning("cache miss: %s session %d (blob file missing)", path, session)
            return None

        # treat partial write as miss by checking expected size
        expected = manifest[key]["size"]
        if len(data) != expected:
            _log.warning(
                "cache miss: %s session %d (partial write: got %d expected %d)",
                path, session, len(data), expected,
            )
            return None

        return data

    def put(self, path: str, session: int, blob: bytes) -> None:
        """
        Input:  path    — relative file path
                session — session number
                blob    — encrypted blob bytes
        Output: None
        Details:
            Writes blob atomically and updates the cache manifest.
        """
        bp = _blob_path(self.cache_dir, path, session)
        bp.parent.mkdir(parents=True, exist_ok=True)

        tmp = bp.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, bp)

        manifest = _load_manifest(self.cache_dir)
        manifest[f"{session}:{path}"] = {"size": len(blob)}
        _save_manifest(self.cache_dir, manifest)

    def get_with_fallback(
        self, path: str, session: int, backend: "BurnBackend"
    ) -> bytes:
        """
        Input:  path    — relative file path
                session — session number
                backend — BurnBackend for disc read on cache miss
        Output: encrypted blob bytes
        Details:
            Returns cached blob if present; otherwise reads from disc/ISO via
            backend.read_path() and populates the cache for the next call.
        """
        cached = self.get(path, session)
        if cached is not None:
            return cached

        # disc fallback: read_path returns the encrypted blob as stored on disc
        disc_path = f"session_{session:03d}/full/{path}"
        blob = backend.read_path(disc_path)
        self.put(path, session, blob)
        return blob


if __name__ == "__main__":
    pass
