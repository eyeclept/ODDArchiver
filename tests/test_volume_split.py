"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for single-file splitting across physical volumes. Covers
    cap-sized packing of several chunks per bin, restore concatenation,
    shrink transition, recovery-mode missing policy, checksum verify,
    and gated GB-scale fixtures.
"""
# Imports
import hashlib
import logging
import math
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.catalog import build_archive_catalog
from oddarchiver.cli import _volume_iso_path, build_parser, dispatch
from oddarchiver.crypto import KeyfileCrypto, NullCrypto, blob_aad, generate_keyfile
from oddarchiver.compress import decode_payload
from oddarchiver.disc import DVD_DL_DISC_BYTES, DVD_SL_DISC_BYTES, ISOBackend
from oddarchiver.filehash import hash_file
from oddarchiver.manifest import load_session_manifests, read_manifest, write_manifest
from oddarchiver.restore import restore
from oddarchiver.restore_plan import restore_archive
from oddarchiver.restore_state import load_restore_state
from oddarchiver.session import (
    SPACE_SAFETY_MARGIN,
    _pack_blobs,
    _PreparedBlob,
    _volume_split_lengths,
    build_staging,
)
from oddarchiver.verify import verify
from oddarchiver.volume import (
    VolumeMarker, build_volume_baseline, read_volume_marker,
    write_volume_marker,
)
from tests.helpers import _write_prior_baseline, fill

# Globals
_DISC = 20_000
_BUDGET = int(_DISC * SPACE_SAFETY_MARGIN)  # 19000
_SPLIT_BYTES = 50_000  # 19000 + 19000 + 12000 = 3 chunks
_SMALL_BYTES = 100
_ARCHIVE_ID = "test-archive-vsplit"
_CREATED_AT = "2026-09-20T00:00:00Z"
_crypto = NullCrypto()
logger = logging.getLogger(__name__)
_log = logger

ROCKY_ISO = Path("/HDD/iso/Rocky-10.0-x86_64-dvd1.iso")
SYNTH_BIN = Path(
    "/HDD/eyeclept/oddarchiver-test-fixtures/oversized_split_test.bin"
)
_GATED_WORK = Path("/HDD/eyeclept/oddarchiver-test-fixtures/section30-work")
_ROCKY_PROFILE = ("rocky.iso", "rocky-archive.iso", "dvd-sl", "ROCKY", DVD_SL_DISC_BYTES)
_SYNTH_PROFILE = ("payload.bin", "synth-archive.iso", "dvd-dl", "SYNTH", DVD_DL_DISC_BYTES)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


# Functions


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    """Input: file path. Output: SHA-256 hex digest."""
    return hash_file(path, 1024 * 1024).hexdigest()


def _make_cache() -> MagicMock:
    """Return a mock CacheManager unused by all-new-file tests."""
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""
    return cache


def _blob(
    rel_path: str,
    encoded_size: int,
    volume_chunk: int | None = None,
    volume_chunk_count: int | None = None,
) -> _PreparedBlob:
    """Build a dummy prepared blob for packing-unit tests."""
    return _PreparedBlob(
        rel_path=rel_path,
        kind="full",
        blob=b"",
        result_checksum="x",
        full_size_bytes=encoded_size,
        encoded_size=encoded_size,
        streamed=True,
        volume_chunk=volume_chunk,
        volume_chunk_count=volume_chunk_count,
    )


def _dispatch(argv: list[str]) -> int:
    """Parse argv and dispatch, matching a real CLI invocation."""
    return dispatch(build_parser().parse_args(argv))


def test_volume_split_lengths_chunk0_uses_active_budget():
    """
    Input:  none
    Output: None
    Details:
        Chunk 0 takes min(remaining, first_budget, BLOB_SIZE_CAP).
        Middle chunks take min(full_budget, BLOB_SIZE_CAP).
        The last chunk takes the remainder.
    """
    lengths = _volume_split_lengths(50_000, 19_000, 19_000)
    assert lengths == [19_000, 19_000, 12_000]
    lengths = _volume_split_lengths(50_000, 0, 19_000)
    assert lengths == [19_000, 19_000, 12_000]


def test_volume_split_lengths_caps_each_chunk(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        Each length stays at or under BLOB_SIZE_CAP even when the disc
        budget is larger.
    """
    monkeypatch.setattr("oddarchiver.session.BLOB_SIZE_CAP", 10_000)
    lengths = _volume_split_lengths(50_000, 19_000, 19_000)
    assert lengths == [10_000, 10_000, 10_000, 10_000, 10_000]


