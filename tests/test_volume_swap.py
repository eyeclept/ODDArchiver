"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for the interactive disc-swap burn loop. Covers ISOBackend
    auto-split across three volumes, unconditional VolumeMarker on a
    non-splitting init, abort-after-volume-0 via simulated 'q' input,
    and resume of leftover files on a later sync.
"""
# Imports
import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.cli import _volume_iso_path, dispatch
from oddarchiver.disc import ISOBackend, parse_disc_size
from oddarchiver.volume import read_volume_marker

# Globals
SMALL_DISC = 50 * 2**20  # 50 MiB. Room for a non-splitting session 0.
_FILE_BYTES = 80_000
_DISC_THAT_SPLITS = 100_000  # 0.95 * this holds one 80 KB file, not two.


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides for the Namespace
    Output: argparse.Namespace with sensible defaults for test commands
    """
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "TEST",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "50mb",
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
        "mirror": None,
        "recover_scan": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Return {relative path: contents} for every file under root."""
    out: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


def _files_on_backend(backend: ISOBackend) -> dict[str, bytes]:
    """
    Input:  backend - ISOBackend whose session manifests should be scanned
    Output: {path: blob bytes} for every full entry on this volume
    Details:
        Probes session_NNN/manifest.json directly so a later volume that
        starts at session_001 (no session_000) is still readable.
    """
    found = backend.read_paths(
        [f"session_{s:03d}/manifest.json" for s in range(256)]
    )
    files: dict[str, bytes] = {}
    for _manifest_path, data in found.items():
        manifest = json.loads(data)
        for entry in manifest.get("entries", []):
            blob_rel = entry.get("file") or ""
            if not blob_rel:
                continue
            files[entry["path"]] = backend.read_path(blob_rel)
    return files


def _union_volume_files(backends: list[ISOBackend]) -> dict[str, bytes]:
    """Merge per-volume file maps and fail if any path appears twice."""
    union: dict[str, bytes] = {}
    for backend in backends:
        files = _files_on_backend(backend)
        overlap = set(union) & set(files)
        assert not overlap, f"duplicate paths across volumes: {sorted(overlap)}"
        union.update(files)
    return union


class _PromptableBackend:
    """
    Delegates to ISOBackend but is not one, so the disc-swap prompt runs
    while burns still land on a real test ISO.
    """

    def __init__(self, iso_path: Path, disc_size: int) -> None:
        self._inner = ISOBackend(iso_path, disc_size=disc_size)
        self.disc_size = disc_size

    def init(self, staging, label, expected_session_count=0):
        return self._inner.init(staging, label, expected_session_count)

    def append(self, staging, label, expected_session_count):
        return self._inner.append(staging, label, expected_session_count)

    def mediainfo(self):
        return self._inner.mediainfo()

    def read_paths(self, paths):
        return self._inner.read_paths(paths)

    def read_path(self, path):
        return self._inner.read_path(path)


def _promptable_factory(args: argparse.Namespace) -> _PromptableBackend:
    """Build a _PromptableBackend from a parsed CLI namespace."""
    size_str = getattr(args, "disc_size", "25gb")
    return _PromptableBackend(Path(args.test_iso), parse_disc_size(size_str))


# Tests


def test_volume_iso_path_inserts_vol_before_iso_suffix():
    """
    Input:  None
    Output: None
    Details:
        archive.iso volume 1 -> archive.vol1.iso; volume 2 -> archive.vol2.iso.
        Always derived from the original path, not from a previous .volN name.
    """
    original = Path("/tmp/archive.iso")
    assert _volume_iso_path(original, 1) == Path("/tmp/archive.vol1.iso")
    assert _volume_iso_path(original, 2) == Path("/tmp/archive.vol2.iso")
    assert _volume_iso_path(Path("/tmp/foo.ISO"), 1) == Path("/tmp/foo.vol1.ISO")


def test_init_without_split_writes_volume_marker(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A session-0 init whose changeset fits on one volume still writes a
        VolumeMarker that read_volume_marker can read back.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "archive.iso"

    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
    ))
    assert rc == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    marker = read_volume_marker(backend)
    assert marker is not None
    assert marker.volume_index == 0
    assert marker.first_session == 0
    assert marker.previous_volume_label == ""
    assert len(marker.archive_id) == 32
    assert marker.created_at


def test_init_across_three_iso_volumes_covers_source(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Three 80 KB files on a 100000-byte ISOBackend force a 3-volume
        split. archive.iso, archive.vol1.iso, and archive.vol2.iso exist;
        combined full blobs cover every source file exactly once; writing
        those blobs out reconstructs the source tree. ISOBackend must not
        prompt on stdin.
    """
    def _boom(prompt=""):
        raise AssertionError(f"ISOBackend must not call input(): {prompt!r}")

    monkeypatch.setattr("builtins.input", _boom)

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * _FILE_BYTES)
    (source / "b.bin").write_bytes(b"B" * _FILE_BYTES)
    (source / "c.bin").write_bytes(b"C" * _FILE_BYTES)
    iso = tmp_path / "archive.iso"

    rc = dispatch(_make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        disc_size=str(_DISC_THAT_SPLITS),
    ))
    assert rc == 0

    vol0 = iso
    vol1 = tmp_path / "archive.vol1.iso"
    vol2 = tmp_path / "archive.vol2.iso"
    assert vol0.is_file()
    assert vol1.is_file()
    assert vol2.is_file()

    backends = [
        ISOBackend(vol0, disc_size=_DISC_THAT_SPLITS),
        ISOBackend(vol1, disc_size=_DISC_THAT_SPLITS),
        ISOBackend(vol2, disc_size=_DISC_THAT_SPLITS),
    ]
    union = _union_volume_files(backends)
    assert union == _tree_bytes(source)

    dest = tmp_path / "restored"
    dest.mkdir()
    for rel, data in union.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert _tree_bytes(dest) == _tree_bytes(source)

    marker0 = read_volume_marker(backends[0])
    marker1 = read_volume_marker(backends[1])
    marker2 = read_volume_marker(backends[2])
    assert marker0 is not None and marker1 is not None and marker2 is not None
    assert marker0.volume_index == 0
    assert marker1.volume_index == 1
    assert marker2.volume_index == 2
    assert marker0.first_session == 0
    assert marker1.first_session == 1
    assert marker2.first_session == 2
    assert marker0.archive_id == marker1.archive_id == marker2.archive_id
    assert marker0.previous_volume_label == ""
    assert marker1.previous_volume_label == "TEST"
    assert marker2.previous_volume_label == "TEST"


