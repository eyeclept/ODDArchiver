"""
Tests for archive catalogs and local disc records.
"""
# Imports
import argparse
import json
import logging
import os
from functools import partial
from types import SimpleNamespace
from typing import Callable

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.catalog import (
    ArchiveCatalog, CatalogDisc, build_archive_catalog, catalog_tree, disc_name,
    highest_seen_volume, remember_discs, volume_for_session, _memory_path,
)
from oddarchiver.cli import _ControlFiles, _run_ls, build_parser
from oddarchiver.manifest import Manifest, ManifestEntry, build_disc_tree
from oddarchiver.volume import PathVersion, VolumeMarker

# Globals
logger = logging.getLogger(__name__)
_log = logger


# Functions


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Keep cache and state files out of the user home directories.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache))


class StubBackend:
    """Provide disc control files and a label for catalog tests."""

    def __init__(self, files: dict[str, bytes], label: str):
        self.files = files
        self.label = label

    def read_paths(self, paths: list[str]) -> dict[str, bytes]:
        """Input: paths. Output: known files. Details: Omit missing files."""
        return {path: self.files[path] for path in paths if path in self.files}

    def mediainfo(self):
        """Input: None. Output: disc label. Details: Use a small test record."""
        return SimpleNamespace(label=self.label)


def manifest(session: int, entries=(), deleted=(), suspect=False) -> Manifest:
    """
    Input:  session, entries, deleted, suspect - one test session
    Output: Manifest with the supplied events
    """
    return Manifest(
        version=1, session=session, timestamp=f"2026-01-{session + 1:02d}T00:00:00Z",
        source="/src", label="TEST", based_on_session=session - 1,
        encryption={}, entries=list(entries), deleted=list(deleted),
        manifest_checksum="", suspect=suspect,
    )


def entry(path: str, session: int, size: int = 12) -> ManifestEntry:
    """
    Input:  path, session, size - file event fields
    Output: File entry with a session-specific blob
    """
    return ManifestEntry(
        path=path, type="full", result_checksum=f"hash-{session}",
        full_size_bytes=size, file=f"session_{session:03d}/full/" + "a" * 64,
    )


def test_disc_tree_filters_by_real_session_number():
    """
    Input:  later-disc manifests
    Output: The state at session 4 excludes the write at session 5
    """
    manifests = [manifest(4, [entry("early.txt", 4)]),
                 manifest(5, [entry("later.txt", 5)])]
    assert [e.path for e in build_disc_tree(manifests, at_session=4)] == ["early.txt"]
    assert build_disc_tree(manifests, at_session=3) == []


def _assert_catalog_history(catalog: ArchiveCatalog) -> None:
    """Input: catalog. Output: None. Check version history and catalog status."""
    assert catalog.complete
    assert catalog.last_session == 3
    assert [(v.session, v.blob_session) for v in catalog.history["a.txt"]] == [
        (0, 0), (1, 1), (2, 2),
    ]
    assert "bad.txt" not in catalog.history


def _assert_catalog_tree(catalog: ArchiveCatalog) -> None:
    """Input: catalog. Output: None. Check current tree entries."""
    tree = {item.path: item for item in catalog_tree(catalog)}
    assert list(tree) == ["a.txt", "gone.txt", "new.txt"]
    assert tree["a.txt"].size == 17
    assert tree["a.txt"].origin_session == 2
    assert tree["a.txt"].volume_index == 1
    assert tree["gone.txt"].deleted
    assert tree["gone.txt"].deleted_session == 1
    assert tree["gone.txt"].origin_session == 0
    assert tree["gone.txt"].size == 9
    assert tree["gone.txt"].volume_label == "NEW"


def _assert_catalog_earlier_tree(catalog: ArchiveCatalog) -> None:
    """Input: catalog. Output: None. Check the tree before later changes."""
    earlier = {item.path: item for item in catalog_tree(catalog, at_session=0)}
    assert not earlier["gone.txt"].deleted
    assert earlier["gone.txt"].volume_index == 0
    assert "new.txt" not in earlier