def test_pack_blobs_packs_several_split_chunks_per_bin():
    """
    Input:  none
    Output: None
    Details:
        Cap-sized volume-split chunks share a bin when they fit.
        Chunk order stays ascending. A small ordinary blob fills leftover
        space in that bin.
    """
    chunks_of_file = [
        _blob("huge.bin", 5_000, 0, 3),
        _blob("huge.bin", 5_000, 1, 3),
        _blob("huge.bin", 5_000, 2, 3),
    ]
    small = _blob("tiny.txt", 100)
    bins = _pack_blobs(chunks_of_file + [small], first_budget=20_000, full_budget=20_000)
    assert len(bins) == 1
    split = [b for b in bins[0] if b.rel_path == "huge.bin"]
    assert [b.volume_chunk for b in split] == [0, 1, 2]
    assert [b.rel_path for b in bins[0] if b.volume_chunk is None] == ["tiny.txt"]


def test_pack_blobs_keeps_chunk_order_across_bins():
    """
    Input:  none
    Output: None
    Details:
        Chunk N never lands in an earlier bin than chunk N-1 when a
        later chunk needs a new bin.
    """
    chunks_of_file = [
        _blob("huge.bin", 8_000, 0, 3),
        _blob("huge.bin", 8_000, 1, 3),
        _blob("huge.bin", 8_000, 2, 3),
    ]
    bins = _pack_blobs(chunks_of_file, first_budget=20_000, full_budget=20_000)
    assert [b.volume_chunk for b in bins[0]] == [0, 1]
    assert [b.volume_chunk for b in bins[1]] == [2]
    seen: list[tuple[int, int]] = []
    for i, bin_items in enumerate(bins):
        for item in bin_items:
            seen.append((item.volume_chunk or 0, i))
    assert [chunk for chunk, _bin in seen] == sorted(chunk for chunk, _bin in seen)
    bins_by_chunk = [bin_i for _chunk, bin_i in seen]
    assert bins_by_chunk == sorted(bins_by_chunk)


def test_pack_blobs_fills_split_tails_to_six_bins():
    """
    Input:  none
    Output: None
    Details:
        Two split files (9486+667) plus 46 ordinary blobs pack into
        six bins. Tails share leftover space. Chunk order holds.
        first_budget <= 0 still emits an empty first bin.
    """
    budget = 9486
    blobs = [
        _blob("a.bin", 9486, 0, 2),
        _blob("a.bin", 667, 1, 2),
        _blob("b.bin", 9486, 0, 2),
        _blob("b.bin", 667, 1, 2),
    ]
    for i in range(46):
        size = 400 + i * 12
        blobs.append(_blob(f"ord_{i:02d}.bin", size))
    total = sum(b.encoded_size for b in blobs)
    expected = math.ceil(total / budget)
    assert expected == 6
    bins = _pack_blobs(blobs, first_budget=budget, full_budget=budget)
    assert len(bins) == expected
    tail_bins = [
        b for b in bins
        if any(
            x.volume_chunk is not None
            and x.volume_chunk == (x.volume_chunk_count or 1) - 1
            for x in b
        )
    ]
    assert len(tail_bins) == 2
    for tb in tail_bins:
        assert any(x.volume_chunk is None for x in tb)
    positions: dict[str, list[int]] = {}
    for i, bin_items in enumerate(bins):
        for item in bin_items:
            if item.volume_chunk is None:
                continue
            positions.setdefault(item.rel_path, []).append(i)
            split_mates = [
                x for x in bin_items
                if x.volume_chunk is not None and x.rel_path != item.rel_path
            ]
            assert split_mates == []
            same = [x for x in bin_items if x.rel_path == item.rel_path]
            assert len(same) == 1
    for rel_path, idxs in positions.items():
        assert idxs == sorted(idxs)
        assert len(idxs) == 2
        assert idxs[1] > idxs[0]

    empty_first = _pack_blobs(blobs, first_budget=0, full_budget=budget)
    assert empty_first[0] == []
    assert len(empty_first) == expected + 1



