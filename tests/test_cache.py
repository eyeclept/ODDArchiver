"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for cache.py. CacheManager get, put, fallback, miss, and partial write.
"""
# Imports
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest

# Functions


def _manifest_backend(tmp_path: Path, session: int, entries: list[ManifestEntry], blobs: dict[str, bytes]):
    """Build a MagicMock backend whose read_path() serves a real manifest.json
    for session plus the given blob path -> bytes map. Unknown paths raise
    FileNotFoundError, matching a real BurnBackend on a missing path.
    """
    manifest = Manifest(
        version=1, session=session, timestamp="2026-01-01T00:00:00Z", source="/src",
        label="TEST", based_on_session=session - 1 if session > 0 else None,
        encryption={}, entries=entries, deleted=[], manifest_checksum="",
    )
    session_dir = tmp_path / f"session_{session:03d}"
    session_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(session_dir, manifest)
    manifest_bytes = (session_dir / "manifest.json").read_bytes()

    disc_files = {f"session_{session:03d}/manifest.json": manifest_bytes, **blobs}

    def fake_read_path(path):
        if path not in disc_files:
            raise FileNotFoundError(path)
        return disc_files[path]

    backend = MagicMock()
    backend.read_path.side_effect = fake_read_path
    return backend


@pytest.fixture()
def cache(tmp_path):
    return CacheManager(cache_dir=tmp_path / "cache")


def test_put_then_get_returns_blob(cache):
    """get returns the blob after put for the same (path, session) key."""
    blob = b"encrypted-data-abc"
    cache.put("docs/file.txt", 2, blob)
    assert cache.get("docs/file.txt", 2) == blob


def test_get_returns_none_on_miss(cache):
    """get returns None on a miss (key never written)."""
    assert cache.get("nonexistent/file.txt", 0) is None


def test_cache_miss_logs_warn(cache, caplog):
    """WARN is logged on every cache miss."""
    with caplog.at_level(logging.WARNING, logger="oddarchiver.cache"):
        result = cache.get("missing.bin", 5)
    assert result is None
    assert any("cache miss" in r.message for r in caplog.records)


def test_cache_miss_resolves_real_blob_path_for_full_entry(cache, tmp_path):
    """
    Input:  cache, tmp_path
    Output: None
    Details:
        A cache miss on a "full" entry must read the entry's actual
        content-hashed blob path from the session manifest, not a path
        guessed from the plaintext file path.
        Regression for bug: sync crashed with "Refusing disc read of
        unrecognized path" on any cache miss (fresh machine, cleared cache,
        cache never populated because a prior post-burn verify crashed)
        because get_with_fallback guessed "session_NNN/full/{path}" instead
        of resolving the manifest's real, content-hashed blob path.
    """
    blob_hash = "a" * 64
    blob = b"full-file-content"
    entry = ManifestEntry(
        path="data/file.bin", type="full", result_checksum="deadbeef",
        full_size_bytes=len(blob), file=f"session_001/full/{blob_hash}",
    )
    backend = _manifest_backend(
        tmp_path, session=1, entries=[entry],
        blobs={f"session_001/full/{blob_hash}": blob},
    )

    result = cache.get_with_fallback("data/file.bin", 1, backend)

    assert result == blob
    # next get hits the cache, not the disc
    assert cache.get("data/file.bin", 1) == blob


def test_cache_miss_resolves_real_blob_path_for_delta_entry(cache, tmp_path):
    """
    Input:  cache, tmp_path
    Output: None
    Details:
        A cache miss on a "delta" entry must read entry.delta_file under
        deltas/, not a path guessed under full/. The old hardcoded
        "session_NNN/full/{path}" fallback was wrong for every delta entry
        even before the plaintext-vs-hash mismatch is considered.
    """
    blob_hash = "b" * 64
    blob = b"xdelta3-delta-bytes"
    entry = ManifestEntry(
        path="data/file.bin", type="delta", result_checksum="deadbeef",
        full_size_bytes=1000, delta_file=f"session_002/deltas/{blob_hash}",
        delta_size_bytes=len(blob),
    )
    backend = _manifest_backend(
        tmp_path, session=2, entries=[entry],
        blobs={f"session_002/deltas/{blob_hash}": blob},
    )

    result = cache.get_with_fallback("data/file.bin", 2, backend)

    assert result == blob


def test_get_with_fallback_raises_when_entry_missing(cache, tmp_path):
    """get_with_fallback must raise, not silently return garbage, when the
    session manifest has no entry for the requested path."""
    backend = _manifest_backend(tmp_path, session=1, entries=[], blobs={})

    with pytest.raises(RuntimeError, match="data/file.bin"):
        cache.get_with_fallback("data/file.bin", 1, backend)


def test_partial_write_treated_as_miss(cache, caplog):
    """Partially written cache entry (simulated truncation) is treated as a miss."""
    blob = b"full-encrypted-blob"
    cache.put("partial.txt", 3, blob)

    # truncate the blob file to simulate a partial write
    bp = cache.cache_dir / "blobs" / "3" / "partial.txt.blob"
    bp.write_bytes(blob[:5])

    with caplog.at_level(logging.WARNING, logger="oddarchiver.cache"):
        result = cache.get("partial.txt", 3)

    assert result is None
    assert any("partial write" in r.message or "cache miss" in r.message for r in caplog.records)


def test_get_with_fallback_uses_cache_on_second_call(cache, tmp_path):
    """get_with_fallback uses the cache on the second call, not the backend."""
    blob_hash = "c" * 64
    blob = b"cached-blob"
    entry = ManifestEntry(
        path="file.txt", type="full", result_checksum="deadbeef",
        full_size_bytes=len(blob), file=f"session_000/full/{blob_hash}",
    )
    backend = _manifest_backend(
        tmp_path, session=0, entries=[entry],
        blobs={f"session_000/full/{blob_hash}": blob},
    )

    cache.get_with_fallback("file.txt", 0, backend)
    call_count_after_first = backend.read_path.call_count
    cache.get_with_fallback("file.txt", 0, backend)

    assert backend.read_path.call_count == call_count_after_first


if __name__ == "__main__":
    pass
