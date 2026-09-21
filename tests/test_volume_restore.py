"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for multi-volume restore: 3-volume ISOBackend fixture, restore-state
    skip of already-applied volumes, missing-middle-volume reporting, and
    the unmarked legacy path leaving restore-state untouched.
"""
# Imports
import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock

from oddarchiver.cli import _run_restore
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.ledger import ledger_dir
from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest
from oddarchiver.restore import restore
from oddarchiver.restore_state import load_restore_state, state_path
from oddarchiver.session import build_staging
from oddarchiver.volume import VolumeMarker, write_volume_marker

# Globals
_DISC = 100_000
_FILE_BYTES = 60_000
_ARCHIVE_ID = "test-archive-3vol"
_LABELS = ("VOL0", "VOL1", "VOL2")
_CREATED_AT = "2026-09-16T00:00:00Z"
_crypto = NullCrypto()


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
    """Matching session.py _blob_id: sha256(session_n:rel_path)."""
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _build_three_volumes(tmp_path: Path) -> tuple[Path, list[Path]]:
    """
    Input:  tmp_path - pytest temp directory
    Output: (source, iso_paths) for a 3-volume marked archive
    Details:
        Three 60 KB files on a 100000-byte ISOBackend force build_staging
        to return 3 chunks. Each chunk is burned to its own ISO with a
        VolumeMarker: volume 0 first_session=0, later volumes increment
        volume_index / first_session and point previous_volume_label at
        the prior volume's UDF label.
    """
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


def test_three_volume_restore_matches_source(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Restore oldest-first, one invocation per ISO. Dest tree must
        match the source byte-for-byte (restore-state file excluded).
    """
    source, isos = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest"
    for iso in isos:
        result = restore(dest, ISOBackend(iso, disc_size=_DISC), _crypto)
        assert result.failures == 0
        assert result.needed is None
    assert _tree_files(dest) == _tree_files(source)
    state = load_restore_state(dest, _ARCHIVE_ID)
    assert state is not None
    assert state.archive_id == _ARCHIVE_ID
    assert state.applied_volumes == [0, 1, 2]


def test_rerun_skips_already_applied_volume(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        After restoring volume 0, a second restore of the same ISO must
        leave restore-state applied_volumes at [0] and not rewrite dest.
    """
    _source, isos = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest_partial"
    backend = ISOBackend(isos[0], disc_size=_DISC)
    first = restore(dest, backend, _crypto)
    assert first.failures == 0
    assert first.restored >= 1
    state_file = state_path(dest, _ARCHIVE_ID)
    state_first = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_first["applied_volumes"] == [0]
    dest_file = dest / "a.bin"
    mtime_before = dest_file.stat().st_mtime_ns
    time.sleep(0.01)
    second = restore(dest, backend, _crypto)
    assert second.restored == 0
    assert second.failures == 0
    state_second = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_second["applied_volumes"] == [0]
    assert dest_file.stat().st_mtime_ns == mtime_before


def test_restore_skips_missing_middle_volume(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Restore volumes 0 and 2 only. Files fully on 0 and 2 appear in
        dest. The volume-1 file is absent. restore() reports needing
        volume 1 by its label, and that is not a failure.
    """
    source, isos = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest_gap"
    r0 = restore(dest, ISOBackend(isos[0], disc_size=_DISC), _crypto)
    r2 = restore(dest, ISOBackend(isos[2], disc_size=_DISC), _crypto)
    assert r0.failures == 0
    assert r2.failures == 0
    assert r2.needed is not None
    assert r2.needed.volume_index == 1
    assert r2.needed.label == "VOL1"
    assert (dest / "a.bin").read_bytes() == (source / "a.bin").read_bytes()
    assert (dest / "sub" / "c.bin").read_bytes() == (source / "sub" / "c.bin").read_bytes()
    assert not (dest / "b.bin").exists()
    state = load_restore_state(dest, _ARCHIVE_ID)
    assert state is not None
    assert state.applied_volumes == [0, 2]


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
        A marked volume whose only entry is a delta, restored with empty
        dest, must not fail or write the file. It reports needing the
        previous volume by label.
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
    assert result.needed is not None
    assert result.needed.volume_index == 0
    assert result.needed.label == "VOL0"
    assert not (dest / "changed.txt").exists()


def test_cli_prints_needed_volume_message(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        _run_restore on volume 2 with an empty dest must exit 0 and print
        the insert-volume-1 message. Missing earlier volumes are not
        failures.
    """
    _source, isos = _build_three_volumes(tmp_path)
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
    out = capsys.readouterr().out
    assert "Restore needs volume 1" in out
    assert "VOL1" in out
    assert "oddarchiver restore" in out
    assert (dest / "sub" / "c.bin").exists()
    assert not (dest / "a.bin").exists()
    assert not (dest / "b.bin").exists()
