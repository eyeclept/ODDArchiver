"""Test catalog learning from old baselines and current disc manifests."""
# Imports
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from oddarchiver.catalog import (
    ArchiveCatalog, CatalogDisc, build_archive_catalog, disc_name, learn_disc,
    volume_for_session,
)
from oddarchiver.cli import dispatch
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, load_session_manifests
from oddarchiver.volume import (
    PathVersion, VolumeBaseline, VolumeMarker, build_volume_baseline,
    read_volume_baseline, read_volume_marker, write_volume_baseline,
)

# Globals
logger = logging.getLogger(__name__)
_log = logger


# Functions
class BaselineBackend:
    """Serve a baseline and disc label to the catalog reader."""

    def __init__(self, baseline: bytes, label: str):
        self.baseline = baseline
        self.label = label

    def read_paths(self, paths):
        """Input: paths. Output: matching control files. Details: Use supplied baseline."""
        return {"volume_baseline.json": self.baseline} if "volume_baseline.json" in paths else {}

    def mediainfo(self):
        """Input: None. Output: disc label. Details: Return a simple record."""
        return SimpleNamespace(label=self.label)


def manifest(session, entries=(), deleted=(), suspect=False):
    """Input: session and events. Output: one test manifest. Details: Set v1 metadata."""
    return Manifest(
        version=1, session=session, timestamp=f"2026-01-{session + 1:02d}T00:00:00Z",
        source="/source", label="TEST", based_on_session=session - 1,
        encryption={}, entries=list(entries), deleted=list(deleted),
        manifest_checksum="", suspect=suspect,
    )


def test_v1_unknown_previous_range_does_not_map_placeholder(tmp_path, monkeypatch):
    """Input: old state and origin. Output: unknown origin disc and unverified event."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    baseline = json.dumps({
        "state": {"changed.txt": "old-hash"},
        "origin": {"changed.txt": 0},
    }).encode()
    marker = VolumeMarker("archive", 2, 2, "now", "DISC1", format=1)
    catalog = build_archive_catalog(
        BaselineBackend(baseline, "DISC2"), None, [manifest(2)], marker,
    )
    assert volume_for_session(catalog, 0) is None
    assert catalog.history["changed.txt"] == [
        PathVersion(0, None, "full", "old-hash", 0, "", 0, verified=False),
    ]
    assert catalog.discs[1].label == "DISC1"
    assert catalog.discs[1].first_session is None
    assert disc_name(catalog, None, 0) == "the disc that holds session 0"
    assert not catalog.complete


def test_learn_disc_replaces_placeholder_with_real_delta(tmp_path, monkeypatch):
    """Input: v1 placeholder and disc 1. Output: verified delta on disc 1."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    baseline = json.dumps({
        "state": {"changed.txt": "new-hash"},
        "origin": {"changed.txt": 1},
    }).encode()
    marker = VolumeMarker("archive", 2, 2, "now", "DISC1", format=1)
    catalog = build_archive_catalog(
        BaselineBackend(baseline, "DISC2"), None, [manifest(2)], marker,
    )
    previous = VolumeMarker("archive", 1, 1, "earlier", "DISC0", format=1)
    delta = ManifestEntry(
        path="changed.txt", type="delta", result_checksum="new-hash",
        full_size_bytes=57, source_checksum="old-hash",
        delta_file="session_001/deltas/" + "a" * 64,
    )
    assert learn_disc(catalog, previous, "DISC1", [manifest(1, [delta])]) == {"changed.txt"}
    assert catalog.history["changed.txt"] == [
        PathVersion(1, 1, "delta", "new-hash", 57,
                    "2026-01-02T00:00:00Z", 1, verified=True),
    ]
    assert catalog.discs[1] == CatalogDisc(1, "DISC1", 1, 1)
    assert learn_disc(catalog, previous, "DISC1", [manifest(1, [delta])]) == set()


