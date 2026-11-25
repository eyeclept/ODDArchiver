"""Disc-aware restore selection, acquisition, resume, and blob reads."""

import hashlib
import logging
from pathlib import Path

import pytest

from oddarchiver.catalog import build_archive_catalog
from oddarchiver.cli import build_parser
from oddarchiver.crypto import KeyfileCrypto, NullCrypto
from oddarchiver.delta import compute_delta
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, load_session_manifests, write_manifest
from oddarchiver.restore import restore
from oddarchiver.restore_plan import restore_archive
from oddarchiver.restore_state import load_restore_state, state_path
from oddarchiver.volume import (
    PathVersion, VolumeBaseline, VolumeMarker, build_volume_baseline,
    read_volume_marker, write_volume_baseline, write_volume_marker,
)

logger = logging.getLogger(__name__)
_DISC = 20 * 2**20
_ID = "restore-plan-test"
_CRYPTO = NullCrypto()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _stage(tmp_path, n, full, crypto, prior=None, delta=None, deleted=(),
           legacy_baseline=None):
    """Write one real session and its disc metadata; return ISO and next baseline."""
    staging = tmp_path / f"staging{n}"
    session_dir = staging / f"session_{n:03d}"
    session_dir.mkdir(parents=True)
    entries = []
    for path, content in full.items():
        rel = f"session_{n:03d}/full/{hashlib.sha256(path.encode()).hexdigest()}"
        blob = staging / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(crypto.encrypt(content))
        entries.append(ManifestEntry(
            path=path, type="full", file=rel,
            result_checksum=hashlib.sha256(content).hexdigest(),
            full_size_bytes=len(content),
        ))
    for path, (base, content) in (delta or {}).items():
        new_file = tmp_path / f"delta-source-{n}"
        new_file.write_bytes(content)
        difference = compute_delta(base, new_file)
        rel = f"session_{n:03d}/deltas/{hashlib.sha256(path.encode()).hexdigest()}"
        blob = staging / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(crypto.encrypt(difference))
        entries.append(ManifestEntry(
            path=path, type="delta", delta_file=rel,
            delta_size_bytes=len(difference),
            result_checksum=hashlib.sha256(content).hexdigest(),
            full_size_bytes=len(content),
        ))
    manifest = Manifest(
        version=1, session=n, timestamp=f"2026-10-15T00:00:{n:02d}Z",
        source="/src", label=f"VOL{n}", based_on_session=n - 1 if n else None,
        encryption={}, entries=entries, deleted=list(deleted), manifest_checksum="",
    )
    write_manifest(session_dir, manifest, crypto if not isinstance(crypto, NullCrypto) else None)
    marker = VolumeMarker(_ID, n, n, manifest.timestamp, f"VOL{n - 1}" if n else "")
    if legacy_baseline is not None:
        write_volume_baseline(staging, legacy_baseline, None)
    elif prior is not None:
        write_volume_baseline(staging, prior, crypto)
    write_volume_marker(staging, marker)
    iso = tmp_path / f"vol{n}.iso"
    ISOBackend(iso, disc_size=_DISC).init(staging, label=f"VOL{n}", expected_session_count=0)
    return iso, build_volume_baseline(prior, [manifest], marker, f"VOL{n}")


def _three_discs(tmp_path):
    iso0, prior = _stage(tmp_path, 0, {
        "a.txt": b"A-old", "old-only.txt": b"old-only",
    }, _CRYPTO)
    iso1, prior = _stage(tmp_path, 1, {"a.txt": b"A-new"}, _CRYPTO, prior)
    iso2, _prior = _stage(tmp_path, 2, {"latest.txt": b"latest"}, _CRYPTO, prior)
    return [iso0, iso1, iso2]


def _source(iso, crypto=_CRYPTO):
    backend = ISOBackend(iso, disc_size=_DISC)
    marker = read_volume_marker(backend)
    manifests = load_session_manifests(backend, crypto, start_session=marker.first_session)
    return backend, build_archive_catalog(backend, crypto, manifests, marker), manifests


