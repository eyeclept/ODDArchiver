"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for fallback after a missing disc and single-disc recovery.
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
from oddarchiver.catalog import build_archive_catalog, remember_encryption_mode
from oddarchiver.cli import _run_restore, build_parser
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import load_session_manifests, read_manifest, write_manifest
from oddarchiver.restore import restore
from oddarchiver.restore_plan import restore_archive
from oddarchiver.restore_state import load_restore_state
from oddarchiver.session import build_staging
from oddarchiver.volume import (
    VolumeMarker, build_volume_baseline, read_volume_marker,
    write_volume_baseline, write_volume_marker,
)

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
logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _isolated_cli_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache))



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
    prior=None,
):
    """Burn one marked ISO with the previous disc's baseline."""
    prev_label = "" if volume_index == 0 else _LABELS[volume_index - 1]
    marker = VolumeMarker(_ARCHIVE_ID, volume_index, session_n, _CREATED_AT, prev_label)
    manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
    if prior is not None:
        write_volume_baseline(staging, prior, _crypto, marker)
        manifest.baseline_checksum = hashlib.sha256(
            (staging / "volume_baseline.json").read_bytes(),
        ).hexdigest()
        write_manifest(staging / f"session_{session_n:03d}", manifest)
    write_volume_marker(staging, marker)
    baseline = build_volume_baseline(prior, [manifest], marker, _LABELS[volume_index])
    iso = tmp_path / f"vol{volume_index}.iso"
    backend = ISOBackend(iso, disc_size=_DISC)
    backend.init(staging, label=_LABELS[volume_index], expected_session_count=0)
    return iso, baseline


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
    assert not is_new
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
        iso0, prior = _burn_marked_volume(tmp_path, staging0, 0, 0)
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
        iso1, prior = _burn_marked_volume(tmp_path, staging1, 1, 1, prior)
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
        iso2, _prior = _burn_marked_volume(tmp_path, staging2, 2, 2, prior)
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


def _move_iso(src: Path, dst: Path) -> None:
    """Move an ISOBackend image with its session folder and metadata file."""
    src.replace(dst)
    src.with_suffix(".d").replace(dst.with_suffix(".d"))
    meta = src.with_name(src.name + ISOBackend._META_SUFFIX)
    if meta.exists():
        meta.replace(dst.with_name(dst.name + ISOBackend._META_SUFFIX))


def _catalog(backend):
    marker = read_volume_marker(backend)
    manifests = load_session_manifests(backend, _crypto, start_session=marker.first_session)
    return build_archive_catalog(backend, _crypto, manifests, marker)


class _Acquire:
    image = None

    def __init__(self, discs, missing=()):
        self.discs = discs
        self.missing = set(missing)
        self.calls = []

    def acquire(self, needed, catalog, archive_id):
        self.calls.append(dict(needed))
        index = min(needed)
        if index in self.missing:
            self.missing.remove(index)
            return None, index, False
        return _backend(self.discs[index]), None, False


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
        all_disks=True,
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


def _assert_recovery_buckets(dest: Path, result, acquisition: _Acquire) -> None:
    """Input: restore destination, result, acquisition. Output: verified recovery buckets."""
    assert result.failures == 0
    assert (dest / "kept.txt").read_bytes() == _KEEP
    assert (dest / "later.txt").read_bytes() == _LATER
    assert (dest / "stale.txt").read_bytes() == _STALE_V0
    assert not (dest / "missing.txt").exists()
    assert result.stale == [("stale.txt", 0)]
    assert result.missing == ["missing.txt"]
    assert acquisition.calls == [{0: 2}]


def test_missing_disc_recovery_buckets_restored_stale_missing(tmp_path):
    isos, _latest = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest"
    inserted = _backend(isos[2])
    acquisition = _Acquire(isos)
    result = restore_archive(
        dest, inserted, _crypto, _catalog(inserted),
        skip_volumes=[1], acquisition=acquisition,
    )
    _assert_recovery_buckets(dest, result, acquisition)
    state = load_restore_state(dest, _ARCHIVE_ID)
    assert state is not None
    assert state.missing_discs == [1]
    assert set(state.read_discs) == {0, 2}


def test_single_disc_delta_without_base_is_unresolved(tmp_path):
    isos, _latest = _build_three_volumes(tmp_path)
    dest = tmp_path / "dest_plain"
    result = restore(dest, _backend(isos[2]), _crypto)
    assert result.failures == 0
    assert result.stale == []
    assert result.missing == []
    assert {path for path, _disc in result.unresolved} == {"stale.txt", "missing.txt"}
    assert (dest / "later.txt").read_bytes() == _LATER
    assert not (dest / "stale.txt").exists()
    assert not (dest / "missing.txt").exists()


def test_missing_prior_disc_keeps_independent_later_file(tmp_path):
    isos, _latest = _build_three_volumes(tmp_path)
    inserted = _backend(isos[2])
    acquisition = _Acquire(isos)
    dest = tmp_path / "dest_gap"
    result = restore_archive(
        dest, inserted, _crypto, _catalog(inserted),
        skip_volumes=[0, 1], acquisition=acquisition,
    )
    assert result.failures == 0
    assert (dest / "later.txt").read_bytes() == _LATER
    assert result.missing == ["kept.txt", "missing.txt", "stale.txt"]
    assert acquisition.calls == []

def _assert_cli_recovery_output(out: str) -> None:
    """Input: CLI stdout. Output: verified stale and missing notices."""
    assert "stale:" in out
    assert "stale.txt" in out
    assert "session 0" in out
    assert "missing:" in out
    assert "missing.txt" in out
    assert "Restore needs volume 1" not in out


def _assert_cli_recovery_files(dest: Path) -> None:
    """Input: restore destination. Output: verified recovered file contents."""
    assert (dest / "kept.txt").read_bytes() == _KEEP
    assert (dest / "stale.txt").read_bytes() == _STALE_V0
    assert (dest / "later.txt").read_bytes() == _LATER
    assert not (dest / "missing.txt").exists()
    restored = {
        str(path.relative_to(dest)): path.read_bytes()
        for path in dest.rglob("*") if path.is_file()
    }
    assert restored == {
        "kept.txt": _KEEP,
        "stale.txt": _STALE_V0,
        "later.txt": _LATER,
    }


def test_cli_prints_stale_and_missing_table(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        The planner reports stale and missing paths with their sessions.
        Restored files match the fallback or inserted-disc bytes.
    """
    isos, _latest = _build_three_volumes(tmp_path)
    remember_encryption_mode(_ARCHIVE_ID, "none")
    dest = tmp_path / "dest_cli"
    _move_iso(isos[0], tmp_path / "archive.iso")
    _move_iso(isos[2], tmp_path / "archive.vol2.iso")
    rc = _run_restore(_restore_args(dest, tmp_path / "archive.vol2.iso", skip_volumes=[1]))
    assert rc == 0
    out = capsys.readouterr().out
    _assert_cli_recovery_output(out)
    _assert_cli_recovery_files(dest)