def test_learn_disc_adds_events_and_resolves_other_unknown_paths():
    """Input: incomplete history and mixed manifests. Output: sorted real events."""
    catalog = ArchiveCatalog(
        "archive", 2, 3,
        {0: CatalogDisc(0, "OLD", None, None)},
        {
            "changed.txt": [PathVersion(1, None, "full", "seed", 0, "", 1, verified=False)],
            "other.txt": [PathVersion(0, None, "full", "other", 3, "old", 0)],
        },
        False,
    )
    marker = VolumeMarker("archive", 0, 0, "earlier", "")
    full = ManifestEntry(
        path="changed.txt", type="full", result_checksum="first",
        full_size_bytes=8, file="session_000/full/" + "b" * 64,
    )
    delta = ManifestEntry(
        path="changed.txt", type="delta", result_checksum="second",
        full_size_bytes=10, delta_file="session_001/deltas/" + "c" * 64,
    )
    suspect = manifest(3, [ManifestEntry(
        path="bad.txt", type="full", result_checksum="bad",
        full_size_bytes=1, file="session_003/full/" + "d" * 64,
    )], suspect=True)
    changed = learn_disc(
        catalog, marker, "FIRST",
        [manifest(2, deleted=["deleted.txt"]), suspect,
         manifest(1, [delta]), manifest(0, [full])],
    )
    assert changed == {"changed.txt", "deleted.txt", "other.txt"}
    assert catalog.discs[0] == CatalogDisc(0, "FIRST", 0, 2)
    assert [(v.session, v.kind, v.blob_session, v.verified)
            for v in catalog.history["changed.txt"]] == [
                (0, "full", 0, True), (1, "delta", 1, True),
            ]
    assert catalog.history["deleted.txt"] == [
        PathVersion(2, 0, "deleted", "", 0, "2026-01-03T00:00:00Z", None),
    ]
    assert catalog.history["other.txt"][0].volume_index == 0
    assert "bad.txt" not in catalog.history


def test_baseline_reader_defaults_missing_verified_to_true():
    """Input: old format 2 history. Output: a verified path version."""
    raw = {
        "version": 2,
        "volumes": [],
        "history": {"old.txt": [{
            "session": 0, "volume_index": 0, "kind": "full",
            "result_checksum": "hash", "full_size_bytes": 7,
            "timestamp": "old", "blob_session": 0,
        }]},
        "state": {"old.txt": "hash"},
        "origin": {"old.txt": 0},
    }
    baseline = read_volume_baseline(
        BaselineBackend(json.dumps(raw).encode(), "OLD"), None,
    )
    assert baseline is not None
    assert baseline.history["old.txt"] == [
        PathVersion(0, 0, "full", "hash", 7, "old", 0, verified=True),
    ]


def test_unverified_v1_placeholder_survives_baseline_roundtrip(tmp_path):
    """Input: v1 state and origin. Output: serialized unverified placeholder."""
    prior = VolumeBaseline(
        version=1, volumes=[], history={},
        state={"old.txt": "checksum"}, origin={"old.txt": 0},
    )
    marker = VolumeMarker("archive", 1, 1, "now", "OLD", format=1)
    baseline = build_volume_baseline(prior, [manifest(1)], marker, "NEW")
    write_volume_baseline(tmp_path, baseline, None)
    raw = (tmp_path / "volume_baseline.json").read_bytes()
    assert json.loads(raw)["history"]["old.txt"][0]["verified"] is False  # scbc ignore[is-true-false-strict] JSON value requires an exact bool
    decoded = read_volume_baseline(BaselineBackend(raw, "NEW"), None)
    assert decoded == baseline
    assert not decoded.history["old.txt"][0].verified


def _assert_catalog_roundtrip(
    before: ArchiveCatalog, after: ArchiveCatalog, staging: Path,
) -> None:
    """Input: catalogs and baseline directory. Output: equal, verified histories."""
    assert before == after
    assert all(v.verified for versions in after.history.values() for v in versions)
    assert all(v["verified"] for versions in json.loads(
        (staging / "volume_baseline.json").read_text()
    )["history"].values() for v in versions)


def test_sync_format2_catalog_unchanged_after_baseline_roundtrip(tmp_path, monkeypatch):
    """Input: real two-disc sync. Output: equal catalogs after baseline rewrite."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * 80_000)
    (source / "b.bin").write_bytes(b"B" * 80_000)
    iso = tmp_path / "archive.iso"
    from oddarchiver.cli import build_parser
    args = build_parser().parse_args([
        "init", str(source), "--test-iso", str(iso),
        "--disc-size", "100000", "--label", "TEST",
    ])
    assert dispatch(args) == 0
    (source / "a.bin").unlink()
    sync_args = build_parser().parse_args([
        "sync", str(source), "--test-iso", str(tmp_path / "archive.vol1.iso"),
        "--disc-size", "100000",
    ])
    assert dispatch(sync_args) == 0
    disc = ISOBackend(tmp_path / "archive.vol1.iso", disc_size=100_000)
    marker = read_volume_marker(disc)
    assert marker is not None and marker.format == 2
    manifests = load_session_manifests(disc, None, start_session=marker.first_session)
    baseline = read_volume_baseline(disc, None)
    assert baseline is not None
    before = build_archive_catalog(disc, None, manifests, marker)
    staging = tmp_path / "staging"
    staging.mkdir()
    write_volume_baseline(staging, baseline, None, marker)
    after = build_archive_catalog(
        BaselineBackend((staging / "volume_baseline.json").read_bytes(), disc.mediainfo().label),
        None, manifests, marker,
    )
    _assert_catalog_roundtrip(before, after, staging)