class Acquisition:
    image = None

    def __init__(self, discs, order):
        self.discs = discs
        self.order = iter(order)
        self.calls = []

    def acquire(self, needed, catalog, archive_id):
        self.calls.append(dict(needed))
        action = next(self.order)
        if action == "q":
            return None, None, True
        if isinstance(action, tuple):
            index = action[1]
            assert index in needed
            return None, index, False
        assert action in needed
        return ISOBackend(self.discs[action], disc_size=_DISC), None, False


def test_newest_version_requests_only_its_disc(tmp_path):
    """Newest a.txt is on disc 1. Restore still checks disc 0 manifests."""
    discs = _three_discs(tmp_path)
    backend, catalog, manifests = _source(discs[2])
    acquisition = Acquisition(discs, [1, 0])
    dest = tmp_path / "selected"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, paths=["a.txt"],
        manifests=manifests, acquisition=acquisition,
    )
    assert result.failures == 0
    assert result.restored == 1
    assert (dest / "a.txt").read_bytes() == b"A-new"
    assert not (dest / "old-only.txt").exists()
    assert acquisition.calls == [{1: 1}, {0: 0}]
    assert set(load_restore_state(dest, _ID).read_discs) == {0, 1}


def test_mixed_order_acquisition_reads_each_disc_once(tmp_path, monkeypatch):
    discs = _three_discs(tmp_path)
    blob_reads = _track_iso_blob_reads(monkeypatch, discs)
    backend, catalog, manifests = _source(discs[2])
    acquisition = Acquisition(discs, [1, 0])
    dest = tmp_path / "all"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    assert result.failures == 0
    assert {p.name: p.read_bytes() for p in dest.iterdir()} == {
        "a.txt": b"A-new", "old-only.txt": b"old-only", "latest.txt": b"latest",
    }
    assert set(load_restore_state(dest, _ID).read_discs) == {0, 1, 2}
    disc_indexes = [index for index, _blobs in blob_reads]
    assert disc_indexes == [2, 1, 0]
    assert disc_indexes.count(0) == 1
    assert disc_indexes.count(1) == 1
    assert disc_indexes.count(2) == 1


def test_missing_disc_after_partial_read_falls_back_to_old_version(tmp_path):
    discs = _three_discs(tmp_path)
    backend, catalog, manifests = _source(discs[2])
    acquisition = Acquisition(discs, [("missing", 1), 0])
    dest = tmp_path / "fallback"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    assert result.failures == 0
    assert (dest / "a.txt").read_bytes() == b"A-old"
    assert (dest / "latest.txt").read_bytes() == b"latest"
    assert result.stale == [("a.txt", 0)]
    state = load_restore_state(dest, _ID)
    assert state.missing_discs == [1]
    assert set(state.read_discs) == {0, 2}


def test_unskip_volume_replaces_fallback_with_newest_version(tmp_path):
    discs = _three_discs(tmp_path)
    backend, catalog, manifests = _source(discs[2])
    dest = tmp_path / "unskip"
    fallback = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        skip_volumes=[1], acquisition=Acquisition(discs, [0]),
    )
    assert fallback.failures == 0
    assert (dest / "a.txt").read_bytes() == b"A-old"
    backend, catalog, manifests = _source(discs[2])
    newest = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        unskip_volumes=[1], acquisition=Acquisition(discs, [1]),
    )
    assert newest.failures == 0
    assert newest.stale == []
    assert (dest / "a.txt").read_bytes() == b"A-new"
    assert load_restore_state(dest, _ID).missing_discs == []


def _assert_paused_disc_progress(paused, capsys, dest: Path, blob_reads) -> None:
    """Input: paused result, capture, destination, reads. Output: None."""
    assert paused.paused
    assert paused.failures == 0
    assert "Restore paused" in capsys.readouterr().err
    assert (dest / "latest.txt").read_bytes() == b"latest"
    assert load_restore_state(dest, _ID).read_discs == [2]
    first = [index for index, _blobs in blob_reads]
    assert first == [2]


def _assert_resumed_disc_progress(result, dest: Path, blob_reads) -> None:
    """Input: resumed result, destination, reads. Output: None."""
    assert result.failures == 0
    assert (dest / "a.txt").read_bytes() == b"A-new"
    assert (dest / "old-only.txt").read_bytes() == b"old-only"
    assert set(load_restore_state(dest, _ID).read_discs) == {0, 1, 2}
    resumed = [index for index, _blobs in blob_reads]
    assert 2 not in resumed
    assert resumed.count(0) == 1
    assert resumed.count(1) == 1


