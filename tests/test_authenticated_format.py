"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Regression tests for authenticated session format 3 and legacy media.
"""
# Imports
import hashlib
import json
import logging
from pathlib import Path

import pytest

import oddarchiver.crypto as crypto_module
from oddarchiver.cache import CacheManager
from oddarchiver.crypto import (
    KeyfileCrypto, PassphraseCrypto, baseline_aad, blob_aad, new_blob_aad,
    manifest_aad,
)
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import (
    MANIFEST_VERSION, Manifest, ManifestEntry, load_session_manifests,
    read_manifest, write_manifest,
)
from oddarchiver.restore import restore
from oddarchiver.session import _blob_id, _build_checksum_index, build_staging
from oddarchiver.verify import _check_blobs, verify
from oddarchiver.volume import (
    BaselineReadError, VolumeBaseline, VolumeMarker, read_volume_baseline,
    write_volume_baseline,
)

# Globals
logger = logging.getLogger(__name__)


# Functions


def _manifest(session: int, entries: list[ManifestEntry], version: int = MANIFEST_VERSION) -> Manifest:
    """Input: session, entries, and version. Output: a complete manifest."""
    return Manifest(
        version=version, session=session, timestamp="2026-10-16T00:00:00Z",
        source="/source", label="TEST", based_on_session=session - 1 if session else None,
        encryption={"mode": "keyfile"}, entries=entries, deleted=[], manifest_checksum="",
    )


def _crypto(tmp_path: Path) -> KeyfileCrypto:
    """Input: test directory. Output: one private keyfile backend."""
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    return KeyfileCrypto(str(key))


def test_blob_ids_are_independent_of_paths_and_each_other():
    """Input: the same session and path twice. Output: distinct random IDs."""
    first = _blob_id(4, "private/name.txt")
    second = _blob_id(4, "private/name.txt")
    assert len(first) == len(second) == 64
    assert first != second
    assert first != hashlib.sha256(b"4:private/name.txt").hexdigest()


class _Files:
    """Input: path map. Output: a small read-only backend for blob checks."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def read_path(self, path: str) -> bytes:
        return self.files[path]

    def read_paths(self, paths: list[str]) -> dict[str, bytes]:
        return {path: self.files[path] for path in paths if path in self.files}

    def extract_path_to_file(self, path: str, dest: Path) -> None:
        dest.write_bytes(self.files[path])


@pytest.mark.parametrize("kind", ["full", "delta"])
def test_swapped_blob_fails_checksum_verification(tmp_path, kind):
    """Input: swapped full or delta ciphertext. Output: authentication error."""
    crypto = _crypto(tmp_path)
    entries = []
    files = {}
    for idx in range(2):
        blob_id = _blob_id(0, f"file-{idx}")
        path = f"session_000/{'full' if kind == 'full' else 'deltas'}/{blob_id}"
        payload = f"payload-{idx}".encode()
        files[path] = crypto.encrypt(payload, aad=new_blob_aad(0, kind, blob_id))
        entries.append(ManifestEntry(
            path=f"file-{idx}", type=kind,
            file=path if kind == "full" else "",
            delta_file=path if kind == "delta" else "",
            full_size_bytes=len(payload), result_checksum=hashlib.sha256(payload).hexdigest(),
            blob_origin_session=0,
        ))
    first_path = entries[0].file if kind == "full" else entries[0].delta_file
    second_path = entries[1].file if kind == "full" else entries[1].delta_file
    files[first_path] = files[second_path]
    errors = []
    _check_blobs(_manifest(0, entries), _Files(files), crypto, errors)
    assert any("authentication" in error for error in errors)


