"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

    Tests for marked single-disc and legacy restore. Multi-disc selection
    lives in test_restore_plan.py.
"""
# Imports
import argparse
import hashlib
import logging
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.catalog import (
    ArchiveCatalog, CatalogDisc, build_archive_catalog, learn_disc,
    remember_encryption_mode,
)
from oddarchiver.cli import _run_restore, build_parser, dispatch
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.ledger import ledger_dir
from oddarchiver.manifest import Manifest, ManifestEntry, load_session_manifests, read_manifest, write_manifest
from oddarchiver.restore import restore
from oddarchiver.restore_plan import restore_archive
from oddarchiver.restore_state import load_restore_state
from oddarchiver.session import build_staging
from oddarchiver.volume import (
    PathVersion, VolumeMarker, build_volume_baseline, read_volume_marker,
    write_volume_marker,
)
from tests.helpers import _write_prior_baseline, fill
from tests.test_fill_layout import IsolatedDrive, _copy_isolated_iso

# Globals
_DISC = 100_000
_FILE_BYTES = 60_000
_ARCHIVE_ID = "test-archive-3vol"
_LABELS = ("VOL0", "VOL1", "VOL2")
_CREATED_AT = "2026-09-16T00:00:00Z"
_crypto = NullCrypto()

logger = logging.getLogger(__name__)

@pytest.fixture(autouse=True)
def _isolated_cli_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache))



# Functions


def _make_cache() -> MagicMock:
    """Return a mock CacheManager unused by all-new-file packing."""
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""
    return cache


def _tree_files(root: Path) -> dict[str, bytes]:
    """
    Input:  root - directory to snapshot
    Output: {relative path: bytes} for every file under root
    Details:
        Restore-state never lives under dest (see restore_state.state_path);
        no filtering is needed to keep this an exact mirror check.
    """
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        out[str(path.relative_to(root))] = path.read_bytes()
    return out


def _blob_id(session_n: int, rel_path: str) -> str:
    """Input: session and path. Output: a legacy blob name for this fixture."""
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _build_three_volumes(tmp_path: Path) -> tuple[Path, list[Path]]:
    """Build three independent marked ISO discs and their v2 baselines."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(fill(b"A", _FILE_BYTES))
    (source / "b.bin").write_bytes(fill(b"B", _FILE_BYTES))
    sub = source / "sub"
    sub.mkdir()
    (sub / "c.bin").write_bytes(fill(b"C", _FILE_BYTES))

    pack_backend = ISOBackend(tmp_path / "pack.iso", disc_size=_DISC)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=pack_backend,
        cache=_make_cache(),
        crypto=_crypto,
        _staging_root=tmp_path / "staging",
    )
    try:
        assert len(chunks) == 3, f"expected 3 chunks, got {len(chunks)}"
        prior = None
        iso_paths: list[Path] = []
        for i, (staging, session_n, is_new) in enumerate(chunks):
            assert session_n == i
            assert is_new == (i > 0)
            prev_label = "" if i == 0 else _LABELS[i - 1]
            marker = VolumeMarker(
                archive_id=_ARCHIVE_ID,
                volume_index=i,
                first_session=session_n,
                created_at=_CREATED_AT,
                previous_volume_label=prev_label,
            )
            _write_prior_baseline(staging, prior, _crypto, marker, session_n)
            write_volume_marker(staging, marker)
            iso = tmp_path / f"vol{i}.iso"
            backend = ISOBackend(iso, disc_size=_DISC)
            backend.init(staging, label=_LABELS[i], expected_session_count=0)
            iso_paths.append(iso)
            manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
            prior = build_volume_baseline(prior, [manifest], marker, _LABELS[i])
        return source, iso_paths
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_marked_restore_uses_inserted_disc_only(tmp_path):
    source, isos = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest"
    result = restore(dest, ISOBackend(isos[2], disc_size=_DISC), _crypto)
    assert result.failures == 0
    assert result.restored == 1
    assert result.unresolved == []
    assert _tree_files(dest) == {"sub/c.bin": (source / "sub" / "c.bin").read_bytes()}