def test_quit_preserves_progress_and_resume_does_not_reread_disc(tmp_path, capsys, monkeypatch):
    discs = _three_discs(tmp_path)
    blob_reads = _track_iso_blob_reads(monkeypatch, discs)
    backend, catalog, manifests = _source(discs[2])
    dest = tmp_path / "paused"
    paused = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=Acquisition(discs, ["q"]),
    )
    _assert_paused_disc_progress(paused, capsys, dest, blob_reads)
    blob_reads.clear()
    resumed_backend, resumed_catalog, resumed_manifests = _source(discs[2])
    acquisition = Acquisition(discs, [0, 1])
    result = restore_archive(
        dest, resumed_backend, _CRYPTO, resumed_catalog,
        manifests=resumed_manifests, acquisition=acquisition,
    )
    _assert_resumed_disc_progress(result, dest, blob_reads)


def _assert_spool_permissions(spool: Path, dest: Path) -> None:
    """Input: spool and destination paths. Output: None."""
    assert not spool.is_relative_to(dest)
    assert spool.stat().st_mode & 0o777 == 0o700
    state_file = state_path(dest, _ID)
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert state_file.parent.stat().st_mode & 0o777 == 0o700


def _assert_encrypted_spool(paused, dest: Path, crypto: KeyfileCrypto,
                            original: bytes, tmp_path: Path) -> Path:
    """Input: pause result, destination, crypto, original, root. Output: spool."""
    assert paused.paused
    assert paused.failures == 0
    assert not (dest / "changed.txt").exists()
    spool = state_path(dest, _ID).with_suffix(".spool")
    saved = list(spool.rglob("*"))
    blobs = [path for path in saved if path.is_file()]
    assert len(blobs) == 1
    encrypted = blobs[0].read_bytes()
    assert encrypted != original
    assert crypto.decrypt(encrypted) == compute_delta(original, tmp_path / "delta-source-1")
    _assert_spool_permissions(spool, dest)
    return spool


def test_encrypted_cross_disc_spool_contains_only_ciphertext(tmp_path):
    crypto = KeyfileCrypto.from_kek(b"K" * 32)
    original = b"old" * 3000
    updated = original[:-8] + b"new-tail"
    iso0, prior = _stage(tmp_path, 0, {"changed.txt": original}, crypto)
    iso1, _prior = _stage(
        tmp_path, 1, {}, crypto, prior,
        delta={"changed.txt": (original, updated)},
    )
    discs = [iso0, iso1]
    backend, catalog, manifests = _source(iso1, crypto)
    dest = tmp_path / "encrypted"
    paused = restore_archive(
        dest, backend, crypto, catalog, manifests=manifests,
        acquisition=Acquisition(discs, ["q"]),
    )
    spool = _assert_encrypted_spool(paused, dest, crypto, original, tmp_path)
    backend, catalog, manifests = _source(iso1, crypto)
    result = restore_archive(
        dest, backend, crypto, catalog, manifests=manifests,
        acquisition=Acquisition(discs, [0]),
    )
    assert result.failures == 0
    assert (dest / "changed.txt").read_bytes() == updated
    assert not spool.exists()


def test_v1_baseline_cross_disc_delta_remains_restorable(tmp_path):
    original = b"A" * 8000
    updated = original[:-8] + b"new-tail"
    iso0, _prior = _stage(tmp_path, 0, {"changed.txt": original}, _CRYPTO)
    legacy = VolumeBaseline(
        version=1, volumes=[], history={},
        state={"changed.txt": hashlib.sha256(original).hexdigest()},
        origin={"changed.txt": 0},
    )
    iso1, _prior = _stage(
        tmp_path, 1, {}, _CRYPTO,
        delta={"changed.txt": (original, updated)}, legacy_baseline=legacy,
    )
    backend, catalog, manifests = _source(iso1)
    acquisition = Acquisition([iso0, iso1], [0])
    dest = tmp_path / "legacy-v1"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    assert result.failures == 0
    assert (dest / "changed.txt").read_bytes() == updated
    assert len(acquisition.calls) == 1


