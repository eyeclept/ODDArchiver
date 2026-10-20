"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for split-on-full staging. build_staging packs a changeset that
    exceeds one ISOBackend disc into multiple chunks. A single file larger
    than one volume is range-split across dedicated bins.
"""
# Imports
import logging
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import ManifestEntry, read_manifest
from oddarchiver.session import build_staging

logger = logging.getLogger(__name__)
# Globals
_DISC_THAT_SPLITS = 100_000   # two 60 KB files exceed 0.95 * this
_FILE_BYTES = 60_000
_TINY_DISC = 50_000           # one 80 KB file exceeds 0.95 * this
_HUGE_BYTES = 80_000


# Functions


def _make_cache() -> MagicMock:
    """Return a mock CacheManager unused by all-new-file tests."""
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""
    return cache


def _chunk_blobs(chunks: list[tuple[Path, int, bool]]) -> dict[str, bytes]:
    """
    Input:  chunks - build_staging return value
    Output: {rel_path: encoded blob bytes} across every chunk
    Details:
        Raises AssertionError if a path appears in more than one chunk.
    """
    covered: dict[str, bytes] = {}
    for staging, session_n, _is_new in chunks:
        manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
        for entry in manifest.entries:
            assert entry.path not in covered, f"duplicate path across chunks: {entry.path}"
            blob_rel = entry.file or entry.delta_file
            covered[entry.path] = (staging / blob_rel).read_bytes()
    return covered


def _assert_chunk_sequence(chunks: list[tuple[Path, int, bool]]) -> None:
    """Input: staging chunks. Output: None; checks volume order."""
    assert len(chunks) >= 2
    assert not chunks[0][2]
    assert all(is_new for _, _, is_new in chunks[1:])


def test_changeset_larger_than_iso_disc_returns_multiple_chunks(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Two 60 KB new files on an ISOBackend with disc_size=100000 exceed
        remaining_bytes * SPACE_SAFETY_MARGIN. build_staging must return
        2+ chunks. Concatenating every chunk's encoded files covers each
        source file exactly once.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * _FILE_BYTES)
    (source / "b.bin").write_bytes(b"B" * _FILE_BYTES)

    backend = ISOBackend(tmp_path / "test.iso", disc_size=_DISC_THAT_SPLITS)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=backend,
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        _assert_chunk_sequence(chunks)
        session_numbers = [session_n for _, session_n, _ in chunks]
        assert session_numbers == list(range(session_numbers[0], session_numbers[0] + len(chunks)))

        covered = _chunk_blobs(chunks)
        assert set(covered) == {"a.bin", "b.bin"}
        assert covered["a.bin"] == b"A" * _FILE_BYTES
        assert covered["b.bin"] == b"B" * _FILE_BYTES
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def _assert_split_entry(
    entry: ManifestEntry, index: int, count: int | None,
) -> int | None:
    """Input: manifest entry and chunk indexes. Output: checked chunk count."""
    assert entry.path == "huge.bin"
    assert entry.streamed
    assert entry.volume_chunk == index
    if count is None:
        count = entry.volume_chunk_count
    assert entry.volume_chunk_count == count
    assert entry.full_size_bytes == _HUGE_BYTES
    return count


def _assert_oversized_chunks(
    chunks: list[tuple[Path, int, bool]], payload: bytes,
) -> None:
    """Input: staging chunks and source bytes. Output: None; checks split data."""
    parts: list[bytes] = []
    count = None
    for i, (staging, session_n, _is_new) in enumerate(chunks):
        manifest = read_manifest(
            staging / f"session_{session_n:03d}" / "manifest.json"
        )
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        count = _assert_split_entry(entry, i, count)
        parts.append((staging / entry.file).read_bytes())
    assert count == len(chunks)
    assert b"".join(parts) == payload


def test_single_file_larger_than_disc_splits_without_prompt(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        One file whose encoded blob exceeds the configured ISO disc size
        splits across two or more dedicated bins. No stdin prompt is issued.
        Each chunk carries volume_chunk / volume_chunk_count. Concatenating
        the chunk blobs reconstructs the source file under NullCrypto.
    """
    def _forbid_input(*_args, **_kwargs):
        raise AssertionError("oversized file must not prompt for input")

    monkeypatch.setattr("builtins.input", _forbid_input)

    source = tmp_path / "src"
    source.mkdir()
    payload = b"H" * _HUGE_BYTES
    (source / "huge.bin").write_bytes(payload)

    backend = ISOBackend(tmp_path / "test.iso", disc_size=_TINY_DISC)
    staging_root = tmp_path / "staging"
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=backend,
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=staging_root,
    )
    try:
        _assert_chunk_sequence(chunks)
        _assert_oversized_chunks(chunks, payload)
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_pack_maximizes_disc_utilization_over_arrival_order(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Two 60 KB files followed by four 10 KB files (160 KB total) on a
        disc whose packable budget is 95 KB fit in 2 sessions at the
        theoretical minimum (ceil(160000 / 95000) == 2) only if small
        files can fill the gap left by a large file instead of being
        pushed to a later chunk purely because of arrival order. A
        sequential-cutoff packer that gives up on the current bin the
        moment one item does not fit -- never revisiting it for a
        smaller later item -- strands the two big files alone and needs
        3 sessions instead.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a_big.bin").write_bytes(b"A" * 60_000)
    (source / "b_big.bin").write_bytes(b"B" * 60_000)
    (source / "c_small.bin").write_bytes(b"C" * 10_000)
    (source / "d_small.bin").write_bytes(b"D" * 10_000)
    (source / "e_small.bin").write_bytes(b"E" * 10_000)
    (source / "f_small.bin").write_bytes(b"F" * 10_000)

    backend = ISOBackend(tmp_path / "test.iso", disc_size=100_000)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=backend,
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        assert len(chunks) == 2
        covered = _chunk_blobs(chunks)
        assert set(covered) == {
            "a_big.bin", "b_big.bin", "c_small.bin",
            "d_small.bin", "e_small.bin", "f_small.bin",
        }
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)
