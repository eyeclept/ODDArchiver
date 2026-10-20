"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Test recovery of deltas encoded against the previous delta blob.
"""
# Imports
import hashlib
import logging

import pytest

from oddarchiver.catalog import build_archive_catalog, remember_encryption_mode
from oddarchiver.cli import build_parser, dispatch
from oddarchiver.crypto import KeyfileCrypto, NullCrypto
from oddarchiver.delta import compute_delta
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, load_session_manifests, write_manifest
from oddarchiver.restore import restore
from oddarchiver.restore_plan import restore_archive
from oddarchiver.verify import verify
from oddarchiver.volume import build_volume_baseline, read_volume_marker, write_volume_baseline, write_volume_marker, VolumeMarker
from tests.test_restore_plan import Acquisition

# Globals
logger = logging.getLogger(__name__)
_DISC = 20 * 2**20
_PATH = "history.bin"
_ARCHIVE_ID = "delta-recovery-test"
_WARNING = "recovered a delta written against the previous delta blob"

# Functions

@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Input: test root. Output: None. Details: Keep user files in the test root."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _hash(data: bytes) -> str:
    """Input: bytes. Output: SHA-256. Details: Hash the file content."""
    return hashlib.sha256(data).hexdigest()


def _next_version(n, versions, previous_delta, corrupt, modern):
    """
    Input: session index, prior versions, prior delta, and chain options.
    Output: content for the indexed session.
    """
    if n == 0:
        return versions[0]
    if n == 1:
        return versions[0][:16000] + b"new-byte" + versions[0][16008:]
    source_content = versions[n - 1] if modern else previous_delta
    content = (source_content * (32000 // len(source_content) + 1))[:32000]
    if corrupt and n == 3:
        return b"Q" * 15999 + b"R" + b"Q" * 16000
    return content[:15999] + bytes([65 + n]) + content[16000:]


def _build_delta_entry(tmp_path, n, content, versions, previous_delta, corrupt, modern):
    """
    Input: test root, session index, content history, and chain options.
    Output: encoded delta and manifest entry.
    """
    new_path = tmp_path / f"version-{n}.bin"
    new_path.write_bytes(content)
    source = versions[n - 1] if n == 1 or modern else previous_delta
    if corrupt and n == 3:
        source = b"Q" * 32000
    raw = compute_delta(source, new_path)
    entry = ManifestEntry(
        path=_PATH, type="delta", result_checksum=_hash(content),
        source_checksum=_hash(versions[n - 1]),
        full_size_bytes=len(content), delta_size_bytes=len(raw),
    )
    return raw, entry


def _write_session(tmp_path, n, content, versions, previous_delta, crypto, corrupt, modern):
    """
    Input: test root, session index, content history, crypto, and chain options.
    Output: staging path, manifest, and prior delta blob.
    """
    staging = tmp_path / f"stage-{n}"
    session_dir = staging / f"session_{n:03d}"
    session_dir.mkdir(parents=True)
    if n == 0:
        raw = content
        kind = "full"
        entry = ManifestEntry(
            path=_PATH, type=kind, result_checksum=_hash(content),
            full_size_bytes=len(content),
        )
    else:
        raw, entry = _build_delta_entry(
            tmp_path, n, content, versions, previous_delta, corrupt, modern,
        )
        kind = "deltas"
        previous_delta = raw
    blob_path = f"session_{n:03d}/{kind}/{_hash(_PATH.encode())}"
    blob = staging / blob_path
    blob.parent.mkdir(parents=True)
    blob.write_bytes(crypto.encrypt(raw))
    if n == 0:
        entry.file = blob_path
    else:
        entry.delta_file = blob_path
    manifest = Manifest(
        version=1, session=n, timestamp=f"2026-10-15T00:00:{n:02d}Z",
        source="/src", label="TEST", based_on_session=n - 1 if n else None,
        encryption={}, entries=[entry], deleted=[], manifest_checksum="",
    )
    write_manifest(session_dir, manifest, crypto)
    return staging, manifest, previous_delta


def _write_separate_disc(tmp_path, n, staging, manifest, crypto, prior_baseline):
    """
    Input: test root, session index, staging, manifest, crypto, prior baseline.
    Output: ISO path and updated baseline.
    """
    marker = VolumeMarker(_ARCHIVE_ID, n, n, manifest.timestamp, "")
    if prior_baseline is not None:
        write_volume_baseline(staging, prior_baseline, crypto)
    write_volume_marker(staging, marker)
    iso_path = tmp_path / (f"archive.vol{n}.iso" if n else "archive.iso")
    backend = ISOBackend(iso_path, disc_size=_DISC)
    backend.init(staging, label="TEST", expected_session_count=0)
    prior_baseline = build_volume_baseline(prior_baseline, [manifest], marker, "TEST")
    return iso_path, prior_baseline


def _append_session(tmp_path, n, staging, backend):
    """
    Input: test root, session index, staging path, and existing backend.
    Output: backend containing the appended session.
    """
    if backend is None:
        backend = ISOBackend(tmp_path / "sessions.iso", disc_size=_DISC)
        backend.init(staging, label="TEST", expected_session_count=0)
    else:
        backend.append(staging, label="TEST", expected_session_count=n)
    return backend


def _archive(tmp_path, crypto, *, separate=False, corrupt=False, modern=False):
    """
    Input: test root, encryption mode, and chain options.
    Output: ISO paths and expected content.
    Details: Encode later deltas against the prior delta blob unless modern is set.
    """
    versions = [bytes(range(256)) * 125]
    discs = []
    prior_baseline = None
    previous_delta = None
    backend = None
    for n in range(4):
        content = _next_version(n, versions, previous_delta, corrupt, modern)
        if n:
            versions.append(content)
        staging, manifest, previous_delta = _write_session(
            tmp_path, n, content, versions, previous_delta, crypto, corrupt, modern,
        )
        if separate:
            iso_path, prior_baseline = _write_separate_disc(
                tmp_path, n, staging, manifest, crypto, prior_baseline,
            )
            discs.append(iso_path)
        else:
            backend = _append_session(tmp_path, n, staging, backend)
    return discs if separate else [tmp_path / "sessions.iso"], versions[-1]


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "keyfile"])
def test_single_disc_broken_chain(tmp_path, caplog, encrypted):
    """Restore three deltas, two encoded against the prior delta blob."""
    crypto = KeyfileCrypto.from_kek(b"K" * 32) if encrypted else NullCrypto()
    discs, expected = _archive(tmp_path, crypto)
    dest = tmp_path / "dest"
    with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
        result = restore(dest, ISOBackend(discs[0], disc_size=_DISC), crypto)
    assert (result.restored, result.failures) == (1, 0)
    assert (dest / _PATH).read_bytes() == expected
    assert _hash((dest / _PATH).read_bytes()) == _hash(expected)
    assert sum(_WARNING in rec.message for rec in caplog.records) == 2


def test_recovery_mode_accepts_broken_chain(tmp_path, caplog):
    """Recovery mode checks each result and keeps the verified target."""
    discs, expected = _archive(tmp_path, NullCrypto())
    dest = tmp_path / "recover-dest"
    with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
        result = restore(
            dest, ISOBackend(discs[0], disc_size=_DISC), NullCrypto(),
            skip_volumes=[99],
        )
    assert (result.restored, result.failures) == (1, 0)
    assert (dest / _PATH).read_bytes() == expected
    assert _hash((dest / _PATH).read_bytes()) == _hash(expected)
    assert sum(_WARNING in rec.message for rec in caplog.records) == 2


def _latest_catalog(disc, crypto):
    """
    Input: newest ISO path and crypto
    Output: backend, manifests, and catalog read from that disc only
    Details: A resumed restore starts from a new CLI run. It has no
        catalog or manifests from the paused run.
    """
    latest = ISOBackend(disc, disc_size=_DISC)
    marker = read_volume_marker(latest)
    manifests = load_session_manifests(latest, crypto, start_session=marker.first_session)
    return latest, manifests, build_archive_catalog(latest, crypto, manifests, marker)


def _assert_cross_disc_resume(resumed, acquisition, dest, expected, caplog):
    """
    Input: resumed result, acquisition history, destination, expected bytes, log.
    Output: assertions about restored content and recovered delta warnings.
    """
    assert (resumed.restored, resumed.failures) == (1, 0)
    assert all(2 not in needed for needed in acquisition.calls)
    assert len(acquisition.calls) == 2
    assert (dest / _PATH).read_bytes() == expected
    assert _hash((dest / _PATH).read_bytes()) == _hash(expected)
    assert sum(_WARNING in rec.message for rec in caplog.records) == 2


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "keyfile"])
def test_cross_disc_broken_chain_resume(tmp_path, caplog, encrypted):
    """Resume after spooling later deltas and restore the earlier discs."""
    crypto = KeyfileCrypto.from_kek(b"K" * 32) if encrypted else NullCrypto()
    discs, expected = _archive(tmp_path, crypto, separate=True)
    latest, manifests, catalog = _latest_catalog(discs[-1], crypto)
    dest = tmp_path / "dest"
    with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
        paused = restore_archive(
            dest, latest, crypto, catalog, manifests=manifests,
            acquisition=Acquisition(discs, [2, "q"]),
        )
        assert paused.paused
        assert not (dest / _PATH).exists()
        latest, manifests, catalog = _latest_catalog(discs[-1], crypto)
        acquisition = Acquisition(discs, [1, 0])
        resumed = restore_archive(
            dest, latest, crypto, catalog, manifests=manifests,
            acquisition=acquisition,
        )
    _assert_cross_disc_resume(resumed, acquisition, dest, expected, caplog)


def test_cli_all_disks_restores_broken_chain(tmp_path, caplog):
    """The CLI finds sibling discs and restores the verified file content."""
    discs, expected = _archive(tmp_path, NullCrypto(), separate=True)
    remember_encryption_mode(_ARCHIVE_ID, "none")
    dest = tmp_path / "cli-dest"
    args = build_parser().parse_args([
        "restore", str(dest), "--test-iso", str(discs[-1]),
        "--all-files", "--all-disks",
    ])
    with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
        assert dispatch(args) == 0
    assert (dest / _PATH).read_bytes() == expected
    assert _hash((dest / _PATH).read_bytes()) == _hash(expected)
    assert _WARNING in caplog.text


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "keyfile"])
def test_corrupt_delta_cannot_restore(tmp_path, caplog, encrypted):
    """Reject bytes that match neither the content nor the delta blob."""
    crypto = KeyfileCrypto.from_kek(b"K" * 32) if encrypted else NullCrypto()
    discs, _expected = _archive(tmp_path, crypto, corrupt=True)
    dest = tmp_path / "dest"
    with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
        result = restore(dest, ISOBackend(discs[0], disc_size=_DISC), crypto)
    assert result.failures == 1
    assert result.restored == 0
    assert not (dest / _PATH).exists()
    assert not list(dest.glob(".oddarchiver_restore_*"))


def test_cli_corrupt_delta_exits_one(tmp_path):
    """The CLI returns failure and never writes the corrupt target file."""
    discs, _expected = _archive(tmp_path, NullCrypto(), corrupt=True)
    dest = tmp_path / "cli-corrupt"
    args = build_parser().parse_args([
        "restore", str(dest), "--test-iso", str(discs[0]),
        "--all-files",
    ])
    assert dispatch(args) == 1
    assert not (dest / _PATH).exists()


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "keyfile"])
def test_verify_full_recovers_broken_and_rejects_corrupt(tmp_path, caplog, encrypted):
    """Full verify replays the same chain and refuses the corrupt result."""
    crypto = KeyfileCrypto.from_kek(b"K" * 32) if encrypted else NullCrypto()
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    good_discs, _ = _archive(good_dir, crypto)
    with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
        assert verify(ISOBackend(good_discs[0], disc_size=_DISC), crypto, level="full")
    assert _WARNING in caplog.text
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_discs, _ = _archive(bad_dir, crypto, corrupt=True)
    with pytest.raises(SystemExit) as exc:
        verify(ISOBackend(bad_discs[0], disc_size=_DISC), crypto, level="full")
    assert exc.value.code == 1


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "keyfile"])
def test_modern_chain_needs_no_fallback(tmp_path, caplog, encrypted):
    """Use the rebuilt content for all modern delta sources."""
    crypto = KeyfileCrypto.from_kek(b"K" * 32) if encrypted else NullCrypto()
    discs, expected = _archive(tmp_path, crypto, modern=True)
    dest = tmp_path / "dest"
    with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
        result = restore(dest, ISOBackend(discs[0], disc_size=_DISC), crypto)
    assert (result.restored, result.failures) == (1, 0)
    assert (dest / _PATH).read_bytes() == expected
    assert _hash((dest / _PATH).read_bytes()) == _hash(expected)
    assert _WARNING not in caplog.text
