"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for split-on-full staging. build_staging packs a changeset that
    exceeds one ISOBackend disc into multiple chunks. A single file larger
    than one volume is range-split across bins. Default packing keeps a
    file whole when it fits one medium.
"""
# Imports
import logging
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cli import build_parser
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import DiscBackend, DiscInfo, ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, read_manifest
from oddarchiver.session import build_staging

logger = logging.getLogger(__name__)
# Globals
_DISC_THAT_SPLITS = 100_000   # two 60 KB files exceed 0.95 * this
_FILE_BYTES = 60_000
_TINY_DISC = 50_000           # one 80 KB file exceeds 0.95 * this
_HUGE_BYTES = 80_000
_DVD_RATIO_DISC = 47_000      # 4.7 GB DVD scaled; 76.6 KB payload still splits
_DVD_RATIO_FILE = 76_600


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


def _chunk_manifests(chunks: list[tuple[Path, int, bool]]) -> list[Manifest]:
    """Input: staging chunks. Output: one manifest per volume."""
    manifests: list[Manifest] = []
    for staging, session_n, _is_new in chunks:
        manifests.append(
            read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
        )
    return manifests


def _joined_blobs(chunks: list[tuple[Path, int, bool]]) -> dict[str, bytes]:
    """
    Input: staging chunks
    Output: path to concatenated blob bytes in volume_chunk order
    """
    parts: dict[str, list[tuple[int, bytes]]] = {}
    for staging, session_n, _is_new in chunks:
        manifest = read_manifest(
            staging / f"session_{session_n:03d}" / "manifest.json"
        )
        for entry in manifest.entries:
            blob_rel = entry.file or entry.delta_file
            index = entry.volume_chunk if entry.volume_chunk is not None else 0
            parts.setdefault(entry.path, []).append(
                (index, (staging / blob_rel).read_bytes())
            )
    covered: dict[str, bytes] = {}
    for path, items in parts.items():
        items.sort(key=lambda item: item[0])
        covered[path] = b"".join(data for _index, data in items)
    return covered


def _stage_pair(
    tmp_path, *, split_large: bool = False,
    file_bytes: int = _FILE_BYTES, disc_size: int = _DISC_THAT_SPLITS,
) -> tuple[list[tuple[Path, int, bool]], bytes, bytes]:
    """Input: temp dir and packing options. Output: chunks and source bytes."""
    source = tmp_path / "src"
    source.mkdir()
    a_bytes = os.urandom(file_bytes)
    b_bytes = os.urandom(file_bytes)
    (source / "a.bin").write_bytes(a_bytes)
    (source / "b.bin").write_bytes(b_bytes)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "test.iso", disc_size=disc_size),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
        split_large=split_large,
    )
    return chunks, a_bytes, b_bytes


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
    a_bytes = os.urandom(_FILE_BYTES)
    b_bytes = os.urandom(_FILE_BYTES)
    (source / "a.bin").write_bytes(a_bytes)
    (source / "b.bin").write_bytes(b_bytes)
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
        assert covered["a.bin"] == a_bytes
        assert covered["b.bin"] == b_bytes
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
    payload = os.urandom(_HUGE_BYTES)
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


def test_real_drive_splits_at_loaded_medium_size(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A DiscBackend with a small blank medium (a DVD in the drive) must
        split a larger file at that medium's size. Before the fix, later
        discs used the BDXL size, so the file stayed whole and could not
        burn to the next small disc.
    """
    monkeypatch.setattr(
        DiscBackend, "mediainfo",
        lambda self, *a, **k: DiscInfo(
            session_count=0, remaining_bytes=_TINY_DISC, used_bytes=0, label="",
        ),
    )
    source = tmp_path / "src"
    source.mkdir()
    payload = os.urandom(_HUGE_BYTES)
    (source / "huge.bin").write_bytes(payload)

    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=DiscBackend("/dev/null-test"),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
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
    (source / "a_big.bin").write_bytes(os.urandom(60_000))
    (source / "b_big.bin").write_bytes(os.urandom(60_000))
    (source / "c_small.bin").write_bytes(os.urandom(10_000))
    (source / "d_small.bin").write_bytes(os.urandom(10_000))
    (source / "e_small.bin").write_bytes(os.urandom(10_000))
    (source / "f_small.bin").write_bytes(os.urandom(10_000))

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


