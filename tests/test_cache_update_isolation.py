"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    A cache-update failure after a verified burn must not fail the run.
"""
# Imports
import logging
from unittest.mock import MagicMock

from oddarchiver.cache import CacheManager, default_cache_dir
from oddarchiver.cli import _burn_verify_commit
from oddarchiver.crypto import NullCrypto
from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest


# Functions


def test_cache_update_failure_does_not_fail_verified_burn(tmp_path, monkeypatch, caplog):
    """OSError during post-verify cache update must not fail _burn_verify_commit."""
    staging = tmp_path / "staging"
    session_dir = staging / "session_000"
    blob_rel = "session_000/full/abc"
    blob_path = staging / blob_rel
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(b"blob-bytes")
    entry = ManifestEntry(
        path="file.txt", type="full", result_checksum="deadbeef",
        full_size_bytes=10, file=blob_rel,
    )
    manifest = Manifest(
        version=1, session=0, timestamp="2026-01-01T00:00:00Z", source="/src",
        label="TEST", based_on_session=None,
        encryption={}, entries=[entry], deleted=[], manifest_checksum="",
    )
    write_manifest(session_dir, manifest)

    backend = MagicMock()
    backend.iso_path = str(tmp_path / "test.iso")
    verify_mod = MagicMock()
    cache_dir = tmp_path / "cache"
    cache = CacheManager(cache_dir=cache_dir)
    assert cache.cache_dir.resolve() != default_cache_dir().resolve()

    def boom(*args, **kwargs):
        raise OSError("injected cache write failure")

    monkeypatch.setattr("oddarchiver.cache._save_manifest", boom)

    with caplog.at_level(logging.ERROR, logger="oddarchiver.cli"):
        rc = _burn_verify_commit(
            command="init",
            staging=staging,
            session_n=0,
            backend=backend,
            label="TEST",
            enc_block={},
            drives=["iso"],
            crypto=NullCrypto(),
            cache=cache,
            verify_mod=verify_mod,
            use_init=True,
            expected_session_count=0,
            mirror_backend=None,
            marker_first_session=0,
        )

    assert rc == 0
    assert any(
        "cache update failed after successful burn and verify for session 0" in r.message
        for r in caplog.records
    )
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


if __name__ == "__main__":
    pass