def test_restore_selection_mutex_and_repeatable_missing_flags():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["restore", "/dest", "--path", "a", "--all-files"])
    assert exc.value.code == 2
    args = parser.parse_args([
        "restore", "/dest", "--all-files", "--all-disks",
        "--skip-volume", "1", "--unskip-volume", "2",
    ])
    assert args.all_files and args.all_disks
    assert args.skip_volumes == [1]
    assert args.unskip_volumes == [2]


def _blob_id(session, path):
    return hashlib.sha256(f"{session}:{path}".encode()).hexdigest()


def _build_initial_five_entries(staging: Path, n: int) -> list[ManifestEntry]:
    """Input: staging directory and session index. Output: initial entries."""
    entries = []
    for path in ("gone.txt", "other.txt"):
        rel = f"session_{n:03d}/full/{_blob_id(n, path)}"
        blob = staging / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(path.encode())
        entries.append(ManifestEntry(
            path=path, type="full", file=rel,
            result_checksum=hashlib.sha256(path.encode()).hexdigest(),
            full_size_bytes=len(path),
        ))
    return entries


def _build_five_version_entry(tmp_path: Path, session: Path, n: int,
                              versions: list[bytes], delta_tail: bool) -> ManifestEntry:
    """Input: session paths, index, versions, mode. Output: version entry."""
    content = versions[n]
    if delta_tail and n >= 3:
        new_file = tmp_path / f"version-{n}.bin"
        new_file.write_bytes(content)
        blob_data = compute_delta(versions[n - 1], new_file)
        kind = "deltas"
        entry = ManifestEntry(
            path="five.txt", type="delta",
            delta_file=f"session_{n:03d}/{kind}/{_blob_id(n, 'five.txt')}",
            delta_size_bytes=len(blob_data),
            result_checksum=hashlib.sha256(content).hexdigest(),
            full_size_bytes=len(content),
        )
    else:
        blob_data = content
        kind = "full"
        entry = ManifestEntry(
            path="five.txt", type="full",
            file=f"session_{n:03d}/{kind}/{_blob_id(n, 'five.txt')}",
            result_checksum=hashlib.sha256(content).hexdigest(),
            full_size_bytes=len(content),
        )
    blob = session / kind / _blob_id(n, "five.txt")
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(blob_data)
    return entry


def _record_five_session(backend: ISOBackend, staging: Path, n: int) -> None:
    """Input: backend, staging directory, index. Output: recorded session."""
    if n == 0:
        backend.init(staging, label="FIVE", expected_session_count=0)
    else:
        backend.append(staging, label="FIVE", expected_session_count=n)


def _five_session_iso(tmp_path, delta_tail=False):
    iso = tmp_path / "five.iso"
    backend = ISOBackend(iso, disc_size=_DISC)
    versions = [f"version-{n}".encode() * 1024 for n in range(5)]
    for n in range(5):
        staging = tmp_path / f"five-stage-{n}"
        session = staging / f"session_{n:03d}"
        session.mkdir(parents=True)
        entries = _build_initial_five_entries(staging, n) if n == 0 else []
        entries.append(_build_five_version_entry(tmp_path, session, n, versions, delta_tail))
        write_manifest(session, Manifest(
            version=1, session=n, timestamp=f"2026-10-15T00:00:{n:02d}Z",
            source="/src", label="FIVE", based_on_session=n - 1 if n else None,
            encryption={}, entries=entries, deleted=["gone.txt"] if n == 4 else [],
            manifest_checksum="",
        ))
        _record_five_session(backend, staging, n)
    return backend, versions


def _blob_reads(backend, monkeypatch):
    seen = []
    original = backend.extract_paths

    def track(paths, dest_root, max_bytes=None):
        seen.extend(path for path in paths if "/full/" in path or "/deltas/" in path)
        return original(paths, dest_root, max_bytes=max_bytes)

    monkeypatch.setattr(backend, "extract_paths", track)
    return seen