def test_catalog_tree_tracks_versions_and_tombstones(tmp_path):
    """
    Input:  version 2 baseline and source manifests
    Output: Old and new files, timestamps, origin sessions, and tombstones
    """
    baseline = {
        "version": 2,
        "volumes": [{"volume_index": 0, "label": "OLD", "first_session": 0,
                     "last_session": 0}],
        "history": {
            "a.txt": [{"session": 0, "volume_index": 0, "kind": "full",
                       "result_checksum": "old", "full_size_bytes": 7,
                       "timestamp": "old-time", "blob_session": 0}],
            "gone.txt": [{"session": 0, "volume_index": 0, "kind": "full",
                          "result_checksum": "gone", "full_size_bytes": 9,
                          "timestamp": "old-time", "blob_session": 0}],
        },
        "state": {"a.txt": "old", "gone.txt": "gone"},
        "origin": {"a.txt": 0, "gone.txt": 0},
    }
    backend = StubBackend({"volume_baseline.json": json.dumps(baseline).encode()}, "NEW")
    marker = VolumeMarker("archive-1", 1, 1, "now", "OLD")
    source = [manifest(1, [entry("a.txt", 1), entry("new.txt", 1)], ["gone.txt"]),
              manifest(2, [entry("a.txt", 2, 17)]),
              manifest(3, [entry("bad.txt", 3)], suspect=True)]
    catalog = build_archive_catalog(backend, None, source, marker)
    _assert_catalog_history(catalog)
    _assert_catalog_tree(catalog)
    _assert_catalog_earlier_tree(catalog)


def _assert_unknown_v1_disc(unknown: ArchiveCatalog) -> None:
    """Input: unknown catalog. Output: None. Check unresolved disc details."""
    assert not unknown.complete
    assert unknown.history["old.txt"][0] == PathVersion(
        0, None, "full", "old-hash", 0, "", 0, verified=False,
    )
    assert disc_name(unknown, None, 0) == "the disc that holds session 0"
    assert highest_seen_volume("archive-v1") is None