def _assert_tagged_chunks(chunks: list[tuple[Path, int, bool]], payload: bytes) -> None:
    """Input: staged chunks and source bytes. Output: None; checks split metadata."""
    parts: list[bytes] = []
    count = None
    for i, (staging, session_n, is_new) in enumerate(chunks):
        assert session_n == i
        assert is_new == (i > 0)
        manifest = read_manifest(
            staging / f"session_{session_n:03d}" / "manifest.json"
        )
        assert len(manifest.entries) == 1
        entry = manifest.entries[0]
        assert entry.path == "huge.bin"
        assert entry.type == "full"
        assert entry.streamed
        assert entry.volume_chunk == i
        if count is None:
            count = entry.volume_chunk_count
        assert entry.volume_chunk_count == count
        assert entry.full_size_bytes == _SPLIT_BYTES
        assert entry.result_checksum == _sha256(payload)
        blob = (staging / entry.file).read_bytes()
        assert len(blob) <= _BUDGET
        parts.append(blob)
    assert count == len(chunks)
    assert b"".join(parts) == payload


def test_oversized_file_splits_into_tagged_dedicated_bins(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A 50000-byte file on a 20000-byte disc is over the disc budget
        and under BLOB_SIZE_CAP, so it still splits. Chunks carry
        sequential volume_chunk fields and full_size_bytes equal to the
        whole file on every entry.
    """
    source = tmp_path / "src"
    source.mkdir()
    payload = fill(b"z", _SPLIT_BYTES)
    (source / "huge.bin").write_bytes(payload)

    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "pack.iso", disc_size=_DISC),
        cache=_make_cache(),
        crypto=_crypto,
        _staging_root=tmp_path / "staging",
    )
    try:
        assert len(chunks) >= 2
        _assert_tagged_chunks(chunks, payload)
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)



def test_capped_file_that_fits_disc_packs_several_chunks_per_bin(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A 3 MiB file on a 50 MiB disc with BLOB_SIZE_CAP at 1 MiB splits
        into chunks under the cap. The chunks share one bin. Concatenating
        them in volume_chunk order reproduces the source bytes.
    """
    cap = 1 << 20
    monkeypatch.setattr("oddarchiver.session.BLOB_SIZE_CAP", cap)
    source = tmp_path / "src"
    source.mkdir()
    payload = bytes([(i * 7) % 256 for i in range(3 * cap)])
    (source / "huge.bin").write_bytes(payload)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "pack.iso", disc_size=50 * 2**20),
        cache=_make_cache(),
        crypto=_crypto,
        _staging_root=tmp_path / "staging",
    )
    try:
        assert len(chunks) == 1
        staging, session_n, is_new = chunks[0]
        assert session_n == 0
        assert not is_new
        manifest = read_manifest(
            staging / f"session_{session_n:03d}" / "manifest.json"
        )
        split_entries = [e for e in manifest.entries if e.path == "huge.bin"]
        assert len(split_entries) == 3
        assert [e.volume_chunk for e in split_entries] == [0, 1, 2]
        parts: list[bytes] = []
        for entry in split_entries:
            blob = (staging / entry.file).read_bytes()
            assert len(blob) <= cap
            parts.append(blob)
        assert b"".join(parts) == payload
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_capped_multi_chunk_test_iso_restore_round_trip(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        init --test-iso of a capped multi-chunk file restores byte-exact
        contents. Several chunks share the same disc.
    """
    cap = 1 << 20
    monkeypatch.setattr("oddarchiver.session.BLOB_SIZE_CAP", cap)
    source = tmp_path / "src"
    source.mkdir()
    payload = os.urandom(3 * cap)
    (source / "huge.bin").write_bytes(payload)
    iso = tmp_path / "cap.iso"
    rc = _dispatch([
        "init", str(source), "--test-iso", str(iso),
        "--disc-size", str(50 * 2**20), "--encrypt", "none",
    ])
    assert rc == 0
    dest = tmp_path / "dest"
    rc = _dispatch(["restore", str(dest), "--test-iso", str(iso)])
    assert rc == 0
    assert (dest / "huge.bin").read_bytes() == payload
    assert not list(tmp_path.glob("cap.vol*"))


def _burn_split_archive(
    tmp_path: Path,
    payload: bytes,
    extra: dict[str, bytes] | None = None,
) -> tuple[Path, list[Path]]:
    """
    Input:  tmp_path - pytest temp directory
            payload  - bytes of huge.bin
            extra    - optional extra source files
    Output: (source, iso_paths) for a marked multi-volume split archive
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "huge.bin").write_bytes(payload)
    for name, data in (extra or {}).items():
        (source / name).write_bytes(data)

    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "pack.iso", disc_size=_DISC),
        cache=_make_cache(),
        crypto=_crypto,
        _staging_root=tmp_path / "staging",
    )
    try:
        prior = None
        iso_paths: list[Path] = []
        for i, (staging, session_n, is_new) in enumerate(chunks):
            assert session_n == i
            assert is_new == (i > 0)
            prev_label = "" if i == 0 else f"VOL{i - 1}"
            label = f"VOL{i}"
            marker = VolumeMarker(_ARCHIVE_ID, i, session_n, _CREATED_AT, prev_label)
            _write_prior_baseline(staging, prior, _crypto, marker, session_n)
            write_volume_marker(staging, marker)
            iso = tmp_path / f"vol{i}.iso"
            backend = ISOBackend(iso, disc_size=_DISC)
            backend.init(staging, label=label, expected_session_count=0)
            iso_paths.append(iso)
            manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
            prior = build_volume_baseline(prior, [manifest], marker, label)
        return source, iso_paths
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


