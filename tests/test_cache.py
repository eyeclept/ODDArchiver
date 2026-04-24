"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for cache.py — CacheManager get/put/fallback/miss/partial-write.
"""
# Imports
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cache import CacheManager

# Functions


@pytest.fixture()
def cache(tmp_path):
    return CacheManager(cache_dir=tmp_path / "cache")


def test_put_then_get_returns_blob(cache):
    """cache_get returns blob after cache_put for the same (path, session) key."""
    blob = b"encrypted-data-abc"
    cache.put("docs/file.txt", 2, blob)
    assert cache.get("docs/file.txt", 2) == blob


def test_get_returns_none_on_miss(cache):
    """cache_get returns None on miss (key never written)."""
    assert cache.get("nonexistent/file.txt", 0) is None


def test_cache_miss_logs_warn(cache, caplog):
    """WARN is logged on every cache miss."""
    with caplog.at_level(logging.WARNING, logger="oddarchiver.cache"):
        result = cache.get("missing.bin", 5)
    assert result is None
    assert any("cache miss" in r.message for r in caplog.records)


def test_cache_miss_triggers_backend_fallback(cache):
    """Cache miss triggers backend.read_path() fallback and returns correct bytes."""
    blob = b"disc-blob-data"
    backend = MagicMock()
    backend.read_path.return_value = blob

    result = cache.get_with_fallback("data/file.bin", 1, backend)

    assert result == blob
    backend.read_path.assert_called_once_with("session_001/full/data/file.bin")
    # subsequent get should hit cache, not disc
    assert cache.get("data/file.bin", 1) == blob


def test_partial_write_treated_as_miss(cache, caplog):
    """Partially written cache entry (simulated truncation) is treated as a miss."""
    blob = b"full-encrypted-blob"
    cache.put("partial.txt", 3, blob)

    # corrupt the blob file by truncating it
    bp = cache.cache_dir / "blobs" / "3" / "partial.txt.blob"
    bp.write_bytes(blob[:5])

    with caplog.at_level(logging.WARNING, logger="oddarchiver.cache"):
        result = cache.get("partial.txt", 3)

    assert result is None
    assert any("partial write" in r.message or "cache miss" in r.message for r in caplog.records)


def test_get_with_fallback_uses_cache_on_second_call(cache):
    """get_with_fallback uses the cache on the second call, not the backend."""
    blob = b"cached-blob"
    backend = MagicMock()
    backend.read_path.return_value = blob

    cache.get_with_fallback("file.txt", 0, backend)
    cache.get_with_fallback("file.txt", 0, backend)

    assert backend.read_path.call_count == 1


if __name__ == "__main__":
    pass