def _track_iso_blob_reads(monkeypatch, discs):
    """
    Input:  monkeypatch and ISO paths in volume-index order
    Output: list of (volume_index, blob_paths) appended on each blob read
    Details:
        Patches ISOBackend.extract_paths so every instance is counted.
    """
    index_by_iso = {str(path.resolve()): i for i, path in enumerate(discs)}
    seen = []
    original = ISOBackend.extract_paths

    def track(self, paths, dest_root, max_bytes=None):
        blobs = [path for path in paths if "/full/" in path or "/deltas/" in path]
        if blobs:
            seen.append((index_by_iso[str(Path(self.iso_path).resolve())], blobs))
        return original(self, paths, dest_root, max_bytes=max_bytes)

    monkeypatch.setattr(ISOBackend, "extract_paths", track)
    return seen



@pytest.mark.parametrize("read_mode", ["sequential", "full-copy"])
def test_latest_full_reads_no_superseded_blobs_and_obeys_selection(
    tmp_path, monkeypatch, read_mode,
):
    backend, versions = _five_session_iso(tmp_path)
    seen = _blob_reads(backend, monkeypatch)
    dest = tmp_path / "latest-full"
    result = restore(dest, backend, _CRYPTO, paths=["five.txt"], read_mode=read_mode)
    assert result.failures == 0
    assert (dest / "five.txt").read_bytes() == versions[4]
    assert not (dest / "other.txt").exists()
    assert seen == [f"session_004/full/{_blob_id(4, 'five.txt')}"]


@pytest.mark.parametrize("read_mode", ["sequential", "full-copy"])
def test_latest_delta_reads_nearest_full_and_only_following_deltas(
    tmp_path, monkeypatch, read_mode,
):
    backend, versions = _five_session_iso(tmp_path, delta_tail=True)
    seen = _blob_reads(backend, monkeypatch)
    dest = tmp_path / "latest-delta"
    result = restore(dest, backend, _CRYPTO, read_mode=read_mode)
    assert result.failures == 0
    assert (dest / "five.txt").read_bytes() == versions[4]
    assert (dest / "other.txt").read_bytes() == b"other.txt"
    assert not (dest / "gone.txt").exists()
    assert seen == [
        f"session_000/full/{_blob_id(0, 'other.txt')}",
        f"session_002/full/{_blob_id(2, 'five.txt')}",
        f"session_003/deltas/{_blob_id(3, 'five.txt')}",
        f"session_004/deltas/{_blob_id(4, 'five.txt')}",
    ]


def test_session_cutoff_selects_earlier_latest_full(tmp_path, monkeypatch):
    backend, versions = _five_session_iso(tmp_path)
    seen = _blob_reads(backend, monkeypatch)
    dest = tmp_path / "at-session"
    result = restore(dest, backend, _CRYPTO, session=2, paths=["five.txt"])
    assert result.failures == 0
    assert (dest / "five.txt").read_bytes() == versions[2]
    assert seen == [f"session_002/full/{_blob_id(2, 'five.txt')}"]


def _chunk_version(session_n: int, chunk: int, count: int) -> PathVersion:
    """Input: session, chunk index and chunk count. Output: a verified chunk version."""
    return PathVersion(
        session=session_n, volume_index=0, kind="full",
        result_checksum=f"c{chunk}", full_size_bytes=10,
        timestamp="t", blob_session=session_n,
        volume_chunk=chunk, volume_chunk_count=count,
    )


def test_chain_complete_rejects_a_chunk_prefix():
    """A volume-split chain that stops before the final chunk is not restorable."""
    from oddarchiver.restore_plan import _chain_complete

    assert not _chain_complete([_chunk_version(0, 0, 3)])
    assert not _chain_complete([_chunk_version(0, 0, 3), _chunk_version(1, 1, 3)])
    assert _chain_complete([
        _chunk_version(0, 0, 3), _chunk_version(1, 1, 3), _chunk_version(1, 2, 3),
    ])


def test_entry_key_separates_chunks_in_one_session():
    """Two chunks of one file in one session keep separate state keys."""
    from oddarchiver.restore_plan import _entry_key

    keys = {_entry_key("big.bin", 4, chunk) for chunk in (0, 1, 2)}
    assert len(keys) == 3
    assert _entry_key("big.bin", 4, None) == _entry_key("big.bin", 4, 0)
