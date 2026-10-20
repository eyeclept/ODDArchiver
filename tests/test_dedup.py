"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for content-addressed dedup (TODO 35). Moved and duplicate
    files reuse an existing full blob instead of encoding a new copy.
"""
# Imports
import hashlib
import shutil
from pathlib import Path
from unittest.mock import MagicMock

from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import DiscInfo, ISOBackend
from oddarchiver.manifest import (
    build_disc_origin,
    build_disc_state,
    load_session_manifests,
    read_manifest,
)
from oddarchiver.restore import restore
from oddarchiver.session import (
    _pack_blobs,
    _PreparedBlob,
    _staging_bytes,
    build_staging,
)
from oddarchiver.verify import verify

# Globals
SMALL_DISC = 20 * 2**20  # 20 MiB - fast ISO builds
_PAYLOAD = b"shared-payload-bytes" * 200  # ~4 KB, bigger than manifest overhead
_UNIQUE_A = b"unique-keep-content"
_UNIQUE_B = b"unique-second-session"
_crypto = NullCrypto()


# Functions


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_cache() -> MagicMock:
    """Return a mock CacheManager unused by full-blob and dedup paths."""
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""
    return cache


def _make_backend(remaining_bytes: int) -> MagicMock:
    """Return a mock BurnBackend with a fixed remaining_bytes."""
    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=1,
        remaining_bytes=remaining_bytes,
        used_bytes=0,
        label="TEST",
    )
    return backend


def _session_blobs(staging: Path, session_n: int) -> list[Path]:
    """Return physical blob files under session_NNN/full and deltas."""
    session_dir = staging / f"session_{session_n:03d}"
    blobs: list[Path] = []
    for sub in ("full", "deltas"):
        d = session_dir / sub
        if d.is_dir():
            blobs.extend(sorted(p for p in d.iterdir() if p.is_file()))
    return blobs


def _read_session_manifest(staging: Path, session_n: int):
    """Return the Manifest written into staging for session_n."""
    return read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")


def _stage(
    tmp_path: Path,
    session_n: int,
    source: Path,
    backend,
    disc_state: dict[str, str],
    disc_origin: dict[str, int],
    manifests=None,
):
    """
    Input:  tmp_path, session_n, source, backend, disc_state, disc_origin,
            manifests
    Output: chunks from build_staging
    Details:
        Each call uses its own staging root so session numbers do not
        collide with a leftover lockfile.
    """
    return build_staging(
        session_n=session_n,
        source=source,
        disc_state=disc_state,
        disc_origin=disc_origin,
        backend=backend,
        cache=_make_cache(),
        crypto=_crypto,
        _staging_root=tmp_path / f"staging_root_{session_n}",
        manifests=manifests,
    )


def _burn_chunks(backend: ISOBackend, chunks) -> None:
    """Burn every staging chunk via init (session 0) or append."""
    for staging, session_n, _is_new in chunks:
        if session_n == 0:
            backend.init(staging, label="TEST", expected_session_count=0)
        else:
            backend.append(staging, label="TEST", expected_session_count=session_n)


def _cleanup_chunks(chunks) -> None:
    """Remove staging directories returned by build_staging."""
    for staging, _session_n, _is_new in chunks:
        shutil.rmtree(staging, ignore_errors=True)


def _entry_map(manifest) -> dict[str, object]:
    """Return {path: ManifestEntry} for a session manifest."""
    return {e.path: e for e in manifest.entries}


def _dummy_blob(
    rel_path: str,
    encoded_size: int,
    *,
    dedup_blob_path: str = "",
    dedup_origin_rel_path: str = "",
) -> _PreparedBlob:
    """Build a dummy prepared blob for packing-unit tests."""
    return _PreparedBlob(
        rel_path=rel_path,
        kind="full",
        blob=b"",
        result_checksum="x",
        full_size_bytes=encoded_size,
        encoded_size=encoded_size,
        dedup_blob_path=dedup_blob_path,
        dedup_origin_rel_path=dedup_origin_rel_path,
    )


# Tests


def test_same_sync_duplicates_share_one_blob(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Two files with identical content, added in the same sync under two
        different paths, produce exactly one physical blob and two
        manifest entries that both reference it.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(_PAYLOAD)
    sub = source / "copy"
    sub.mkdir()
    (sub / "b.txt").write_bytes(_PAYLOAD)

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    chunks = _stage(tmp_path, 0, source, backend, {}, {})
    try:
        assert len(chunks) == 1
        staging, session_n, _is_new = chunks[0]
        blobs = _session_blobs(staging, session_n)
        assert len(blobs) == 1, blobs
        manifest = _read_session_manifest(staging, session_n)
        entries = _entry_map(manifest)
        assert set(entries) == {"a.txt", "copy/b.txt"}
        blob_rel = f"session_000/full/{blobs[0].name}"
        for entry in entries.values():
            assert entry.type == "full"
            assert entry.result_checksum == _sha256(_PAYLOAD)
            assert entry.file == blob_rel
        assert (staging / blob_rel).exists()
        _burn_chunks(backend, chunks)
    finally:
        _cleanup_chunks(chunks)


def test_moved_file_references_origin_session_blob(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A file moved to a new path between syncs stages zero new blob
        bytes. Its manifest entry references the origin session's
        existing blob, not a fresh one in the current session.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "orig.txt").write_bytes(_PAYLOAD)

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    chunks0 = _stage(tmp_path, 0, source, backend, {}, {})
    try:
        origin_manifest = _read_session_manifest(chunks0[0][0], 0)
        origin_entry = _entry_map(origin_manifest)["orig.txt"]
        origin_blob = origin_entry.file
        _burn_chunks(backend, chunks0)
    finally:
        _cleanup_chunks(chunks0)

    (source / "orig.txt").unlink()
    moved = source / "moved"
    moved.mkdir()
    (moved / "orig.txt").write_bytes(_PAYLOAD)

    manifests = load_session_manifests(backend, _crypto)
    chunks1 = _stage(
        tmp_path, 1, source, backend,
        build_disc_state(manifests),
        build_disc_origin(manifests),
        manifests,
    )
    try:
        assert len(chunks1) == 1
        staging, session_n, _is_new = chunks1[0]
        assert _session_blobs(staging, session_n) == []
        manifest = _read_session_manifest(staging, session_n)
        assert manifest.deleted == ["orig.txt"]
        entry = _entry_map(manifest)["moved/orig.txt"]
        assert entry.type == "full"
        assert entry.result_checksum == _sha256(_PAYLOAD)
        assert entry.file == origin_blob
        assert entry.file.startswith("session_000/")
        _burn_chunks(backend, chunks1)
    finally:
        _cleanup_chunks(chunks1)


def test_changed_file_matching_other_checksum_dedups(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A file whose content changes to match some other already-archived
        file's content dedups instead of being delta- or full-encoded.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(_PAYLOAD)
    (source / "b.txt").write_bytes(b"different-original-bytes")

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    chunks0 = _stage(tmp_path, 0, source, backend, {}, {})
    try:
        origin_blob = _entry_map(_read_session_manifest(chunks0[0][0], 0))["a.txt"].file
        _burn_chunks(backend, chunks0)
    finally:
        _cleanup_chunks(chunks0)

    (source / "b.txt").write_bytes(_PAYLOAD)

    manifests = load_session_manifests(backend, _crypto)
    chunks1 = _stage(
        tmp_path, 1, source, backend,
        build_disc_state(manifests),
        build_disc_origin(manifests),
        manifests,
    )
    try:
        staging, session_n, _is_new = chunks1[0]
        assert _session_blobs(staging, session_n) == []
        entry = _entry_map(_read_session_manifest(staging, session_n))["b.txt"]
        assert entry.type == "full"
        assert entry.result_checksum == _sha256(_PAYLOAD)
        assert entry.file == origin_blob
        assert not entry.delta_file
        _burn_chunks(backend, chunks1)
    finally:
        _cleanup_chunks(chunks1)


def test_unique_file_still_gets_physical_blob(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A genuinely new, unique file is unaffected and still gets a real
        physical blob (regression guard against dedup swallowing an
        ordinary write).
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep.txt").write_bytes(_UNIQUE_A)
    (source / "dup1.txt").write_bytes(_PAYLOAD)
    (source / "dup2.txt").write_bytes(_PAYLOAD)

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    chunks = _stage(tmp_path, 0, source, backend, {}, {})
    try:
        staging, session_n, _is_new = chunks[0]
        blobs = _session_blobs(staging, session_n)
        assert len(blobs) == 2, blobs
        entries = _entry_map(_read_session_manifest(staging, session_n))
        keep = entries["keep.txt"]
        assert (staging / keep.file).exists()
        assert keep.result_checksum == _sha256(_UNIQUE_A)
        assert entries["dup1.txt"].file == entries["dup2.txt"].file
        assert entries["dup1.txt"].file != keep.file
        _burn_chunks(backend, chunks)
    finally:
        _cleanup_chunks(chunks)


def _two_session_moved_and_duplicated(tmp_path: Path) -> ISOBackend:
    """
    Input:  tmp_path
    Output: ISOBackend holding a two-session archive
    Details:
        Session 0 stores keep.txt (unique) and orig.txt (shared payload).
        Session 1 moves orig.txt to moved/orig.txt, adds dup.txt with the
        same payload, and adds unique2.txt. One physical blob per distinct
        checksum.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep.txt").write_bytes(_UNIQUE_A)
    (source / "orig.txt").write_bytes(_PAYLOAD)

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    chunks0 = _stage(tmp_path, 0, source, backend, {}, {})
    try:
        assert len(_session_blobs(chunks0[0][0], 0)) == 2
        _burn_chunks(backend, chunks0)
    finally:
        _cleanup_chunks(chunks0)

    (source / "orig.txt").unlink()
    (source / "moved").mkdir()
    (source / "moved" / "orig.txt").write_bytes(_PAYLOAD)
    (source / "dup.txt").write_bytes(_PAYLOAD)
    (source / "unique2.txt").write_bytes(_UNIQUE_B)

    manifests = load_session_manifests(backend, _crypto)
    chunks1 = _stage(
        tmp_path, 1, source, backend,
        build_disc_state(manifests),
        build_disc_origin(manifests),
        manifests,
    )
    try:
        staging, session_n, _is_new = chunks1[0]
        blobs1 = _session_blobs(staging, session_n)
        assert len(blobs1) == 1, blobs1
        entries = _entry_map(_read_session_manifest(staging, session_n))
        orig_blob = entries["moved/orig.txt"].file
        assert orig_blob.startswith("session_000/")
        assert entries["dup.txt"].file == orig_blob
        assert entries["unique2.txt"].file.startswith("session_001/")
        assert (staging / entries["unique2.txt"].file).exists()
        _burn_chunks(backend, chunks1)
    finally:
        _cleanup_chunks(chunks1)
    return backend