class _Acquire:
    image = None

    def __init__(self, paths, order):
        self.paths = paths
        self.order = iter(order)
        self.seen = []

    def acquire(self, needed, catalog, archive_id):
        index = next(self.order)
        assert index in needed
        self.seen.append(index)
        return ISOBackend(self.paths[index], disc_size=_DISC), None, False


def _restore_split(dest, paths, order):
    backend = ISOBackend(paths[-1], disc_size=_DISC)
    marker = read_volume_marker(backend)
    manifests = load_session_manifests(
        backend, _crypto, start_session=marker.first_session, marker=marker,
    )
    catalog = build_archive_catalog(backend, _crypto, manifests, marker)
    acquisition = _Acquire(paths, order)
    result = restore_archive(
        dest, backend, _crypto, catalog, manifests=manifests, acquisition=acquisition,
    )
    return result, acquisition


def _track_blob_reads(monkeypatch, isos: list[Path]) -> dict[int, list[str]]:
    """Input: pytest patcher and ISO paths. Output: per-volume blob reads."""
    iso_index = {path.resolve(): index for index, path in enumerate(isos)}
    blob_reads: dict[int, list[str]] = {index: [] for index in range(len(isos))}
    orig_read = ISOBackend.read_paths
    orig_one = ISOBackend.extract_path_to_file

    def record(self, paths):
        blobs = [path for path in paths if "/full/" in path or "/deltas/" in path]
        if blobs:
            blob_reads[iso_index[self.iso_path.resolve()]].extend(blobs)

    def track_read(self, paths):
        record(self, paths)
        return orig_read(self, paths)

    def track_one(self, path, dest_file):
        record(self, [path])
        return orig_one(self, path, dest_file)

    monkeypatch.setattr(ISOBackend, "read_paths", track_read)
    if hasattr(ISOBackend, "extract_paths"):
        orig_many = ISOBackend.extract_paths

        def track_many(self, paths, *args, **kwargs):
            record(self, paths)
            return orig_many(self, paths, *args, **kwargs)

        monkeypatch.setattr(ISOBackend, "extract_paths", track_many)
    else:
        monkeypatch.setattr(ISOBackend, "extract_path_to_file", track_one)
    return blob_reads


def _assert_single_read_per_volume(
    isos: list[Path], blob_reads: dict[int, list[str]],
) -> None:
    """Input: ISO list and recorded paths. Output: None; checks each volume."""
    for index, _iso in enumerate(isos):
        blobs = blob_reads[index]
        assert blobs, f"volume {index} had no blob reads"
        assert len(blobs) == len(set(blobs)), f"volume {index} reread blobs: {blobs}"
        assert len(set(blobs)) == 1


