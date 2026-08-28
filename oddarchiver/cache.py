"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Local cache for encrypted blobs that mirror disc content.
    Cache is a performance optimization only. Correctness never depends on it.
"""
# Imports
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.disc import BurnBackend

# Globals
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "oddarchiver"
_log = logging.getLogger(__name__)


# Functions


def _blob_path(cache_dir: Path, path: str, session: int) -> Path:
    """
    Input:  cache_dir - cache root
            path      - relative file path
            session   - session number
    Output: Path for the blob file in the cache
    Details:
        Layout: <cache_dir>/blobs/<session>/<path>.blob
    """
    return cache_dir / "blobs" / str(session) / (path + ".blob")


def _manifest_path(cache_dir: Path) -> Path:
    """
    Input:  cache_dir - cache root
    Output: Path to the cache manifest JSON file
    """
    return cache_dir / "cache_manifest.json"


def _load_manifest(cache_dir: Path) -> dict:
    """
    Input:  cache_dir - cache root
    Output: dict of cache manifest entries, empty if missing or corrupt
    Details:
        Returns an empty dict when the file is missing or JSON is invalid.
    """
    mp = _manifest_path(cache_dir)
    try:
        return json.loads(mp.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_manifest(cache_dir: Path, manifest: dict) -> None:
    """
    Input:  cache_dir - cache root
            manifest  - dict to persist
    Output: None
    Details:
        Writes via a .tmp file then os.replace so the replace is atomic.
    """
    mp = _manifest_path(cache_dir)
    mp.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, sort_keys=True))
    os.replace(tmp, mp)


def _read_blob_from_disc(
    path: str,
    session: int,
    backend: "BurnBackend",
    crypto: "CryptoBackend | None",
) -> bytes:
    """
    Input:  path    - relative file path to resolve
            session - session the entry belongs to
            backend - BurnBackend to read the manifest and blob from
            crypto  - CryptoBackend for an encrypted manifest (None = plaintext)
    Output: raw blob bytes exactly as stored on disc for that entry
    Details:
        Reads session's manifest (manifest.enc preferred, manifest.json
        fallback) into a temp file so read_manifest() can operate on a real
        path, same convention as verify._read_all_manifests and
        restore._read_manifests. Finds the entry for path and reads its
        actual blob: entry.file for a "full" entry, entry.delta_file for a
        "delta" entry. The blob is named by content hash, never by path.
        Raises RuntimeError if the manifest or the entry cannot be found.
    """
    from oddarchiver.manifest import read_manifest

    data: bytes | None = None
    suffix = ".json"
    for disc_path in (
        f"session_{session:03d}/manifest.enc",
        f"session_{session:03d}/manifest.json",
    ):
        try:
            data = backend.read_path(disc_path)
            suffix = Path(disc_path).suffix
            break
        except (OSError, ValueError):
            continue
    if data is None:
        raise RuntimeError(
            f"Cannot read session {session} manifest to resolve cache miss for {path!r}"
        )

    with tempfile.TemporaryDirectory(prefix="oddarchiver_cache_fallback_") as tmp:
        tmp_file = Path(tmp) / f"manifest_{session:03d}{suffix}"
        tmp_file.write_bytes(data)
        manifest = read_manifest(tmp_file, crypto=crypto)

    for entry in manifest.entries:
        if entry.path != path:
            continue
        blob_rel = entry.file if entry.type == "full" else entry.delta_file
        if not blob_rel:
            break
        return backend.read_path(blob_rel)

    raise RuntimeError(f"No entry for {path!r} in session {session} manifest")


class CacheManager:
    """
    Input:  cache_dir - root directory for the cache (default ~/.cache/oddarchiver)
    Output: N/A (class)
    Details:
        Blobs are keyed by (session, path). get() treats a missing or partial
        blob as a miss. get_with_fallback() reads from the backend on a miss.
    """

    def __init__(self, cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
        self.cache_dir = cache_dir

    def get(self, path: str, session: int) -> bytes | None:
        """
        Input:  path    - relative file path
                session - session number the blob belongs to
        Output: encrypted blob bytes, or None on miss
        Details:
            Logs WARNING on miss. Treats a partially written blob as a miss.
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

        # Treat a partial write as a miss by checking expected size.
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
        Input:  path    - relative file path
                session - session number
                blob    - encrypted blob bytes
        Output: None
        Details:
            Writes the blob atomically and updates the cache manifest.
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
        self,
        path: str,
        session: int,
        backend: "BurnBackend",
        crypto: "CryptoBackend | None" = None,
    ) -> bytes:
        """
        Input:  path    - relative file path
                session - session number
                backend - BurnBackend for disc read on cache miss
                crypto  - CryptoBackend to decrypt an encrypted manifest
                          (None for a plaintext manifest)
        Output: encrypted blob bytes, exactly as stored on disc for that entry
        Details:
            Returns the cached blob if present. On a miss, reads session's
            manifest to find the real entry for path, then reads that entry's
            actual blob (entry.file for "full", entry.delta_file for "delta")
            from the backend. Blobs are named by content hash under full/ or
            deltas/, never by the plaintext path, so the path cannot be
            guessed. Raises RuntimeError if the manifest or the entry for
            path cannot be found.
        """
        cached = self.get(path, session)
        if cached is not None:
            return cached

        blob = _read_blob_from_disc(path, session, backend, crypto)
        self.put(path, session, blob)
        return blob


if __name__ == "__main__":
    pass
