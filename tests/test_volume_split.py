"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for single-file splitting across physical volumes. Covers
    dedicated-bin packing, restore concatenation, shrink transition,
    recovery-mode missing policy, checksum verify, and gated GB-scale
    fixtures.
"""
# Imports
import hashlib
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cli import _volume_iso_path, build_parser, dispatch
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import DVD_DL_DISC_BYTES, DVD_SL_DISC_BYTES, ISOBackend
from oddarchiver.manifest import load_session_manifests, read_manifest
from oddarchiver.restore import restore
from oddarchiver.restore_state import load_restore_state
from oddarchiver.session import (
    SPACE_SAFETY_MARGIN,
    _pack_blobs,
    _PreparedBlob,
    _volume_split_lengths,
    build_staging,
)
from oddarchiver.verify import verify
from oddarchiver.volume import VolumeMarker, write_volume_marker

# Globals
_DISC = 20_000
_BUDGET = int(_DISC * SPACE_SAFETY_MARGIN)  # 19000
_SPLIT_BYTES = 50_000  # 19000 + 19000 + 12000 = 3 chunks
_SMALL_BYTES = 100
_ARCHIVE_ID = "test-archive-vsplit"
_LABELS = ("VOL0", "VOL1", "VOL2", "VOL3")
_CREATED_AT = "2026-09-20T00:00:00Z"
_crypto = NullCrypto()

ROCKY_ISO = Path("/HDD/iso/Rocky-10.0-x86_64-dvd1.iso")
SYNTH_BIN = Path(
    "/HDD/eyeclept/oddarchiver-test-fixtures/oversized_split_test.bin"
)
_GATED_WORK = Path("/HDD/eyeclept/oddarchiver-test-fixtures/section30-work")


# Functions


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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
        Chunk 0 takes min(remaining, first_budget). Middle chunks take
        full_budget. The last chunk takes the remainder.
    """
    lengths = _volume_split_lengths(50_000, 19_000, 19_000)
    assert lengths == [19_000, 19_000, 12_000]
    lengths = _volume_split_lengths(50_000, 0, 19_000)
    assert lengths == [19_000, 19_000, 12_000]


def test_pack_blobs_gives_each_split_chunk_a_dedicated_bin():
    """
    Input:  none
    Output: None
    Details:
        Three volume-split chunks of one file each land in their own bin
        in ascending volume_chunk order. A small ordinary blob packs
        after them, not beside a split chunk.
    """
    chunks_of_file = [
        _blob("huge.bin", 19_000, 0, 3),
        _blob("huge.bin", 19_000, 1, 3),
        _blob("huge.bin", 12_000, 2, 3),
    ]
    small = _blob("tiny.txt", 100)
    bins = _pack_blobs(chunks_of_file + [small], first_budget=19_000, full_budget=19_000)
    assert len(bins) == 4
    for i in range(3):
        assert len(bins[i]) == 1
        assert bins[i][0].rel_path == "huge.bin"
        assert bins[i][0].volume_chunk == i
    assert [b.rel_path for b in bins[3]] == ["tiny.txt"]


