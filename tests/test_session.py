"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for Task 5: session.py — build_staging, scan/diff/stage, space check,
    manifest deleted list, and SIGINT cleanup.
"""
# Imports
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import oddarchiver.session as session_mod
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import DiscInfo
from oddarchiver.manifest import read_manifest

# Globals
_LARGE_REMAINING = 25 * 2**30   # 25 GB — passes space check for tiny staging dirs


# Functions


def _make_backend(remaining_bytes: int = _LARGE_REMAINING) -> MagicMock:
    """Return a mock BurnBackend whose mediainfo() returns the given remaining_bytes."""
    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=1,
        remaining_bytes=remaining_bytes,
        used_bytes=0,
        label="TEST",
    )
    return backend


def _make_cache(old_bytes: bytes = b"") -> MagicMock:
    """Return a mock CacheManager whose get_with_fallback returns NullCrypto(old_bytes)."""
    cache = MagicMock()
    cache.get_with_fallback.return_value = old_bytes
    return cache


def test_new_files_staged_in_full_dir(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        With an empty disc_state, all source files are new.
        build_staging must write them under session_NNN/full/.
    """
    (tmp_path / "hello.txt").write_bytes(b"hello world")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "data.bin").write_bytes(b"\x00\x01\x02")

    staging = session_mod.build_staging(
        session_n=0,
        source=tmp_path,
        disc_state={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        full_dir = staging / "session_000" / "full"
        assert (full_dir / "hello.txt").exists()
        assert (full_dir / "sub" / "data.bin").exists()
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_changed_files_staged_in_deltas_dir(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A file present in disc_state with a different checksum is classified
        as changed. build_staging must produce a .xdelta entry under
        session_NNN/deltas/ for it (assuming delta < threshold).
    """
    # Write a large repetitive file so xdelta3 produces a small delta
    old_content = b"The quick brown fox jumps over the lazy dog.\n" * 200
    new_content = b"The quick brown fox jumps over the LAZY dog.\n" * 200

    src = tmp_path / "doc.txt"
    src.write_bytes(new_content)

    old_checksum = hashlib.sha256(old_content).hexdigest()
    disc_state = {"doc.txt": old_checksum}

    staging = session_mod.build_staging(
        session_n=1,
        source=tmp_path,
        disc_state=disc_state,
        backend=_make_backend(),
        cache=_make_cache(old_content),   # cache returns the old plaintext (NullCrypto)
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        deltas_dir = staging / "session_001" / "deltas"
        assert (deltas_dir / "doc.txt.xdelta").exists()
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_deleted_list_in_manifest(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Files present in disc_state but absent from source must appear in
        the manifest's deleted list.
    """
    (tmp_path / "kept.txt").write_bytes(b"still here")

    disc_state = {
        "kept.txt": hashlib.sha256(b"old kept").hexdigest(),
        "gone.txt": hashlib.sha256(b"was here").hexdigest(),
    }

    staging = session_mod.build_staging(
        session_n=2,
        source=tmp_path,
        disc_state=disc_state,
        backend=_make_backend(),
        cache=_make_cache(b"old kept"),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        manifest = read_manifest(staging / "session_002" / "manifest.json")
        assert "gone.txt" in manifest.deleted
        assert "kept.txt" not in manifest.deleted
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_space_check_exits_1_when_over_limit(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        When staging size >= remaining_bytes * SPACE_SAFETY_MARGIN, build_staging
        must raise SystemExit(1).
    """
    # Write enough data to exceed a tiny remaining_bytes limit
    (tmp_path / "big.bin").write_bytes(b"X" * 1024 * 64)   # 64 KB

    # Set remaining so that 64 KB staging exceeds SPACE_SAFETY_MARGIN * remaining
    # remaining = 1 byte → limit ≈ 0.95 bytes → staging >> limit
    tiny_backend = _make_backend(remaining_bytes=1)

    with pytest.raises(SystemExit) as exc_info:
        session_mod.build_staging(
            session_n=0,
            source=tmp_path,
            disc_state={},
            backend=tiny_backend,
            cache=_make_cache(),
            crypto=NullCrypto(),
            _staging_root=tmp_path / "staging",
        )

    assert exc_info.value.code == 1


def test_sigint_during_staging_cleans_up_temp_dir(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        If _sigint_received is set during the staging scan, build_staging raises
        KeyboardInterrupt and the staging directory is removed.
    """
    (tmp_path / "file.txt").write_bytes(b"content")

    # Simulate SIGINT arriving during the sha256 scan
    real_sha256 = session_mod._sha256_file

    def sha256_then_signal(path: Path) -> str:
        session_mod._sigint_received = True
        return real_sha256(path)

    monkeypatch.setattr(session_mod, "_sha256_file", sha256_then_signal)

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    expected_staging = staging_root / "oddarchiver_staging_001"

    with pytest.raises(KeyboardInterrupt):
        session_mod.build_staging(
            session_n=1,
            source=tmp_path,
            disc_state={},
            backend=_make_backend(),
            cache=_make_cache(),
            crypto=NullCrypto(),
            _staging_root=staging_root,
        )

    assert not expected_staging.exists(), "staging dir must be removed after SIGINT"