@pytest.mark.parametrize("backend", ["passphrase", "keyfile"])
@pytest.mark.parametrize("change", ["reorder", "drop", "duplicate", "after_final"])
def test_stream_frames_reject_sequence_changes(tmp_path, monkeypatch, backend, change):
    """Input: a changed frame sequence. Output: decrypt_file refuses it."""
    monkeypatch.setattr(crypto_module, "STREAM_CHUNK_SIZE", 16)
    crypto = PassphraseCrypto("password") if backend == "passphrase" else _crypto(tmp_path)
    source = tmp_path / "source"
    encrypted = tmp_path / "encrypted"
    source.write_bytes(b"A" * 16 + b"B" * 16)
    aad = new_blob_aad(0, "full", "a" * 64)
    crypto.encrypt_file(source, encrypted, aad=aad)
    data = encrypted.read_bytes()
    header = len(crypto_module._BOUND_MAGIC) + (16 if backend == "passphrase" else 60)
    frames = []
    offset = header
    while offset < len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 5 + 12 + size
        frames.append(data[offset:end])
        offset = end
    assert len(frames) == 3
    if change == "reorder":
        frames[0], frames[1] = frames[1], frames[0]
    elif change == "drop":
        del frames[1]
    elif change == "duplicate":
        frames.insert(1, frames[0])
    else:
        frames.append(frames[0])
    encrypted.write_bytes(data[:header] + b"".join(frames))
    with pytest.raises(ValueError):
        crypto.decrypt_file(encrypted, tmp_path / "out", aad=aad)


def test_manifest_slot_and_downgrade_are_rejected(tmp_path):
    """Input: moved or downgraded manifest. Output: suspect read."""
    crypto = _crypto(tmp_path)
    first = tmp_path / "session_000"
    second = tmp_path / "session_001"
    first.mkdir()
    second.mkdir()
    manifest = _manifest(0, [])
    write_manifest(first, manifest, crypto)
    bytes_ = (first / "manifest.enc").read_bytes()
    assert read_manifest(first / "manifest.enc", crypto, slot=0).version == 3
    (second / "manifest.enc").write_bytes(bytes_)
    assert read_manifest(second / "manifest.enc", crypto, slot=1).suspect
    raw = json.loads(crypto.decrypt(bytes_, aad=manifest_aad(0)))
    raw["version"] = 2
    raw["manifest_checksum"] = ""
    # Even a valid version field inside a bound envelope cannot select legacy mode.
    from oddarchiver.manifest import _compute_checksum
    raw["manifest_checksum"] = _compute_checksum(raw)
    (first / "manifest.enc").write_bytes(
        crypto.encrypt(json.dumps(raw).encode(), aad=manifest_aad(0))
    )
    assert read_manifest(first / "manifest.enc", crypto, slot=0).suspect
    (first / "manifest.enc").write_bytes(crypto.encrypt(json.dumps(raw).encode()))
    assert read_manifest(first / "manifest.enc", crypto, slot=0).version == 2


def test_baseline_swap_and_legacy_downgrade_fail(tmp_path):
    """Input: another volume's baseline or an old baseline. Output: refusal."""
    crypto = _crypto(tmp_path)
    marker_a = VolumeMarker("archive", 1, 1, "now", "old")
    marker_b = VolumeMarker("archive", 2, 2, "now", "old")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    baseline = VolumeBaseline(2, [], {}, {}, {})
    write_volume_baseline(first, baseline, crypto, marker_a)
    write_volume_baseline(second, baseline, crypto, marker_b)
    good = (first / "volume_baseline.enc").read_bytes()
    other = (second / "volume_baseline.enc").read_bytes()
    manifest = _manifest(1, [])
    manifest.baseline_checksum = hashlib.sha256(good).hexdigest()
    backend = _Files({
        "volume_baseline.enc": other,
        "volume.json": json.dumps(marker_a.__dict__).encode(),
    })
    with pytest.raises(BaselineReadError, match="checksum"):
        read_volume_baseline(backend, crypto, marker=marker_a, manifests=[manifest])
    backend.files["volume_baseline.enc"] = crypto.encrypt(
        json.dumps({"version": 2, "volumes": [], "history": {}, "state": {}, "origin": {}}).encode()
    )
    with pytest.raises(BaselineReadError, match="checksum"):
        read_volume_baseline(backend, crypto, marker=marker_a, manifests=[manifest])
    backend.files["volume_baseline.enc"] = good
    assert read_volume_baseline(backend, crypto, marker=marker_a, manifests=[manifest]).version == 2
    assert baseline_aad(marker_a.archive_id, marker_a.volume_index) != baseline_aad(
        marker_b.archive_id, marker_b.volume_index,
    )