def test_split_file_restore_round_trip_three_volumes(tmp_path, monkeypatch):
    """Collect split chunks in mixed disc order and reconstruct the file."""
    payload = bytes([(i * 3 + 5) % 256 for i in range(_SPLIT_BYTES)])
    _source, isos = _burn_split_archive(tmp_path, payload)
    assert len(isos) >= 2
    dest = tmp_path / "dest"
    order = list(range(len(isos) - 1))
    order.reverse()
    blob_reads = _track_blob_reads(monkeypatch, isos)
    result, _acquisition = _restore_split(dest, isos, order)
    assert result.failures == 0
    assert (dest / "huge.bin").read_bytes() == payload
    assert _sha256_path(dest / "huge.bin") == _sha256(payload)
    state = load_restore_state(dest, _ARCHIVE_ID)
    assert state is not None
    assert set(state.read_discs) == set(range(len(isos)))
    _assert_single_read_per_volume(isos, blob_reads)


def test_split_file_independent_chunk_decrypt(tmp_path):
    """
    Input: temporary source and keyfile
    Output: None
    Details: Every encrypted chunk decrypts on its own to its source range.
    """
    source = tmp_path / "src"
    source.mkdir()
    payload = fill(b"Z", _SPLIT_BYTES)
    (source / "huge.bin").write_bytes(payload)
    keyfile = tmp_path / "archive.key"
    generate_keyfile(str(keyfile))
    crypto = KeyfileCrypto(str(keyfile))
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "pack.iso", disc_size=_DISC),
        cache=_make_cache(),
        crypto=crypto,
        _staging_root=tmp_path / "staging",
    )
    try:
        entries = []
        plains: list[bytes] = []
        for staging, session_n, _is_new in chunks:
            manifest = read_manifest(
                staging / f"session_{session_n:03d}" / "manifest.json", crypto=crypto,
            )
            entry = manifest.entries[0]
            entries.append(entry)
            stored = tmp_path / f"stored_{entry.volume_chunk}"
            crypto.decrypt_file(
                staging / entry.file, stored, aad=blob_aad(entry),
            )
            plains.append(stored.read_bytes())
        assert all(plains)
        assert decode_payload(b"".join(plains), entries[0].codec) == payload
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def _remove_staged(chunks: list[tuple[Path, int, bool]]) -> None:
    """Input: staged chunks. Output: None; removes each staging directory."""
    for staging, _session_n, _is_new in chunks:
        shutil.rmtree(staging, ignore_errors=True)


def _copy_staged_sessions(
    chunks: list[tuple[Path, int, bool]], combined: Path,
) -> None:
    """Input: staged sessions and destination. Output: None; copies sessions."""
    for staging, session_n, _is_new in chunks:
        shutil.copytree(
            staging / f"session_{session_n:03d}",
            combined / f"session_{session_n:03d}",
        )


def _assert_shrunk_chunk(
    sync_chunks: list[tuple[Path, int, bool]], shrunk: bytes, backend: ISOBackend,
) -> None:
    """Input: sync chunks, new bytes, backend. Output: None; checks and appends."""
    assert len(sync_chunks) == 1
    staging, session_n, is_new = sync_chunks[0]
    assert not is_new
    manifest = read_manifest(
        staging / f"session_{session_n:03d}" / "manifest.json"
    )
    entry = {e.path: e for e in manifest.entries}["huge.bin"]
    assert entry.volume_chunk is None
    assert entry.volume_chunk_count is None
    assert entry.streamed
    assert entry.full_size_bytes == len(shrunk)
    backend.append(
        staging,
        label="SHRINK",
        expected_session_count=backend.mediainfo().session_count,
    )


