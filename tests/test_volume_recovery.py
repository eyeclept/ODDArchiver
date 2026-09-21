"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for restore --skip-volume recovery mode: a 3-volume archive
    whose middle volume is permanently lost, bucketing files into
    restored / stale / missing, and confirming the same scenario without
    --skip-volume keeps today's failed/unresolved behaviour.
"""
# Imports
import argparse
import hashlib
import shutil
from pathlib import Path
from unittest.mock import MagicMock

from oddarchiver.cli import _run_restore, build_parser
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import read_manifest
from oddarchiver.restore import restore
from oddarchiver.restore_state import load_restore_state
from oddarchiver.session import build_staging
from oddarchiver.volume import VolumeMarker, write_volume_marker

# Globals
_DISC = 20 * 2**20
_PAYLOAD = 8000
_ARCHIVE_ID = "test-archive-recovery"
_LABELS = ("VOL0", "VOL1", "VOL2")
_CREATED_AT = "2026-09-16T00:00:00Z"
_crypto = NullCrypto()

_KEEP = b"KEEP" + b"K" * _PAYLOAD
_STALE_V0 = bytes([i % 256 for i in range(_PAYLOAD)])
_STALE_V1 = bytes([(i * 7 + 3) % 256 for i in range(_PAYLOAD)])
_STALE_V2 = _STALE_V1[:-16] + b"STALE-V2-TAIL!!!!"
_MISS_V1 = b"MISSING-V1-" + bytes([i % 256 for i in range(_PAYLOAD)])
_MISS_V2 = _MISS_V1[:-8] + b"V2-TAIL!"
_LATER = b"LATE" + b"L" * _PAYLOAD


# Functions


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_cache(old_by_path: dict[str, bytes] | None = None) -> MagicMock:
    """Return a mock CacheManager that serves plaintext old bytes per path."""
    cache = MagicMock()
    blobs = old_by_path or {}

    def _get(rel_path, session, backend, crypto=None, **_kwargs):
        return blobs.get(rel_path, b"")

    cache.get_with_fallback.side_effect = _get
    return cache


def _entry_types(staging: Path, session_n: int) -> dict[str, str]:
    """Return {path: entry.type} from a staging session manifest."""
    manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
    return {entry.path: entry.type for entry in manifest.entries}


def _burn_marked_volume(
    tmp_path: Path,
    staging: Path,
    session_n: int,
    volume_index: int,
) -> Path:
    """Write a VolumeMarker and init a new ISO for this session."""
    prev_label = "" if volume_index == 0 else _LABELS[volume_index - 1]
    write_volume_marker(
        staging,
        VolumeMarker(
            archive_id=_ARCHIVE_ID,
            volume_index=volume_index,
            first_session=session_n,
            created_at=_CREATED_AT,
            previous_volume_label=prev_label,
        ),
    )
    iso = tmp_path / f"vol{volume_index}.iso"
    backend = ISOBackend(iso, disc_size=_DISC)
    backend.init(staging, label=_LABELS[volume_index], expected_session_count=0)
    return iso


def _stage_session(
    tmp_path: Path,
    source: Path,
    session_n: int,
    disc_state: dict[str, str],
    disc_origin: dict[str, int],
    cache: MagicMock,
) -> Path:
    """
    Input:  tmp_path, source, session_n, disc_state, disc_origin, cache
    Output: staging path of the single returned chunk
    Details:
        Each recovery-fixture session is small enough to fit on one 20 MiB
        ISO. A multi-chunk split would mean the fixture files are too big.
    """
    pack_backend = ISOBackend(tmp_path / f"pack{session_n}.iso", disc_size=_DISC)
    chunks = build_staging(
        session_n=session_n,
        source=source,
        disc_state=disc_state,
        disc_origin=disc_origin,
        backend=pack_backend,
        cache=cache,
        crypto=_crypto,
        _staging_root=tmp_path / f"staging{session_n}",
    )
    assert len(chunks) == 1, f"session {session_n} split into {len(chunks)} chunks"
    staging, returned_n, is_new = chunks[0]
    assert returned_n == session_n
    assert is_new is False
    return staging


def _build_three_volumes(tmp_path: Path) -> tuple[list[Path], dict[str, bytes]]:
    """
    Input:  tmp_path - pytest temp directory
    Output: (iso_paths, expected_latest) for a 3-volume marked archive
    Details:
        Volume 0 session 0: kept.txt full, stale.txt full.
        Volume 1 session 1: stale.txt delta, missing.txt full.
        Volume 2 session 2: stale.txt delta (vs volume 1), missing.txt
        delta (vs volume 1), later.txt full.
        expected_latest is the live tree after all three sessions.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "kept.txt").write_bytes(_KEEP)
    (source / "stale.txt").write_bytes(_STALE_V0)

    staging0 = _stage_session(tmp_path, source, 0, {}, {}, _make_cache())
    try:
        types0 = _entry_types(staging0, 0)
        assert types0["kept.txt"] == "full"
        assert types0["stale.txt"] == "full"
        iso0 = _burn_marked_volume(tmp_path, staging0, 0, 0)
    finally:
        shutil.rmtree(staging0, ignore_errors=True)

    (source / "stale.txt").write_bytes(_STALE_V1)
    (source / "missing.txt").write_bytes(_MISS_V1)
    staging1 = _stage_session(
        tmp_path,
        source,
        1,
        {"kept.txt": _sha256(_KEEP), "stale.txt": _sha256(_STALE_V0)},
        {"kept.txt": 0, "stale.txt": 0},
        _make_cache({"stale.txt": _STALE_V0}),
    )
    try:
        types1 = _entry_types(staging1, 1)
        assert types1["stale.txt"] == "delta", types1
        assert types1["missing.txt"] == "full"
        iso1 = _burn_marked_volume(tmp_path, staging1, 1, 1)
    finally:
        shutil.rmtree(staging1, ignore_errors=True)

    (source / "stale.txt").write_bytes(_STALE_V2)
    (source / "missing.txt").write_bytes(_MISS_V2)
    (source / "later.txt").write_bytes(_LATER)
    staging2 = _stage_session(
        tmp_path,
        source,
        2,
        {
            "kept.txt": _sha256(_KEEP),
            "stale.txt": _sha256(_STALE_V1),
            "missing.txt": _sha256(_MISS_V1),
        },
        {"kept.txt": 0, "stale.txt": 1, "missing.txt": 1},
        _make_cache({"stale.txt": _STALE_V1, "missing.txt": _MISS_V1}),
    )
    try:
        types2 = _entry_types(staging2, 2)
        assert types2["stale.txt"] == "delta", types2
        assert types2["missing.txt"] == "delta", types2
        assert types2["later.txt"] == "full"
        iso2 = _burn_marked_volume(tmp_path, staging2, 2, 2)
    finally:
        shutil.rmtree(staging2, ignore_errors=True)

    expected_latest = {
        "kept.txt": _KEEP,
        "stale.txt": _STALE_V2,
        "missing.txt": _MISS_V2,
        "later.txt": _LATER,
    }
    return [iso0, iso1, iso2], expected_latest