def test_oversized_file_splits_into_tagged_dedicated_bins(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A 50000-byte file on a 20000-byte disc splits into 3 dedicated
        bins with volume_chunk 0..2, volume_chunk_count 3, and
        full_size_bytes equal to the whole file on every entry.
    """
    source = tmp_path / "src"
    source.mkdir()
    payload = bytes([(i * 7) % 256 for i in range(_SPLIT_BYTES)])
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
        assert len(chunks) == 3
        parts: list[bytes] = []
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
            assert entry.streamed is True
            assert entry.volume_chunk == i
            assert entry.volume_chunk_count == 3
            assert entry.full_size_bytes == _SPLIT_BYTES
            assert entry.result_checksum == _sha256(payload)
            blob = (staging / entry.file).read_bytes()
            if i < 2:
                assert len(blob) == _BUDGET
            else:
                assert len(blob) == _SPLIT_BYTES - 2 * _BUDGET
            parts.append(blob)
        assert b"".join(parts) == payload
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


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
        iso_paths: list[Path] = []
        for i, (staging, session_n, is_new) in enumerate(chunks):
            assert session_n == i
            assert is_new == (i > 0)
            prev_label = "" if i == 0 else _LABELS[i - 1]
            write_volume_marker(
                staging,
                VolumeMarker(
                    archive_id=_ARCHIVE_ID,
                    volume_index=i,
                    first_session=session_n,
                    created_at=_CREATED_AT,
                    previous_volume_label=prev_label,
                ),
            )
            iso = tmp_path / f"vol{i}.iso"
            backend = ISOBackend(iso, disc_size=_DISC)
            backend.init(staging, label=_LABELS[i], expected_session_count=0)
            iso_paths.append(iso)
        return source, iso_paths
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_split_file_restore_round_trip_three_volumes(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Sync a 3-chunk split file onto 3 marked ISOBackend volumes.
        Oldest-first restore reconstructs huge.bin byte-for-byte.
    """
    payload = bytes([(i * 3 + 5) % 256 for i in range(_SPLIT_BYTES)])
    source, isos = _burn_split_archive(tmp_path, payload)
    assert len(isos) == 3
    dest = tmp_path / "dest"
    for iso in isos:
        result = restore(dest, ISOBackend(iso, disc_size=_DISC), _crypto)
        assert result.failures == 0
    assert (dest / "huge.bin").read_bytes() == payload
    assert _sha256_path(dest / "huge.bin") == _sha256(payload)
    state = load_restore_state(dest, _ARCHIVE_ID)
    assert state is not None
    assert state.applied_volumes == [0, 1, 2]


def test_split_file_independent_chunk_decrypt(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Each range-encrypted chunk decrypts via decrypt_file with no
        extra params. Concatenating those plaintexts equals the source.
    """
    source = tmp_path / "src"
    source.mkdir()
    payload = b"Z" * _SPLIT_BYTES
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
        plains: list[bytes] = []
        for i, (staging, session_n, _is_new) in enumerate(chunks):
            manifest = read_manifest(
                staging / f"session_{session_n:03d}" / "manifest.json"
            )
            blob = staging / manifest.entries[0].file
            out = tmp_path / f"plain_{i}"
            _crypto.decrypt_file(blob, out)
            plains.append(out.read_bytes())
        assert b"".join(plains) == payload
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


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
    monkeypatch.setattr(restore_mod, "STREAM_CHUNK_SIZE", 100)
    monkeypatch.setattr(verify_mod, "STREAM_CHUNK_SIZE", 100)

    source = tmp_path / "src"
    source.mkdir()
    large = b"L" * _SPLIT_BYTES
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
        for staging, session_n, _is_new in chunks:
            shutil.copytree(
                staging / f"session_{session_n:03d}",
                combined / f"session_{session_n:03d}",
            )
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=2_000_000)
    backend.init(combined, label="SHRINK", expected_session_count=0)

    shrunk = b"S" * 8_000
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
        assert len(sync_chunks) == 1
        staging, session_n, is_new = sync_chunks[0]
        assert is_new is False
        manifest = read_manifest(
            staging / f"session_{session_n:03d}" / "manifest.json"
        )
        entry = {e.path: e for e in manifest.entries}["huge.bin"]
        assert entry.volume_chunk is None
        assert entry.volume_chunk_count is None
        assert entry.streamed is True
        assert entry.full_size_bytes == len(shrunk)
        backend.append(
            staging,
            label="SHRINK",
            expected_session_count=backend.mediainfo().session_count,
        )
    finally:
        for staging, _session_n, _is_new in sync_chunks:
            shutil.rmtree(staging, ignore_errors=True)

    dest = tmp_path / "dest"
    restored, failures = restore(dest, backend, _crypto)
    assert failures == 0
    assert restored >= 1
    assert (dest / "huge.bin").read_bytes() == shrunk






def test_skip_volume_on_split_chunk_reports_missing(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        --skip-volume on a volume that holds one chunk of a split file
        reports that file missing, never stale, and does not write a
        partial dest file. A small file on a later volume still restores.
    """
    payload = b"H" * _SPLIT_BYTES
    extra = {"other.txt": b"O" * _SMALL_BYTES}
    _source, isos = _burn_split_archive(tmp_path, payload, extra=extra)
    assert len(isos) == 4
    dest = tmp_path / "dest"
    skip = [1]
    r0 = restore(
        dest, ISOBackend(isos[0], disc_size=_DISC), _crypto, skip_volumes=skip,
    )
    r2 = restore(
        dest, ISOBackend(isos[2], disc_size=_DISC), _crypto, skip_volumes=skip,
    )
    r3 = restore(
        dest, ISOBackend(isos[3], disc_size=_DISC), _crypto, skip_volumes=skip,
    )
    assert r0.failures == 0
    assert r2.failures == 0
    assert r3.failures == 0
    assert "huge.bin" in r0.missing
    assert r0.stale == []
    assert "huge.bin" in r2.missing
    assert r2.stale == []
    assert r3.stale == []
    assert not (dest / "huge.bin").exists()
    assert (dest / "other.txt").read_bytes() == extra["other.txt"]


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
    payload = b"V" * _SPLIT_BYTES
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
        for staging, session_n, _is_new in chunks:
            shutil.copytree(
                staging / f"session_{session_n:03d}",
                combined / f"session_{session_n:03d}",
            )
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)

    iso = tmp_path / "all.iso"
    backend = ISOBackend(iso, disc_size=_DISC * 4)
    backend.init(combined, label="VERIFY", expected_session_count=0)
    assert verify(backend, _crypto, level="checksum") is True

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
    return work



def _expected_split_count(file_size: int, disc_size: int) -> int:
    """Return the number of volume chunks for file_size on disc_size."""
    budget = int(disc_size * SPACE_SAFETY_MARGIN)
    return len(_volume_split_lengths(file_size, budget, budget))


@pytest.mark.skipif(not ROCKY_ISO.is_file(), reason="Rocky DVD ISO fixture not present")
def test_rocky_iso_splits_across_dvd_sl_and_restores(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        The Rocky 10 DVD ISO exceeds DVD_SL_DISC_BYTES and fits
        DVD_DL_DISC_BYTES. init with --disc-size dvd-sl splits it across
        the expected volume count. Oldest-first restore matches the
        original ISO byte-for-byte.
    """
    work = _gated_work(tmp_path, monkeypatch)
    try:
        source = work / "src"
        source.mkdir()
        (source / "rocky.iso").symlink_to(ROCKY_ISO)
        iso0 = work / "rocky-archive.iso"
        rc = _dispatch([
            "init", str(source), "--test-iso", str(iso0),
            "--encrypt", "none", "--disc-size", "dvd-sl", "--label", "ROCKY",
        ])
        assert rc == 0
        expected = _expected_split_count(ROCKY_ISO.stat().st_size, DVD_SL_DISC_BYTES)
        vol_paths = [iso0] + [
            _volume_iso_path(iso0, i) for i in range(1, expected)
        ]
        for path in vol_paths:
            assert path.is_file(), f"missing volume ISO {path}"
        dest = work / "dest"
        for path in vol_paths:
            result = restore(
                dest, ISOBackend(path, disc_size=DVD_SL_DISC_BYTES), _crypto,
            )
            assert result.failures == 0
        restored = dest / "rocky.iso"
        assert restored.is_file()
        assert restored.stat().st_size == ROCKY_ISO.stat().st_size
        assert _sha256_path(restored) == _sha256_path(ROCKY_ISO)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not SYNTH_BIN.is_file(), reason="9.5GB synthetic fixture not present")
def test_synth_9_5gb_splits_across_dvd_dl_and_restores(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        The 9.5 GB high-entropy fixture exceeds DVD_DL_DISC_BYTES. init
        with --disc-size dvd-dl splits it across the expected volume
        count. Oldest-first restore matches the original file
        byte-for-byte.
    """
    work = _gated_work(tmp_path, monkeypatch)
    try:
        source = work / "src"
        source.mkdir()
        (source / "payload.bin").symlink_to(SYNTH_BIN)
        iso0 = work / "synth-archive.iso"
        rc = _dispatch([
            "init", str(source), "--test-iso", str(iso0),
            "--encrypt", "none", "--disc-size", "dvd-dl", "--label", "SYNTH",
        ])
        assert rc == 0
        expected = _expected_split_count(SYNTH_BIN.stat().st_size, DVD_DL_DISC_BYTES)
        vol_paths = [iso0] + [
            _volume_iso_path(iso0, i) for i in range(1, expected)
        ]
        for path in vol_paths:
            assert path.is_file(), f"missing volume ISO {path}"
        dest = work / "dest"
        for path in vol_paths:
            result = restore(
                dest, ISOBackend(path, disc_size=DVD_DL_DISC_BYTES), _crypto,
            )
            assert result.failures == 0
        restored = dest / "payload.bin"
        assert restored.is_file()
        assert restored.stat().st_size == SYNTH_BIN.stat().st_size
        assert _sha256_path(restored) == _sha256_path(SYNTH_BIN)
    finally:
        shutil.rmtree(work, ignore_errors=True)