def _assert_legacy_staging_entries(new_manifest: Manifest, legacy_path: str) -> None:
    """Input: appended manifest and legacy path. Output: dedup assertions."""
    assert len(new_manifest.entries) == 2
    entries = {entry.path: entry for entry in new_manifest.entries}
    assert entries["alias.txt"].file == legacy_path
    assert entries["alias.txt"].blob_origin_session is None
    assert entries["new.txt"].file.rsplit("/", 1)[-1] != hashlib.sha256(b"1:new.txt").hexdigest()


def _assert_legacy_restore(dest: Path, result) -> None:
    """Input: restore destination and result. Output: restored-byte assertions."""
    assert result.failures == 0
    assert (dest / "legacy.txt").read_bytes() == b"legacy payload"
    assert (dest / "new.txt").read_bytes() == b"new payload"
    assert (dest / "alias.txt").read_bytes() == b"legacy payload"


def _assert_legacy_full_copy(copied: Path, copied_result) -> None:
    """Input: full-copy destination and result. Output: restored-byte assertions."""
    assert copied_result.failures == 0
    assert (copied / "legacy.txt").read_bytes() == b"legacy payload"
    assert (copied / "new.txt").read_bytes() == b"new payload"


def test_legacy_writer_append_restore_verify_and_dedup(tmp_path):
    """Input: legacy session then a new session. Output: both remain readable."""
    crypto = _crypto(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "legacy.txt").write_bytes(b"legacy payload")
    iso = tmp_path / "archive.iso"
    backend = ISOBackend(iso, disc_size=50 * 2**20)
    legacy_staging = tmp_path / "legacy_staging"
    legacy_dir = legacy_staging / "session_000"
    full_dir = legacy_dir / "full"
    full_dir.mkdir(parents=True)
    legacy_name = hashlib.sha256(b"0:legacy.txt").hexdigest()
    legacy_path = f"session_000/full/{legacy_name}"
    (full_dir / legacy_name).write_bytes(crypto.encrypt(b"legacy payload"))
    old_entry = ManifestEntry(
        path="legacy.txt", type="full", file=legacy_path,
        full_size_bytes=14, result_checksum=hashlib.sha256(b"legacy payload").hexdigest(),
    )
    write_manifest(legacy_dir, _manifest(0, [old_entry], version=2), crypto)
    backend.init(legacy_staging, label="TEST", expected_session_count=0)
    old = load_session_manifests(backend, crypto)
    assert old[0].version == 2
    (source / "new.txt").write_bytes(b"new payload")
    (source / "alias.txt").write_bytes(b"legacy payload")
    chunks = build_staging(
        1, source, {"legacy.txt": old_entry.result_checksum}, {"legacy.txt": 0},
        backend, CacheManager(tmp_path / "cache"), crypto,
        _staging_root=tmp_path / "staging", manifests=old,
    )
    assert len(chunks) == 1
    staging = chunks[0][0]
    staged_dir = staging / "session_001"
    provisional = staged_dir / "manifest.json"
    new_manifest = read_manifest(provisional, crypto=None) if provisional.exists() else read_manifest(
        staged_dir / "manifest.enc", crypto, slot=1,
    )
    _assert_legacy_staging_entries(new_manifest, legacy_path)
    write_manifest(staged_dir, new_manifest, crypto)
    backend.append(staging, label="TEST", expected_session_count=1)
    manifests = load_session_manifests(backend, crypto)
    assert [m.version for m in manifests] == [2, 3]
    assert verify(backend, crypto, level="checksum")
    dest = tmp_path / "restored"
    result = restore(dest, backend, crypto)
    _assert_legacy_restore(dest, result)
    copied = tmp_path / "full_copy"
    copied_result = restore(copied, backend, crypto, read_mode="full-copy")
    _assert_legacy_full_copy(copied, copied_result)
    index = _build_checksum_index({}, {}, manifests)
    assert index[old_entry.result_checksum].blob_path == legacy_path