def test_rerun_skips_identical_file_without_applied_volume_list(tmp_path):
    _source, isos = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest_partial"
    backend = ISOBackend(isos[0], disc_size=_DISC)
    first = restore(dest, backend, _crypto)
    assert first.failures == 0
    dest_file = dest / "a.bin"
    mtime_before = dest_file.stat().st_mtime_ns
    second = restore(dest, backend, _crypto)
    assert second.restored == 0
    assert second.failures == 0
    assert dest_file.stat().st_mtime_ns == mtime_before
    state = load_restore_state(dest, _ARCHIVE_ID)
    assert state is not None
    assert state.files["a.bin"].last_session == 0


def test_single_disc_restore_does_not_request_missing_middle(tmp_path):
    source, isos = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest_gap"
    result = restore(dest, ISOBackend(isos[2], disc_size=_DISC), _crypto)
    assert result.failures == 0
    assert result.unresolved == []
    assert _tree_files(dest) == {"sub/c.bin": (source / "sub" / "c.bin").read_bytes()}


def test_legacy_unmarked_does_not_write_state(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        An ISO with no volume.json must restore as today and must not
        create any restore-state file, in dest or under ledger_dir().
    """
    staging = tmp_path / "staging_legacy"
    session_dir = staging / "session_000" / "full"
    session_dir.mkdir(parents=True)
    content = b"legacy file"
    blob_name = _blob_id(0, "legacy.txt")
    (session_dir / blob_name).write_bytes(content)
    write_manifest(
        staging / "session_000",
        Manifest(
            version=1,
            session=0,
            timestamp=_CREATED_AT,
            source="/src",
            label="TEST",
            based_on_session=None,
            encryption={},
            entries=[
                ManifestEntry(
                    path="legacy.txt",
                    type="full",
                    result_checksum=hashlib.sha256(content).hexdigest(),
                    full_size_bytes=len(content),
                    file=f"session_000/full/{blob_name}",
                )
            ],
            deleted=[],
            manifest_checksum="",
        ),
    )
    iso = tmp_path / "legacy.iso"
    backend = ISOBackend(iso, disc_size=20 * 2**20)
    backend.init(staging, label="TEST")
    dest = tmp_path / "dest_legacy"
    restored, failures = restore(dest, backend, _crypto)
    assert failures == 0
    assert restored == 1
    assert (dest / "legacy.txt").read_bytes() == content
    state_dir = ledger_dir()
    written = list(state_dir.glob("restore-*.state.json")) if state_dir.exists() else []
    assert written == [], f"legacy restore must not write restore-state: {written}"


def test_delta_without_prior_volume_is_unresolved(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A delta with no local full base is unresolved without prompting
        for another disc. The planner handles cross-disc reconstruction.
    """
    staging = tmp_path / "staging_delta"
    delta_dir = staging / "session_001" / "deltas"
    delta_dir.mkdir(parents=True)
    blob_name = _blob_id(1, "changed.txt")
    (delta_dir / blob_name).write_bytes(b"not a real delta")
    write_manifest(
        staging / "session_001",
        Manifest(
            version=1,
            session=1,
            timestamp=_CREATED_AT,
            source="/src",
            label="VOL1",
            based_on_session=0,
            encryption={},
            entries=[
                ManifestEntry(
                    path="changed.txt",
                    type="delta",
                    result_checksum="0" * 64,
                    full_size_bytes=12,
                    delta_file=f"session_001/deltas/{blob_name}",
                    delta_size_bytes=16,
                )
            ],
            deleted=[],
            manifest_checksum="",
        ),
    )
    write_volume_marker(
        staging,
        VolumeMarker(
            archive_id=_ARCHIVE_ID,
            volume_index=1,
            first_session=1,
            created_at=_CREATED_AT,
            previous_volume_label="VOL0",
        ),
    )
    iso = tmp_path / "delta.iso"
    backend = ISOBackend(iso, disc_size=20 * 2**20)
    backend.init(staging, label="VOL1")
    dest = tmp_path / "dest_delta"
    result = restore(dest, backend, _crypto)
    assert result.failures == 0
    assert result.restored == 0
    assert result.unresolved == [("changed.txt", "disc 0")]
    assert not (dest / "changed.txt").exists()


def _assert_single_disc_cli_files(dest: Path, source: Path) -> None:
    """Input: destination and source paths. Output: verified single-disc contents."""
    assert (dest / "sub" / "c.bin").read_bytes() == (source / "sub" / "c.bin").read_bytes()
    assert (dest / "sub" / "c.bin").read_bytes() == fill(b"C", _FILE_BYTES)
    assert not (dest / "a.bin").exists()
    assert not (dest / "b.bin").exists()
    restored = {
        str(path.relative_to(dest)): path.read_bytes()
        for path in dest.rglob("*") if path.is_file()
    }
    assert restored == {"sub/c.bin": fill(b"C", _FILE_BYTES)}


def test_cli_single_disc_notice_without_needed_volume(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        A marked single-disc restore identifies its limited scope.
        The file on the inserted disc matches the source bytes.
    """
    source, isos = _build_three_volumes(tmp_path)
    remember_encryption_mode(_ARCHIVE_ID, "none")
    dest = tmp_path / "dest_cli"
    args = argparse.Namespace(
        dest=str(dest),
        test_iso=str(isos[2]),
        device=None,
        disc_size="1mb",
        session=None,
        force=False,
        paths=None,
        key=None,
        recover_scan=False,
        label="",
    )
    rc = _run_restore(args)
    assert rc == 0
    out = capsys.readouterr()
    assert "Restore needs volume" not in out.out + out.err
    assert "Other discs can hold newer versions" in out.err
    _assert_single_disc_cli_files(dest, source)


_FILL_DISC = "8mb"
_FILL_DISC_BYTES = 8_000_000


def _fill_dispatch(argv: list[str]) -> int:
    """Input: CLI argv. Output: dispatch exit code."""
    return dispatch(build_parser().parse_args(argv))


def _restored_files(dest: Path) -> dict[str, bytes]:
    """Input: restore destination. Output: relative path to file bytes."""
    return {
        str(path.relative_to(dest)): path.read_bytes()
        for path in dest.rglob("*") if path.is_file()
    }


def _build_filled_two_volume_archive(tmp_path: Path, monkeypatch):
    """
    Input: tmp_path, monkeypatch
    Output: source dir, volume-0 ISO, volume-1 ISO, and source file bytes
    Details: Volume 0 holds a.bin then a fill session for c.bin. Volume 1
        holds b.bin. Matches the host fill-restore data-loss layout.
    """
    def _boom(prompt=""):
        raise AssertionError(f"ISOBackend must not call input(): {prompt!r}")

    monkeypatch.setattr("builtins.input", _boom)
    source = tmp_path / "src"
    source.mkdir()
    a_bytes = fill(b"A", 3_000_000)
    b_bytes = fill(b"B", 5_500_000)
    c_bytes = fill(b"C", 40_000)
    (source / "a.bin").write_bytes(a_bytes)
    iso = tmp_path / "v.iso"
    assert _fill_dispatch([
        "init", str(source), "--test-iso", str(iso),
        "--disc-size", _FILL_DISC, "--label", "FS",
    ]) == 0
    (source / "b.bin").write_bytes(b_bytes)
    assert _fill_dispatch([
        "sync", str(source), "--test-iso", str(iso),
        "--disc-size", _FILL_DISC, "--no-fill",
    ]) == 0
    vol1 = tmp_path / "v.vol1.iso"
    assert vol1.is_file()
    (source / "c.bin").write_bytes(c_bytes)
    assert _fill_dispatch([
        "sync", str(source), "--test-iso", str(vol1),
        "--disc-size", _FILL_DISC, "--fill",
    ]) == 0
    return source, iso, vol1, {"a.bin": a_bytes, "b.bin": b_bytes, "c.bin": c_bytes}


def _restore_all_disks(dest: Path, iso: Path) -> int:
    """Input: dest and starting ISO. Output: restore exit code."""
    return _fill_dispatch([
        "restore", str(dest), "--test-iso", str(iso), "--all-disks",
    ])


class _ScriptedIsolatedAcquisition:
    """Yield IsolatedDrive volumes by index. No sibling ISO scan."""

    image = None

    def __init__(self, volumes: dict[int, IsolatedDrive]) -> None:
        self.volumes = volumes
        self.needed_log: list[dict[int, int]] = []
        self.yielded: list[int] = []

    def acquire(self, needed, catalog, archive_id):
        self.needed_log.append(dict(needed))
        for index in sorted(needed):
            drive = self.volumes.get(index)
            if drive is None:
                continue
            self.yielded.append(index)
            return drive, None, False
        return None, next(iter(needed)), False


def _isolated_fill_volumes(tmp_path: Path, iso: Path, vol1: Path) -> dict[int, IsolatedDrive]:
    """Input: tmp_path and the two ISO paths. Output: IsolatedDrive per volume."""
    iso0 = _copy_isolated_iso(iso, tmp_path / "only0")
    iso1 = _copy_isolated_iso(vol1, tmp_path / "only1")
    return {
        0: IsolatedDrive(iso0, _FILL_DISC_BYTES),
        1: IsolatedDrive(iso1, _FILL_DISC_BYTES),
    }


def _restore_isolated(
    dest: Path, start_index: int, volumes: dict[int, IsolatedDrive],
) -> tuple[object, _ScriptedIsolatedAcquisition]:
    """
    Input: dest, starting volume index, IsolatedDrive map
    Output: restore result and the scripted acquisition
    """
    start = volumes[start_index]
    marker = read_volume_marker(start)
    assert marker is not None
    manifests = load_session_manifests(
        start, _crypto, start_session=marker.first_session, marker=marker,
    )
    catalog = build_archive_catalog(start, _crypto, manifests, marker)
    acquisition = _ScriptedIsolatedAcquisition(volumes)
    result = restore_archive(
        dest, start, _crypto, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    return result, acquisition


def _assert_missing_requested_once(
    acquisition: _ScriptedIsolatedAcquisition, start_index: int, missing: set[int],
) -> None:
    """Input: acquisition log, start index, missing indexes. Output: None."""
    requested = [index for needed in acquisition.needed_log for index in needed]
    for index in missing:
        assert requested.count(index) == 1, requested
    assert start_index not in requested


def test_fill_restore_all_disks_from_either_volume(tmp_path, monkeypatch):
    """
    Input: two ISO volumes in separate dirs after a fill onto volume 0
    Output: each missing volume requested once; all three files match from either start
    """
    _source, iso, vol1, expected = _build_filled_two_volume_archive(
        tmp_path, monkeypatch,
    )
    volumes_new = _isolated_fill_volumes(tmp_path / "from1", iso, vol1)
    dest_new = tmp_path / "out-new"
    result_new, acq_new = _restore_isolated(dest_new, 1, volumes_new)
    assert result_new.failures == 0
    _assert_missing_requested_once(acq_new, 1, {0})
    assert _restored_files(dest_new) == expected
    volumes_old = _isolated_fill_volumes(tmp_path / "from0", iso, vol1)
    dest_old = tmp_path / "out-old"
    result_old, acq_old = _restore_isolated(dest_old, 0, volumes_old)
    assert result_old.failures == 0
    _assert_missing_requested_once(acq_old, 0, {1})
    assert _restored_files(dest_old) == expected



def test_fill_verify_full_both_volumes(tmp_path, monkeypatch, capsys):
    """
    Input: filled two-volume ISO archive with the burn ledger still present
    Output: verify --level full reports 0 errors on both volumes
    """
    from oddarchiver.ledger import load_ledger

    _source, iso, vol1, _expected = _build_filled_two_volume_archive(
        tmp_path, monkeypatch,
    )
    marker = read_volume_marker(ISOBackend(iso, disc_size=_FILL_DISC_BYTES))
    assert marker is not None
    assert load_ledger(
        "FS", str(iso), volume_index=0, archive_id=marker.archive_id,
    ) is not None
    capsys.readouterr()
    assert _fill_dispatch([
        "verify", "--test-iso", str(iso), "--level", "full",
    ]) == 0
    out0 = capsys.readouterr().out
    assert "0 error" in out0
    assert _fill_dispatch([
        "verify", "--test-iso", str(vol1), "--level", "full",
    ]) == 0
    out1 = capsys.readouterr().out
    assert "0 error" in out1


def test_fill_restore_without_local_memory_learns_filled_disc(tmp_path, monkeypatch):
    """
    Input: filled archive with local disc records removed; isolated volume 1
    Output: acquisition yields volume 0 once; all three files match
    """
    from oddarchiver.catalog import archive_memory_path

    _source, iso, vol1, expected = _build_filled_two_volume_archive(
        tmp_path, monkeypatch,
    )
    marker = read_volume_marker(ISOBackend(vol1, disc_size=_FILL_DISC_BYTES))
    assert marker is not None
    memory = archive_memory_path(marker.archive_id)
    memory.unlink(missing_ok=True)
    remember_encryption_mode(marker.archive_id, "none")
    volumes = _isolated_fill_volumes(tmp_path / "cold", iso, vol1)
    dest = tmp_path / "out-cold"
    result, acquisition = _restore_isolated(dest, 1, volumes)
    assert result.failures == 0
    assert acquisition.yielded.count(0) == 1
    assert _restored_files(dest) == expected



def test_fill_restore_skip_older_volume_then_second_run(tmp_path, monkeypatch):
    """
    Input: restore from volume 1 with volume 0 marked missing, then a rerun
    Output: first run lacks fill files; second run restores them
    """
    _source, iso, vol1, expected = _build_filled_two_volume_archive(
        tmp_path, monkeypatch,
    )
    dest = tmp_path / "out-skip"
    rc = _fill_dispatch([
        "restore", str(dest), "--test-iso", str(vol1),
        "--all-disks", "--skip-volume", "0",
    ])
    assert rc == 0
    first = _restored_files(dest)
    assert first.get("b.bin") == expected["b.bin"]
    assert "a.bin" not in first
    assert "c.bin" not in first
    rc = _fill_dispatch([
        "restore", str(dest), "--test-iso", str(vol1),
        "--all-disks", "--unskip-volume", "0",
    ])
    assert rc == 0
    assert _restored_files(dest) == expected


def test_learn_disc_keeps_explained_fill_session():
    """
    Input: volume 0 manifests for sessions 0 and 2, session 1 on volume 1
    Output: learn_disc keeps the fill session and ignores a forged later slot
    """
    def _full(path, session, checksum):
        return ManifestEntry(
            path=path, type="full", result_checksum=checksum, full_size_bytes=1,
            file=f"session_{session:03d}/full/{checksum}",
        )

    def _manifest(session, entries):
        return Manifest(
            version=2, session=session, timestamp="t", source="", label="FS",
            based_on_session=None, encryption={}, entries=list(entries),
            deleted=[], manifest_checksum="",
        )

    catalog = ArchiveCatalog(
        "fill-learn", 1, 1,
        {
            0: CatalogDisc(0, "FS", 0, 0),
            1: CatalogDisc(1, "FS", 1, 1),
        },
        {
            "a.bin": [PathVersion(0, 0, "full", "a", 1, "t", 0)],
            "b.bin": [PathVersion(1, 1, "full", "b", 1, "t", 1)],
        },
        True,
    )
    marker = VolumeMarker("fill-learn", 0, 0, "now", "", session_ranges=[[0, 0], [2, 2]])
    changed = learn_disc(catalog, marker, "FS", [
        _manifest(0, [_full("a.bin", 0, "a")]),
        _manifest(2, [_full("c.bin", 2, "c")]),
        _manifest(5, [_full("forged.bin", 5, "x")]),
    ])
    assert "c.bin" in changed
    assert "forged.bin" not in catalog.history
    assert [version.session for version in catalog.history["c.bin"]] == [2]
    assert catalog.discs[0].session_ranges == [[0, 0], [2, 2]]