def test_two_files_that_fit_one_medium_stay_whole(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Two 60 KB files on a 100 KB disc match two 3 GB files on a
        4.46 GB DVD. Each file fits one medium. Default packing keeps
        each file whole on its own volume and leaves free space.
    """
    chunks, a_bytes, b_bytes = _stage_pair(tmp_path)
    try:
        _assert_chunk_sequence(chunks)
        assert len(chunks) == 2
        manifests = _chunk_manifests(chunks)
        assert [len(m.entries) for m in manifests] == [1, 1]
        paths = {m.entries[0].path for m in manifests}
        assert paths == {"a.bin", "b.bin"}
        for manifest in manifests:
            entry = manifest.entries[0]
            assert entry.volume_chunk is None
            assert entry.volume_chunk_count is None
        covered = _chunk_blobs(chunks)
        assert covered["a.bin"] == a_bytes
        assert covered["b.bin"] == b_bytes
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_file_larger_than_medium_still_splits(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A 76.6 KB file on a 47 KB disc matches a 7.66 GB file on a
        4.46 GB DVD. The file exceeds one medium and still splits.
    """
    source = tmp_path / "src"
    source.mkdir()
    payload = os.urandom(_DVD_RATIO_FILE)
    (source / "huge.bin").write_bytes(payload)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "test.iso", disc_size=_DVD_RATIO_DISC),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        _assert_chunk_sequence(chunks)
        manifests = _chunk_manifests(chunks)
        assert len(manifests) >= 2
        parts: list[bytes] = []
        count = None
        for i, (staging, session_n, _is_new) in enumerate(chunks):
            manifest = manifests[i]
            assert len(manifest.entries) == 1
            entry = manifest.entries[0]
            assert entry.path == "huge.bin"
            assert entry.volume_chunk == i
            if count is None:
                count = entry.volume_chunk_count
            assert entry.volume_chunk_count == count
            parts.append((staging / entry.file).read_bytes())
        assert count == len(chunks)
        assert b"".join(parts) == payload
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_split_large_splits_second_file_onto_first_volume(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        --split-large restores greedy fill. Two 60 KB files on a 100 KB
        disc put a.bin whole on volume 1 and split b.bin across volumes.
    """
    chunks, a_bytes, b_bytes = _stage_pair(tmp_path, split_large=True)
    try:
        _assert_chunk_sequence(chunks)
        assert len(chunks) >= 2
        manifests = _chunk_manifests(chunks)
        vol0_paths = {e.path for e in manifests[0].entries}
        assert "a.bin" in vol0_paths
        assert "b.bin" in vol0_paths
        a_entries = [
            e for m in manifests for e in m.entries if e.path == "a.bin"
        ]
        assert len(a_entries) == 1
        assert a_entries[0].volume_chunk is None
        b_entries = [
            e for m in manifests for e in m.entries if e.path == "b.bin"
        ]
        assert len(b_entries) >= 2
        assert {e.volume_chunk for e in b_entries} == set(range(len(b_entries)))
        joined = _joined_blobs(chunks)
        assert joined["a.bin"] == a_bytes
        assert joined["b.bin"] == b_bytes
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_file_above_blob_cap_still_splits(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A 50 KB file fits a 200 KB disc but exceeds a 20 KB BLOB_SIZE_CAP.
        Staging still splits it into volume chunks.
    """
    monkeypatch.setattr("oddarchiver.session.BLOB_SIZE_CAP", 20_000)
    source = tmp_path / "src"
    source.mkdir()
    payload = os.urandom(50_000)
    (source / "cap.bin").write_bytes(payload)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "test.iso", disc_size=200_000),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        manifests = _chunk_manifests(chunks)
        entries = [e for m in manifests for e in m.entries if e.path == "cap.bin"]
        assert len(entries) >= 2
        assert all(e.volume_chunk is not None for e in entries)
        assert entries[0].volume_chunk_count == len(entries)
        joined = _joined_blobs(chunks)
        assert joined["cap.bin"] == payload
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_parser_accepts_split_large_on_init_and_sync():
    """
    Input:  none
    Output: None
    Details:
        init and sync accept --split-large. The default is off.
    """
    parser = build_parser()
    assert parser.parse_args(["init", "/s", "--split-large"]).split_large is True  # scbc ignore[is-true-false-strict] argparse.Namespace has no bool annotation
    assert parser.parse_args(["sync", "/s", "--split-large"]).split_large is True  # scbc ignore[is-true-false-strict] argparse.Namespace has no bool annotation
    assert parser.parse_args(["sync", "/s"]).split_large is False  # scbc ignore[is-true-false-strict] argparse.Namespace has no bool annotation