def test_abort_after_volume_0_then_sync_resumes_leftovers(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Simulating 'q' after volume 0 burns leaves volume 0's files
        committed and cached. A later sync against the same ISO picks up
        only the leftover files and does not re-burn already-committed
        paths onto volume 0.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        "oddarchiver.cli.CacheManager",
        lambda *a, **k: CacheManager(cache_dir),
    )

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * _FILE_BYTES)
    (source / "b.bin").write_bytes(b"B" * _FILE_BYTES)
    iso = tmp_path / "archive.iso"
    args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        disc_size=str(_DISC_THAT_SPLITS),
    )

    with patch("oddarchiver.cli._make_backend", _promptable_factory), \
         patch("builtins.input", return_value="q"):
        rc = dispatch(args)
    assert rc == 1
    assert iso.is_file()
    assert not (tmp_path / "archive.vol1.iso").exists()

    vol0 = ISOBackend(iso, disc_size=_DISC_THAT_SPLITS)
    committed = _files_on_backend(vol0)
    assert committed
    assert set(committed) <= {"a.bin", "b.bin"}
    leftover = set(_tree_bytes(source)) - set(committed)
    assert leftover

    cache = CacheManager(cache_dir)
    for rel in committed:
        assert cache.get(rel, 0) is not None
    for rel in leftover:
        assert cache.get(rel, 0) is None

    rc = dispatch(_make_args(
        command="sync",
        source=str(source),
        test_iso=str(iso),
        disc_size=str(_DISC_THAT_SPLITS),
    ))
    assert rc == 0

    vol0_after = _files_on_backend(ISOBackend(iso, disc_size=_DISC_THAT_SPLITS))
    assert set(committed).issubset(vol0_after)
    assert not leftover.intersection(vol0_after)

    later = []
    vol1_path = tmp_path / "archive.vol1.iso"
    if vol1_path.is_file():
        later.append(ISOBackend(vol1_path, disc_size=_DISC_THAT_SPLITS))
    vol2_path = tmp_path / "archive.vol2.iso"
    if vol2_path.is_file():
        later.append(ISOBackend(vol2_path, disc_size=_DISC_THAT_SPLITS))
    resumed = {}
    for backend in later:
        resumed.update(_files_on_backend(backend))
    # An empty first-chunk append on the full volume 0 is allowed; leftover
    # file bytes must still land on a later volume, never as a duplicate.
    assert set(resumed) >= leftover
    union = _union_volume_files(
        [ISOBackend(iso, disc_size=_DISC_THAT_SPLITS), *later]
    )
    assert union == _tree_bytes(source)


def test_confirm_without_swapping_disc_refuses_nonblank(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Confirming the swap prompt without inserting a blank medium leaves
        session_count != 0 on the same backend. That must raise (exit 1)
        rather than overwrite the volume just burned.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * _FILE_BYTES)
    (source / "b.bin").write_bytes(b"B" * _FILE_BYTES)
    iso = tmp_path / "archive.iso"
    args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        disc_size=str(_DISC_THAT_SPLITS),
    )

    with patch("oddarchiver.cli._make_backend", _promptable_factory), \
         patch("builtins.input", return_value=""):
        rc = dispatch(args)
    assert rc == 1
    assert iso.is_file()
    committed = _files_on_backend(ISOBackend(iso, disc_size=_DISC_THAT_SPLITS))
    assert committed
    assert not (tmp_path / "archive.vol1.iso").exists()


def test_mirror_plus_multivolume_raises(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        A splitting changeset with --mirror must fail with a clear error
        rather than attempting to mirror across a disc swap.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * _FILE_BYTES)
    (source / "b.bin").write_bytes(b"B" * _FILE_BYTES)
    iso = tmp_path / "archive.iso"
    mirror = tmp_path / "mirror.iso"

    rc = dispatch(_make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        mirror=str(mirror),
        disc_size=str(_DISC_THAT_SPLITS),
    ))
    assert rc == 1
    err = capsys.readouterr().err
    assert "mirror" in err.lower()
    assert not iso.exists()
    assert not mirror.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