def test_split_file_shrinks_to_single_blob(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A previously split file that later fits one disc re-encodes with
        no volume_chunk fields. Restore of the new encoding returns the
        shrunk contents.
    """
    import oddarchiver.crypto as crypto_mod
    import oddarchiver.restore as restore_mod
    import oddarchiver.session as session_mod
    import oddarchiver.verify as verify_mod
    from oddarchiver.manifest import build_disc_origin, build_disc_state

    monkeypatch.setattr(crypto_mod, "STREAM_CHUNK_SIZE", 100)
    monkeypatch.setattr(session_mod, "STREAM_CHUNK_SIZE", 100)
    monkeypatch.setattr(restore_mod, "HASH_CHUNK_SIZE", 100)
    monkeypatch.setattr(verify_mod, "STREAM_CHUNK_SIZE", 100)

    source = tmp_path / "src"
    source.mkdir()
    large = fill(b"L", _SPLIT_BYTES)
    (source / "huge.bin").write_bytes(large)

    pack_backend = ISOBackend(tmp_path / "pack.iso", disc_size=_DISC)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=pack_backend,
        cache=_make_cache(),
        crypto=_crypto,
        _staging_root=tmp_path / "staging0",
    )
    combined = tmp_path / "combined0"
    combined.mkdir()
    try:
        assert len(chunks) >= 2
        _copy_staged_sessions(chunks, combined)
    finally:
        _remove_staged(chunks)

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=2_000_000)
    backend.init(combined, label="SHRINK", expected_session_count=0)

    shrunk = fill(b"S", 8_000)
    (source / "huge.bin").write_bytes(shrunk)
    manifests = load_session_manifests(backend, crypto=_crypto)
    disc_state = build_disc_state(manifests)
    disc_origin = build_disc_origin(manifests)
    sync_chunks = build_staging(
        session_n=len(manifests),
        source=source,
        disc_state=disc_state,
        disc_origin=disc_origin,
        backend=backend,
        cache=_make_cache(),
        crypto=_crypto,
        _staging_root=tmp_path / "staging1",
    )
    try:
        _assert_shrunk_chunk(sync_chunks, shrunk, backend)
    finally:
        _remove_staged(chunks)

    dest = tmp_path / "dest"
    restored, failures = restore(dest, backend, _crypto)
    assert failures == 0
    assert restored >= 1
    assert (dest / "huge.bin").read_bytes() == shrunk






def test_skip_volume_on_split_chunk_reports_missing(tmp_path):
    """Missing a split chunk leaves no partial file, but restores other files.

    Restore checks the manifests of every other volume once; the skipped
    volume 1 is never requested.
    """
    payload = fill(b"H", _SPLIT_BYTES)
    extra = {"other.txt": fill(b"O", _SMALL_BYTES)}
    _source, isos = _burn_split_archive(tmp_path, payload, extra=extra)
    assert len(isos) >= 3
    backend = ISOBackend(isos[-1], disc_size=_DISC)
    marker = read_volume_marker(backend)
    manifests = load_session_manifests(
        backend, _crypto, start_session=marker.first_session, marker=marker,
    )
    catalog = build_archive_catalog(backend, _crypto, manifests, marker)
    checks = [index for index in range(len(isos) - 1) if index != 1]
    acquire = _Acquire(isos, checks)
    result = restore_archive(
        tmp_path / "dest", backend, _crypto, catalog,
        skip_volumes=[1], manifests=manifests, acquisition=acquire,
    )
    assert sorted(acquire.seen) == checks
    assert result.failures == 0
    assert result.stale == []
    assert result.missing == ["huge.bin"]
    assert not (tmp_path / "dest" / "huge.bin").exists()
    assert (tmp_path / "dest" / "other.txt").read_bytes() == extra["other.txt"]

def test_verify_checksum_catches_tampered_split_chunk(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Checksum-level verify of a complete volume-split group passes when
        the chunks are intact and reports a checksum mismatch when one
        chunk is tampered.
    """
    source = tmp_path / "src"
    source.mkdir()
    payload = fill(b"V", _SPLIT_BYTES)
    (source / "huge.bin").write_bytes(payload)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "pack.iso", disc_size=_DISC),
        cache=_make_cache(),
        crypto=_crypto,
        _staging_root=tmp_path / "staging",
    )
    combined = tmp_path / "combined"
    combined.mkdir()
    try:
        _copy_staged_sessions(chunks, combined)
    finally:
        _remove_staged(chunks)

    iso = tmp_path / "all.iso"
    backend = ISOBackend(iso, disc_size=_DISC * 4)
    backend.init(combined, label="VERIFY", expected_session_count=0)
    assert verify(backend, _crypto, level="checksum")

    manifests = load_session_manifests(backend, crypto=_crypto)
    split_entries = [
        e for m in manifests for e in m.entries if e.volume_chunk == 1
    ]
    assert split_entries
    blob = backend._sessions_root / split_entries[0].file
    original = blob.read_bytes()
    blob.write_bytes(b"\x00" * len(original))
    with pytest.raises(SystemExit) as exc:
        verify(backend, _crypto, level="checksum")
    assert exc.value.code == 1