@pytest.mark.usefixtures("compression_on")
def test_staging_records_lzma_for_compressible_file(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A compressible file through ISOBackend records codec lzma and a
        stored size smaller than the raw source.
    """
    source = tmp_path / "src"
    source.mkdir()
    raw = b"hello world, this is archive text.\n" * 3000
    (source / "doc.txt").write_bytes(raw)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "test.iso", disc_size=10_000_000),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        assert len(chunks) == 1
        staging, session_n, _is_new = chunks[0]
        manifest = read_manifest(
            staging / f"session_{session_n:03d}" / "manifest.json"
        )
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        stored = (staging / entry.file).read_bytes()
        assert entry.codec == "lzma"
        assert entry.original_size == len(raw)
        assert len(stored) < len(raw)
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)



def test_disc_backend_orders_batch_by_lba(monkeypatch):
    """A batch of small files is extracted in ascending LBA order."""
    from oddarchiver import disc as disc_module

    backend = disc_module.DiscBackend("/dev/sr0")
    backend._extents_cache = {
        "a": (900, 10), "b": (100, 10), "c": (500, 10),
        "d": (300, 10), "e": (700, 10), "f": (200, 10),
    }
    paths = ["a", "b", "c", "d", "e", "f"]
    assert backend._order_paths_by_lba(paths) == ["b", "f", "d", "c", "e", "a"]


def test_disc_backend_keeps_short_batch_order(monkeypatch):
    """Fewer than the threshold: no listing call, request order preserved."""
    from oddarchiver import disc as disc_module

    backend = disc_module.DiscBackend("/dev/sr0")

    def _boom():
        raise AssertionError("listing call is not worth it for a short batch")

    monkeypatch.setattr(backend, "list_file_extents", _boom)
    assert backend._order_paths_by_lba(["z", "a", "m", "b", "y"]) == ["z", "a", "m", "b", "y"]


def test_disc_backend_unknown_paths_sort_last(monkeypatch):
    """Paths absent from the listing keep request order after known extents."""
    from oddarchiver import disc as disc_module

    backend = disc_module.DiscBackend("/dev/sr0")
    backend._extents_cache = {
        "a": (900, 10), "b": (100, 10), "c": (500, 10),
        "d": (300, 10), "e": (700, 10), "f": (200, 10),
    }
    paths = ["x", "a", "y", "b", "c", "d", "e", "f"]
    assert backend._order_paths_by_lba(paths) == ["b", "f", "d", "c", "e", "a", "x", "y"]


def test_parse_report_lba_extents_reads_lba_and_size():
    """The extent parser returns start LBA and file size per path."""
    from oddarchiver.disc import _parse_report_lba_extents

    text = (
        "File data lba:  0 ,      567 ,        1 ,      883 , '/session_000/TRANS.TBL'\n"
        "File data lba:  0 ,      568 ,      489 ,  1000107 , '/session_000/full/ab'\n"
    )
    assert _parse_report_lba_extents(text) == {
        "session_000/TRANS.TBL": (567, 883),
        "session_000/full/ab": (568, 1000107),
    }


def test_build_disc_state_drops_incomplete_split_chain():
    """A burn stopped after chunk 0 of 2 must not look up to date."""
    from oddarchiver.manifest import Manifest, ManifestEntry, build_disc_state

    def entry(chunk, count):
        return ManifestEntry(
            path="big.bin", type="full", result_checksum="abc",
            full_size_bytes=100, file=f"session_000/full/blob_{chunk}",
            streamed=True, volume_chunk=chunk, volume_chunk_count=count,
        )

    partial = Manifest(
        version=3, session=0, timestamp="t", source="s", label="L",
        based_on_session=None, encryption={}, entries=[entry(0, 2)],
        deleted=[], manifest_checksum="",
    )
    assert build_disc_state([partial]) == {}

    complete = Manifest(
        version=3, session=1, timestamp="t", source="s", label="L",
        based_on_session=0, encryption={}, entries=[entry(1, 2)],
        deleted=[], manifest_checksum="",
    )
    assert build_disc_state([partial, complete]) == {"big.bin": "abc"}


def test_short_extraction_is_rejected(tmp_path):
    """A truncated blob must not pass as a complete read."""
    from oddarchiver import disc as disc_module

    backend = disc_module.DiscBackend("/dev/sr0")
    backend._extents_cache = {"session_000/full/ab": (0, 1000)}
    dest = tmp_path / "ab"
    dest.write_bytes(b"x" * 400)
    assert backend._extracted_sizes_match([("session_000/full/ab", dest)], "") is False
    assert not dest.exists()

    dest.write_bytes(b"x" * 1000)
    assert backend._extracted_sizes_match([("session_000/full/ab", dest)], "") is True


def test_iso_sync_records_used_and_remaining_bytes(tmp_path, monkeypatch):
    """
    Input:  tmp_path ISO init then sync
    Output: remaining_bytes dropped and used_bytes rose after sync
    """
    import json

    from oddarchiver.cli import dispatch
    from oddarchiver.volume import read_volume_marker
    from tests.helpers import _make_args

    def _disc_row():
        records = list((tmp_path / "state" / "oddarchiver").glob("archive-*.json"))
        assert records
        payload = json.loads(records[0].read_text(encoding="utf-8"))
        marker = read_volume_marker(ISOBackend(iso, disc_size=50 * 2**20))
        assert marker is not None
        return payload["discs"][str(marker.volume_index)]

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"alpha")
    iso = tmp_path / "space.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size="50mb",
    )) == 0
    after_init = _disc_row()
    (source / "b.txt").write_bytes(b"beta")
    assert dispatch(_make_args(
        command="sync", source=str(source), test_iso=str(iso),
        disc_size="50mb",
    )) == 0
    after_sync = _disc_row()
    assert after_sync["used_bytes"] > after_init["used_bytes"]
    assert after_sync["remaining_bytes"] < after_init["remaining_bytes"]



_FILL_DISC = "8mb"
_FILL_DISC_BYTES = 8_000_000


def _fill_argv(*parts: str) -> list[str]:
    """Input: CLI tokens. Output: parsed-ready argv list."""
    return list(parts)


def _fill_dispatch(argv: list[str]) -> int:
    """Input: CLI argv. Output: dispatch exit code."""
    from oddarchiver.cli import dispatch

    return dispatch(build_parser().parse_args(argv))


def _iso_sessions(iso: Path) -> list[int]:
    """Input: ISO path. Output: session numbers on that volume."""
    return ISOBackend(iso, disc_size=_FILL_DISC_BYTES).list_session_numbers()


def _build_two_volume_fill_archive(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """
    Input:  tmp_path, monkeypatch
    Output: source, volume-0 ISO, volume-1 ISO
    Details:
        Volume 0 holds a small file and leftover space. Volume 1 holds a
        file that did not fit on volume 0.
    """
    from tests.helpers import fill

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def _boom(prompt=""):
        raise AssertionError(f"ISOBackend must not call input(): {prompt!r}")

    monkeypatch.setattr("builtins.input", _boom)
    source = tmp_path / "src"
    source.mkdir()
    (source / "small.bin").write_bytes(fill(b"A", 120_000))
    iso = tmp_path / "archive.iso"
    assert _fill_dispatch(_fill_argv(
        "init", str(source), "--test-iso", str(iso), "--disc-size", _FILL_DISC,
    )) == 0
    (source / "overflow.bin").write_bytes(fill(b"B", 6_500_000))
    assert _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(iso), "--disc-size", _FILL_DISC,
        "--no-fill",
    )) == 0
    (source / "spill.bin").write_bytes(fill(b"C", 700_000))
    assert _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(iso), "--disc-size", _FILL_DISC,
        "--no-fill",
    )) == 0
    vol1 = tmp_path / "archive.vol1.iso"
    assert iso.is_file()
    assert vol1.is_file()
    assert 0 in _iso_sessions(iso)
    assert 0 not in _iso_sessions(vol1)
    return source, iso, vol1


def test_fill_sync_writes_new_session_on_oldest_iso(tmp_path, monkeypatch):
    """
    Input:  two ISO volumes, volume 0 with free space, volume 1 newest
    Output: fill session on volume 0 with exact ranges; layout stub on volume 1
    """
    from tests.helpers import fill
    from oddarchiver.volume import compact_session_ranges, read_layout_stub, read_volume_marker

    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    vol1_before = vol1.read_bytes()
    vol1_sessions = _iso_sessions(vol1)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    assert rc == 0
    vol0_sessions = _iso_sessions(iso)
    assert max(vol0_sessions) > max(vol1_sessions)
    assert vol1_sessions == _iso_sessions(vol1)
    assert vol1.read_bytes() != vol1_before
    stub = read_layout_stub(ISOBackend(vol1, disc_size=_FILL_DISC_BYTES), None)
    assert stub is not None
    marker = read_volume_marker(ISOBackend(iso, disc_size=_FILL_DISC_BYTES))
    assert marker is not None
    assert marker.session_ranges == compact_session_ranges(set(vol0_sessions))



def test_no_fill_sync_appends_to_inserted_newest_iso(tmp_path, monkeypatch):
    """
    Input:  two ISO volumes and --no-fill
    Output: the new session lands on volume 1 and volume 0 is unchanged
    """
    from tests.helpers import fill

    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    vol0_before = iso.read_bytes()
    vol0_sessions = _iso_sessions(iso)
    (source / "newest.bin").write_bytes(fill(b"D", 40_000))
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--no-fill",
    ))
    assert rc == 0
    assert _iso_sessions(iso) == vol0_sessions
    assert iso.read_bytes() == vol0_before
    assert max(_iso_sessions(vol1)) > max(vol0_sessions)


def test_fill_blob_too_big_for_old_volume_goes_to_newest(tmp_path, monkeypatch):
    """
    Input:  two ISO volumes and a blob larger than leftover space on volume 0
    Output: the session appends to the inserted newest ISO
    """
    from tests.helpers import fill

    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    vol0_sessions = _iso_sessions(iso)
    vol1_sessions = _iso_sessions(vol1)
    remaining = ISOBackend(iso, disc_size=_FILL_DISC_BYTES).mediainfo().remaining_bytes
    (source / "too-big.bin").write_bytes(fill(b"E", remaining + 200_000))
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    assert rc == 0
    assert _iso_sessions(iso) == vol0_sessions
    assert max(_iso_sessions(vol1)) > max(vol1_sessions)


def test_fill_skips_missing_iso_sibling(tmp_path, monkeypatch):
    """
    Input:  volume 1 with volume 0 ISO removed
    Output: fill skips the missing sibling and appends to volume 1
    """
    from tests.helpers import fill

    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    iso.unlink()
    sessions_d = iso.with_suffix(".d")
    if sessions_d.exists():
        shutil.rmtree(sessions_d)
    vol1_sessions = _iso_sessions(vol1)
    (source / "after-skip.bin").write_bytes(fill(b"F", 20_000))
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    assert rc == 0
    assert max(_iso_sessions(vol1)) > max(vol1_sessions)