def test_restore_reconstructs_all_duplicate_paths(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Restore reconstructs every duplicate-content path correctly from
        the one shared blob, byte-identical to source, including a path
        whose blob lives several sessions back.
    """
    backend = _two_session_moved_and_duplicated(tmp_path)
    dest = tmp_path / "dest"
    result = restore(dest, backend, _crypto)
    assert result.failures == 0
    assert (dest / "keep.txt").read_bytes() == _UNIQUE_A
    assert (dest / "moved" / "orig.txt").read_bytes() == _PAYLOAD
    assert (dest / "dup.txt").read_bytes() == _PAYLOAD
    assert (dest / "unique2.txt").read_bytes() == _UNIQUE_B
    assert not (dest / "orig.txt").exists()


def test_verify_checksum_passes_for_duplicate_entries(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        verify --level checksum passes for every duplicate-content entry.
    """
    backend = _two_session_moved_and_duplicated(tmp_path)
    assert verify(backend, _crypto, level="checksum")


def test_dedup_reference_counts_as_zero_in_pack_budget(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        The packing budget counts a dedup reference as zero bytes, so two
        identical files that would not both fit as real blobs still pack
        into one bin. The pre-burn staged-size estimate matches the bytes
        actually written (one blob, not two).
    """
    real = _dummy_blob("a.txt", 1000)
    other_real = _dummy_blob("c.txt", 1000)
    ref = _dummy_blob(
        "b.txt", 0, dedup_blob_path="session_000/full/" + "ab" * 32,
    )
    two_real = _pack_blobs([real, other_real], first_budget=1500, full_budget=1500)
    assert len(two_real) == 2
    one_plus_ref = _pack_blobs([real, ref], first_budget=1500, full_budget=1500)
    assert len(one_plus_ref) == 1
    assert {b.rel_path for b in one_plus_ref[0]} == {"a.txt", "b.txt"}

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(_PAYLOAD)
    (source / "b.txt").write_bytes(_PAYLOAD)
    remaining = 1_000_000
    backend = _make_backend(remaining)
    chunks = _stage(tmp_path, 0, source, backend, {}, {})
    try:
        staging, session_n, _is_new = chunks[0]
        assert len(_session_blobs(staging, session_n)) == 1
        used = _staging_bytes(staging)
        assert used < len(_PAYLOAD) + 20_000
        captured = capsys.readouterr()
        expected_pct = 100.0 * used / remaining
        assert f"{expected_pct:.1f}%" in captured.out
        assert "OK: session is" in captured.out
    finally:
        _cleanup_chunks(chunks)


if __name__ == "__main__":
    pass
