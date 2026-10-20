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
from oddarchiver.catalog import remember_encryption_mode
from oddarchiver.cli import _run_restore
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.ledger import ledger_dir
from oddarchiver.manifest import Manifest, ManifestEntry, read_manifest, write_manifest
from oddarchiver.restore import restore
from oddarchiver.restore_state import load_restore_state
from oddarchiver.session import build_staging
from oddarchiver.volume import (
    VolumeMarker, build_volume_baseline, write_volume_marker,
)
from tests.helpers import _write_prior_baseline

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
    (source / "a.bin").write_bytes(b"A" * _FILE_BYTES)
    (source / "b.bin").write_bytes(b"B" * _FILE_BYTES)
    sub = source / "sub"
    sub.mkdir()
    (sub / "c.bin").write_bytes(b"C" * _FILE_BYTES)

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
    assert (dest / "sub" / "c.bin").read_bytes() == b"C" * _FILE_BYTES
    assert not (dest / "a.bin").exists()
    assert not (dest / "b.bin").exists()
    restored = {
        str(path.relative_to(dest)): path.read_bytes()
        for path in dest.rglob("*") if path.is_file()
    }
    assert restored == {"sub/c.bin": b"C" * _FILE_BYTES}


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