def _assert_known_v1_disc(known: ArchiveCatalog, tmp_path) -> None:
    """Input: known catalog and temporary path. Output: None. Check stored disc."""
    assert known.complete
    assert volume_for_session(known, 0) == 0
    assert catalog_tree(known)[0].volume_label == "FIRST"
    remember_discs(known)
    path = tmp_path / "state" / "oddarchiver" / "archive-archive-v1.json"
    stored = json.loads(path.read_text())
    assert stored["highest_volume_index"] == 1
    assert set(stored["discs"]) == {"0", "1"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert highest_seen_volume("archive-v1") == 1


def test_v1_catalog_learns_earlier_disc_from_local_memory(tmp_path):
    """
    Input:  v1 baseline and local record from the first disc
    Output: Unknown disc before learning, then a named disc after learning
    """
    old = StubBackend({}, "FIRST")
    first = VolumeMarker("archive-v1", 0, 0, "now", "")
    later = StubBackend({"volume_baseline.json": json.dumps({
        "state": {"old.txt": "old-hash"}, "origin": {"old.txt": 0},
    }).encode()}, "SECOND")
    second = VolumeMarker("archive-v1", 1, 1, "later", "FIRST")
    unknown = build_archive_catalog(later, None, [manifest(1)], second)
    _assert_unknown_v1_disc(unknown)
    remember_discs(build_archive_catalog(old, None, [manifest(0, [entry("old.txt", 0)])], first))
    assert highest_seen_volume("archive-v1") == 0
    known = build_archive_catalog(later, None, [manifest(1)], second)
    _assert_known_v1_disc(known, tmp_path)


def test_memory_keeps_higher_disc_and_rejects_unreadable_record(tmp_path, caplog):
    """
    Input: Existing higher disc, then a lower disc and a broken file
    Output: Highest index survives. A broken file is never overwritten.
    """
    catalog = ArchiveCatalog("memory-test", 2, 2, {
        2: CatalogDisc(2, "LAST", 2, 2),
    }, {}, True)
    remember_discs(catalog)
    catalog.source_volume_index = 0
    catalog.discs = {0: CatalogDisc(0, "FIRST", 0, 0)}
    remember_discs(catalog)
    assert highest_seen_volume("memory-test") == 2
    path = tmp_path / "state" / "oddarchiver" / "archive-memory-test.json"
    assert set(json.loads(path.read_text())["discs"]) == {"0", "2"}
    path.write_bytes(b"broken")
    with pytest.raises(RuntimeError, match="Move or delete this file") as error:
        highest_seen_volume("memory-test")
    assert str(path) in str(error.value)
    with pytest.raises(RuntimeError, match="Move or delete this file"):
        remember_discs(catalog)
    assert path.read_bytes() == b"broken"
    assert "Cannot read local disc record" in caplog.text


def test_long_archive_id_has_bounded_file_name():
    """Input: Long safe archive ID. Output: A bounded hashed file name."""
    path = _memory_path("x" * 10000)
    assert len(path.name) < 100
    assert path.name != "archive-" + "x" * 10000 + ".json"


def _record_read_paths(
    reads: list[str], original_read: Callable[[list[str]], dict[str, bytes]], paths: list[str],
) -> dict[str, bytes]:
    """Input: read log, original reader, paths. Output: original files."""
    reads.extend(paths)
    return original_read(paths)


def test_ls_skips_baseline_and_unchanged_memory_write(monkeypatch, tmp_path, capsys):
    """
    Input: A marked disc with baseline and a printable Unicode path
    Output: No baseline reads and no second local record write
    """
    backend = StubBackend({
        "volume.json": json.dumps({
            "archive_id": "recording", "volume_index": 1, "first_session": 3,
            "created_at": "now", "previous_volume_label": "FIRST",
        }).encode(),
        "volume_baseline.json": b"broken",
    }, "SECOND\x1b[31m")
    reads = []
    infos = []
    original_read = backend.read_paths
    recording_read = partial(_record_read_paths, reads, original_read)
    def recording_info():
        infos.append(1)
        return SimpleNamespace(label=backend.label, session_count=1)
    backend.read_paths = recording_read
    backend.mediainfo = recording_info
    writes = []
    real_replace = os.replace
    def counting_replace(src, dst):
        writes.append(str(dst))
        return real_replace(src, dst)
    monkeypatch.setattr("oddarchiver.catalog.os.replace", counting_replace)
    monkeypatch.setattr("oddarchiver.cli._make_backend", lambda args: backend)
    marker = VolumeMarker("recording", 1, 3, "now", "FIRST", format=1)
    monkeypatch.setattr("oddarchiver.cli._crypto_and_manifests", lambda *a, **k: (
        None, [manifest(3, [entry("café\x1b[2J.txt", 3)])],
        _ControlFiles(backend.read_paths([
            "volume.json", "session_003/enc_mode.json",
            "session_003/manifest.enc", "session_003/manifest.json",
        ]), marker),
    ))
    monkeypatch.setattr("oddarchiver.cli._check_loaded_manifests", lambda *a, **k: None)
    args = argparse.Namespace(key=None, all_disks=False, session=None, path=None,
                              show_deleted=False)
    assert _run_ls(args) == 0
    assert len(writes) == 1
    backend.read_paths = recording_read
    assert _run_ls(args) == 0
    assert len(writes) == 1
    assert "volume_baseline.enc" not in reads
    assert "volume_baseline.json" not in reads
    assert reads.count("volume.json") == 2
    assert len(infos) == 2
    assert "café\\u001b[2J.txt" in capsys.readouterr().out


def test_ls_all_disks_reads_marker_and_label_once(monkeypatch, capsys):
    """Input: Marked disc. Output: One marker read and one media info call."""
    marker = VolumeMarker("read-once", 0, 0, "now", "")
    backend = StubBackend({
        "volume.json": json.dumps(vars(marker)).encode(),
    }, "LABEL\x1b[31m")
    reads = []
    infos = []
    original_read = backend.read_paths
    recording_read = partial(_record_read_paths, reads, original_read)
    backend.read_paths = recording_read
    backend.mediainfo = lambda: (infos.append(1) or SimpleNamespace(
        label=backend.label, session_count=1,
    ))
    monkeypatch.setattr("oddarchiver.cli._make_backend", lambda args: backend)
    monkeypatch.setattr("oddarchiver.cli._crypto_and_manifests", lambda *a, **k: (
        None, [manifest(0, [entry("a.txt", 0)])],
        _ControlFiles(backend.read_paths([
            "volume.json", "session_000/enc_mode.json",
            "session_000/manifest.enc", "session_000/manifest.json",
        ]), marker),
    ))
    monkeypatch.setattr("oddarchiver.cli._check_loaded_manifests", lambda *a, **k: None)
    args = argparse.Namespace(key=None, all_disks=True, session=None, path=None,
                              show_deleted=False)
    assert _run_ls(args) == 0
    assert reads.count("volume.json") == 1
    assert len(infos) == 1
    out = capsys.readouterr()
    assert "LABEL\\u001b[31m" in out.out + out.err
    assert "\x1b" not in out.out + out.err

def test_legacy_catalog_has_no_disc_record(tmp_path):
    """
    Input:  Disc with no marker
    Output: Local path history and no local state file
    """
    catalog = build_archive_catalog(
        StubBackend({}, "LEGACY"), None, [manifest(0, [entry("a.txt", 0)])], None,
    )
    remember_discs(catalog)
    assert catalog.archive_id is None
    assert catalog.complete
    assert catalog.discs == {}
    assert catalog_tree(catalog)[0].volume_index is None
    assert list((tmp_path / "state").rglob("archive-*.json")) == []


def _assert_v1_listing_sequence(args: argparse.Namespace, capsys) -> None:
    """Input: listing arguments and output capture. Output: None. Check three listings."""
    assert _run_ls(args) == 0
    before = capsys.readouterr()
    assert "the disc that holds session 0" in before.out
    assert "disc names for some older sessions are not known" in before.err
    args.device = "first"
    args.all_disks = False
    assert _run_ls(args) == 0
    local = capsys.readouterr()
    assert "Note: This list shows this disc only." in local.err
    args.device = "second"
    args.all_disks = True
    assert _run_ls(args) == 0
    after = capsys.readouterr()
    assert "FIRST (disc 0)" in after.out
    assert "disc names for some older sessions are not known" not in after.err


def test_ls_v1_baseline_names_old_disc_after_local_listing(monkeypatch, capsys):
    """
    Input:  Two simulated discs with a v1 baseline on the second
    Output: The first listing teaches the second listing its disc name
    """
    first = StubBackend({}, "FIRST")
    second = StubBackend({"volume_baseline.json": json.dumps({
        "state": {"old.txt": "old-hash"}, "origin": {"old.txt": 0},
    }).encode()}, "SECOND")
    first_marker = VolumeMarker("archive-cli-v1", 0, 0, "now", "")
    second_marker = VolumeMarker("archive-cli-v1", 1, 1, "later", "FIRST")
    sources = {
        "first": (first, first_marker, [manifest(0, [entry("old.txt", 0)])]),
        "second": (second, second_marker, [manifest(1, [entry("new.txt", 1)])]),
    }
    monkeypatch.setattr("oddarchiver.cli._make_backend", lambda args: sources[args.device][0])
    monkeypatch.setattr(
        "oddarchiver.cli._crypto_and_manifests",
        lambda backend, **kwargs: (
            None, next(item[2] for item in sources.values() if item[0] is backend),
            _ControlFiles(
                backend.read_paths(["volume.json"]),
                next(item[1] for item in sources.values() if item[0] is backend),
            ),
        ),
    )
    monkeypatch.setattr("oddarchiver.cli._check_loaded_manifests", lambda *a, **k: None)
    first.mediainfo = lambda: SimpleNamespace(label="FIRST", session_count=1)
    second.mediainfo = lambda: SimpleNamespace(label="SECOND", session_count=1)
    args = argparse.Namespace(device="second", key=None, session=None, path=None,
                              show_deleted=False, all_disks=True)
    _assert_v1_listing_sequence(args, capsys)


def test_ls_parser_accepts_all_disks():
    """
    Input:  ls arguments with --all-disks
    Output: A namespace that requests archive-wide listing
    """
    args = build_parser().parse_args(["ls", "--all-disks"])
    assert args.all_disks