def _gated_work(tmp_path, monkeypatch) -> Path:
    """
    Input:  tmp_path    - pytest temp path, used only for a unique name
            monkeypatch - points XDG dirs at the per-test work directory
    Output: work directory on /HDD large enough for GB-scale staging
    Details:
        Each call gets its own subdirectory so two gated tests, or two
        concurrent pytest processes, cannot rmtree each other's files.
        Still lives under _GATED_WORK so staging is not on a small tmpfs.
    """
    work = _GATED_WORK / f"{tmp_path.name}-{os.getpid()}"
    work.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(work / "xdg_runtime"))
    monkeypatch.setenv("XDG_STATE_HOME", str(work / "xdg_state"))
    monkeypatch.setattr(
        "oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(work / "cache"),
    )
    return work



def _expected_split_count(file_size: int, disc_size: int) -> int:
    """Return the number of volume chunks for file_size on disc_size."""
    budget = int(disc_size * SPACE_SAFETY_MARGIN)
    return len(_volume_split_lengths(file_size, budget, budget))


def _assert_gated_round_trip(
    work: Path,
    fixture: Path,
    profile: tuple[str, str, str, str, int],
) -> None:
    """Input: work path, source fixture and archive profile. Output: None; checks restore."""
    name, archive_name, disc_option, label, disc_bytes = profile
    source = work / "src"
    source.mkdir()
    (source / name).symlink_to(fixture)
    iso0 = work / archive_name
    rc = _dispatch([
        "init", str(source), "--test-iso", str(iso0),
        "--encrypt", "none", "--disc-size", disc_option, "--label", label,
    ])
    assert rc == 0
    expected = _expected_split_count(fixture.stat().st_size, disc_bytes)
    vol_paths = [iso0] + [
        _volume_iso_path(iso0, i) for i in range(1, expected)
    ]
    for path in vol_paths:
        assert path.is_file(), f"missing volume ISO {path}"
    dest = work / "dest"
    assert _dispatch([
        "restore", str(dest), "--test-iso", str(vol_paths[-1]), "--all-disks",
    ]) == 0
    restored = dest / name
    assert restored.is_file()
    assert restored.stat().st_size == fixture.stat().st_size
    assert _sha256_path(restored) == _sha256_path(fixture)


def _run_gated_round_trip(tmp_path, monkeypatch, fixture: Path, profile: tuple[str, str, str, str, int]) -> None:
    """Input: pytest paths, fixture and profile. Output: None; cleans temporary work."""
    work = _gated_work(tmp_path, monkeypatch)
    try:
        _assert_gated_round_trip(work, fixture, profile)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not ROCKY_ISO.is_file(), reason="Rocky DVD ISO fixture not present")
def test_rocky_iso_splits_across_dvd_sl_and_restores(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        The Rocky 10 DVD ISO exceeds DVD_SL_DISC_BYTES and fits
        DVD_DL_DISC_BYTES. init with --disc-size dvd-sl splits it across
        the expected volume count. Disc-aware restore matches the
        original ISO byte-for-byte.
    """
    _run_gated_round_trip(tmp_path, monkeypatch, ROCKY_ISO, _ROCKY_PROFILE)


@pytest.mark.skipif(not SYNTH_BIN.is_file(), reason="9.5GB synthetic fixture not present")
def test_synth_9_5gb_splits_across_dvd_dl_and_restores(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        The 9.5 GB high-entropy fixture exceeds DVD_DL_DISC_BYTES. init
        with --disc-size dvd-dl splits it across the expected volume
        count. Disc-aware restore matches the original file
        byte-for-byte.
    """
    _run_gated_round_trip(tmp_path, monkeypatch, SYNTH_BIN, _SYNTH_PROFILE)