def _backend(iso: Path) -> ISOBackend:
    return ISOBackend(iso, disc_size=_DISC)


def _restore_args(dest: Path, iso: Path, skip_volumes=None) -> argparse.Namespace:
    """Namespace matching _run_restore's getattr usage."""
    return argparse.Namespace(
        dest=str(dest),
        test_iso=str(iso),
        device=None,
        disc_size="20mb",
        session=None,
        force=False,
        paths=None,
        key=None,
        recover_scan=False,
        label="",
        skip_volumes=skip_volumes,
    )


def test_skip_volume_flag_is_repeatable():
    """
    Input:  None
    Output: None
    Details:
        restore --skip-volume 1 --skip-volume 2 parses as skip_volumes=[1, 2].
    """
    parser = build_parser()
    args = parser.parse_args(
        ["restore", "/dest", "--skip-volume", "1", "--skip-volume", "2"]
    )
    assert args.skip_volumes == [1, 2]


def test_recovery_buckets_restored_stale_missing(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Restore volumes 0 and 2 with --skip-volume 1. kept.txt and later.txt
        restore. stale.txt lands in stale at session 0. missing.txt is
        missing and absent from dest. failures stay 0. needed is None.
    """
    isos, _latest = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest"
    skip = [1]

    r0 = restore(dest, _backend(isos[0]), _crypto, skip_volumes=skip)
    r2 = restore(dest, _backend(isos[2]), _crypto, skip_volumes=skip)

    assert r0.failures == 0
    assert r2.failures == 0
    restored, failures = r2
    assert (restored, failures) == (r2.restored, r2.failures)
    assert len(r2) == 2

    assert (dest / "kept.txt").read_bytes() == _KEEP
    assert (dest / "later.txt").read_bytes() == _LATER
    assert (dest / "stale.txt").read_bytes() == _STALE_V0
    assert not (dest / "missing.txt").exists()

    assert ("stale.txt", 0) in r2.stale
    assert r2.missing == ["missing.txt"]
    assert r2.needed is None
    assert r0.stale == []
    assert r0.missing == []

    state = load_restore_state(dest, _ARCHIVE_ID)
    assert state is not None
    assert state.applied_volumes == [0, 2]


def test_recovery_does_not_raise_on_missing(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A recovery-mode run where every file is restored, stale, or missing
        returns failures==0. dest is unchanged for the missing path.
    """
    isos, _latest = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest_ok"
    skip = [1]
    r0 = restore(dest, _backend(isos[0]), _crypto, skip_volumes=skip)
    r2 = restore(dest, _backend(isos[2]), _crypto, skip_volumes=skip)
    assert r0.failures == 0
    assert r2.failures == 0
    assert r2.stale
    assert r2.missing
    assert not (dest / "missing.txt").exists()


def test_without_skip_volume_unresolvable_stays_non_recovery(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        The same 0-then-2 restore without --skip-volume keeps today's
        behaviour: the volume-1 gap is still needed, missing.txt is not
        written, and stale.txt is not a recovery stale bucket.
    """
    isos, _latest = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest_plain"
    r0 = restore(dest, _backend(isos[0]), _crypto)
    r2 = restore(dest, _backend(isos[2]), _crypto)

    assert r0.failures == 0
    assert r2.needed is not None
    assert r2.needed.volume_index == 1
    assert r2.needed.label == "VOL1"
    assert r2.stale == []
    assert r2.missing == []
    assert not (dest / "missing.txt").exists()
    assert (dest / "kept.txt").read_bytes() == _KEEP
    assert (dest / "stale.txt").read_bytes() == _STALE_V0
    assert (dest / "later.txt").read_bytes() == _LATER
    # stale.txt's volume-2 delta cannot apply on volume-0 bytes.
    assert r2.failures >= 1

    state = load_restore_state(dest, _ARCHIVE_ID)
    assert state is not None
    assert 1 not in state.applied_volumes
    assert 2 not in state.applied_volumes


def test_skip_volume_walks_past_to_earlier_needed(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Restore volume 2 only, with --skip-volume 1. Volume 1 must not be
        reported as needed. Volume 0 still is.
    """
    isos, _latest = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest_gap"
    result = restore(dest, _backend(isos[2]), _crypto, skip_volumes=[1])
    assert result.failures == 0
    assert result.needed is not None
    assert result.needed.volume_index == 0
    assert (dest / "later.txt").read_bytes() == _LATER
    assert not (dest / "stale.txt").exists()
    assert not (dest / "missing.txt").exists()


def test_cli_prints_stale_and_missing_table(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        _run_restore on volume 2 after volume 0, with skip_volumes=[1],
        exits 0 and prints each stale path with its session and each
        missing path plainly. Stale/missing are not failures.
    """
    isos, _latest = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest_cli"
    r0 = restore(dest, _backend(isos[0]), _crypto, skip_volumes=[1])
    assert r0.failures == 0
    rc = _run_restore(_restore_args(dest, isos[2], skip_volumes=[1]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "stale:" in out
    assert "stale.txt" in out
    assert "session 0" in out
    assert "missing:" in out
    assert "missing.txt" in out
    assert "Restore needs volume 1" not in out
