"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Regression tests for delta sources and post-burn cache content.
"""
# Imports
import hashlib
import logging
from pathlib import Path

import pytest

from oddarchiver.cache import CacheManager, _blob_path
from oddarchiver.cli import dispatch
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import read_manifest
from oddarchiver.restore import restore
from tests.helpers import _args

# Globals
logger = logging.getLogger(__name__)
DISC_BYTES = 64 * 2**20

# Functions


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """
    Input: isolated test directory and monkeypatch
    Output: source, ISO, cache, and source file
    Details: Keep the ledger, cache, and staging out of the user HOME.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg_runtime"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cache = CacheManager()
    assert cache.cache_dir == tmp_path / ".cache" / "oddarchiver"
    source = tmp_path / "source"
    source.mkdir()
    path = source / "data.bin"
    path.write_bytes(b"A" * 34000)
    iso = tmp_path / "archive.iso"
    assert dispatch(_args("init", source, iso)) == 0
    return source, iso, cache, path


def _entry(iso: Path, session: int):
    """
    Input: ISO path and session number
    Output: the file entry in the selected session
    Details: Read the manifest through the ISO backend.
    """
    backend = ISOBackend(iso, disc_size=DISC_BYTES)
    return next(entry for entry in read_manifest(
        backend._sessions_root / f"session_{session:03d}" / "manifest.json"
    ).entries if entry.path == "data.bin")


def _restore_matches(iso: Path, dest: Path, expected: bytes) -> None:
    """
    Input: ISO, destination, and expected file bytes
    Output: None
    Details: Compare the restored bytes and the manifest result checksum.
    """
    restore(dest, ISOBackend(iso, disc_size=DISC_BYTES), NullCrypto())
    data = (dest / "data.bin").read_bytes()
    assert data == expected
    assert hashlib.sha256(data).hexdigest() == _entry(
        iso, ISOBackend(iso, disc_size=DISC_BYTES).mediainfo().session_count - 1,
    ).result_checksum


def test_three_consecutive_deltas_restore_and_cache_full_contents(archive, tmp_path):
    """
    Input: a one-disc archive and three small edits
    Output: three delta entries and an exact restored file
    Details: Cache each delta session as the new file content.
    """
    source, iso, cache, path = archive
    for session in range(1, 4):
        data = bytearray(b"A" * 34000)
        data[session * 17] = ord("B") + session
        path.write_bytes(data)
        assert dispatch(_args("sync", source, iso)) == 0
        entry = _entry(iso, session)
        assert entry.type == "delta"
        assert 0 < entry.delta_size_bytes < 1000
        assert cache.get("data.bin", session) == bytes(data)
        assert hashlib.sha256(cache.get("data.bin", session)).hexdigest() == entry.result_checksum
    _restore_matches(iso, tmp_path / "restored", path.read_bytes())


def _assert_prior_mismatch_warnings(records) -> None:
    """
    Input: captured log records
    Output: None
    Details: Both dry-run and sync must reject the poisoned prior content.
    """
    matches = sum(
        "data.bin" in record.message
        and "cached prior version did not match" in record.message
        for record in records
    )
    assert matches >= 2


def test_poisoned_cache_forces_full_and_dry_run_agrees(archive, tmp_path, caplog, capsys):
    """
    Input: a cache that contains the old delta blob
    Output: a full entry and the same dry-run choice
    Details: Log a prior-content mismatch and restore exact bytes.
    """
    source, iso, cache, path = archive
    path.write_bytes(b"B" + b"A" * 33999)
    assert dispatch(_args("sync", source, iso)) == 0
    prior = _entry(iso, 1)
    assert prior.type == "delta"
    cache.put("data.bin", 1, ISOBackend(iso, disc_size=DISC_BYTES).read_path(prior.delta_file))
    expected = b"C" + b"A" * 33999
    path.write_bytes(expected)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso, dry_run=True)) == 0
        dry_output = capsys.readouterr().out
        assert "[full]" in dry_output and "data.bin" in dry_output
        assert dispatch(_args("sync", source, iso)) == 0
    assert _entry(iso, 2).type == "full"
    _assert_prior_mismatch_warnings(caplog.records)
    _restore_matches(iso, tmp_path / "restored", expected)


def test_delta_disc_fallback_forces_full(archive, tmp_path, caplog):
    """
    Input: a prior delta with no local cached copy
    Output: a full entry and an exact restore
    Details: The disc fallback returns delta bytes and fails the source guard.
    """
    source, iso, cache, path = archive
    path.write_bytes(b"B" + b"A" * 33999)
    assert dispatch(_args("sync", source, iso)) == 0
    assert _entry(iso, 1).type == "delta"
    cache_dir = cache.cache_dir
    _blob_path(cache_dir, "data.bin", 1).unlink()
    expected = b"C" + b"A" * 33999
    path.write_bytes(expected)
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso, no_cache=True)) == 0
    assert _entry(iso, 2).type == "full"
    assert any("cached prior version did not match" in r.message for r in caplog.records)
    _restore_matches(iso, tmp_path / "restored", expected)


def test_no_cache_reads_prior_from_disc_and_keeps_local_cache(archive, tmp_path, caplog, capsys):
    """
    Input: a local cache entry with wrong bytes for the prior full version
    Output: --no-cache stages a delta from the disc copy and leaves that entry alone
    Details:
        Without --no-cache the guard rejects the wrong bytes and stages a
        full blob. With --no-cache the prior blob comes from the disc, so
        the delta is valid. The disc read does not enter the local cache,
        the new session is cached, and no private read directory remains.
    """
    source, iso, cache, path = archive
    poison = b"Z" * 34000
    cache.put("data.bin", 0, poison)
    expected = b"B" + b"A" * 33999
    path.write_bytes(expected)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso, dry_run=True, no_cache=True)) == 0
        assert "[delta]" in capsys.readouterr().out
        assert dispatch(_args("sync", source, iso, no_cache=True)) == 0
    assert not any("cached prior version did not match" in r.message for r in caplog.records)
    assert _entry(iso, 1).type == "delta"
    assert cache.get("data.bin", 0) == poison
    assert cache.get("data.bin", 1) == expected
    assert not list(cache.cache_dir.parent.glob("oddarchiver_nocache_*"))
    _restore_matches(iso, tmp_path / "restored", expected)


def test_failed_burn_removes_delta_sidecar_without_cache_update(archive, tmp_path, monkeypatch):
    """
    Input: a simulated append failure after delta staging
    Output: no new cache entry or delta sidecar
    Details: Preserve the prior cache entry when the burn fails.
    """
    source, iso, cache, path = archive
    path.write_bytes(b"B" + b"A" * 33999)
    old = cache.get("data.bin", 0)

    def fail_append(self, staging, label, expected_session_count):
        raise RuntimeError("simulated burn failure")

    monkeypatch.setattr(ISOBackend, "append", fail_append)
    assert dispatch(_args("sync", source, iso)) == 1
    assert cache.get("data.bin", 0) == old
    assert cache.get("data.bin", 1) is None
    assert not (tmp_path / "xdg_runtime" / "oddarchiver" / "oddarchiver_staging_001.cache_full").exists()


def test_source_change_during_burn_caches_staged_content(archive, tmp_path, monkeypatch):
    """
    Input: a source file that changes during the burn
    Output: the staged version in the cache and restored file
    Details: Do not read the live source after staging.
    """
    source, iso, cache, path = archive
    staged = b"B" + b"A" * 33999
    path.write_bytes(staged)
    append = ISOBackend.append

    def change_source(self, staging, label, expected_session_count):
        append(self, staging, label, expected_session_count)
        path.write_bytes(b"C" + b"A" * 33999)

    monkeypatch.setattr(ISOBackend, "append", change_source)
    assert dispatch(_args("sync", source, iso)) == 0
    assert _entry(iso, 1).type == "delta"
    assert cache.get("data.bin", 1) == staged
    _restore_matches(iso, tmp_path / "restored", staged)


def test_mismatched_staged_content_skips_cache_entry(archive, monkeypatch, caplog):
    """
    Input: an altered encrypted full copy after the burn
    Output: no delta cache entry and a checksum warning
    Details: Keep the committed disc session intact.
    """
    source, iso, cache, path = archive
    path.write_bytes(b"B" + b"A" * 33999)
    append = ISOBackend.append

    def alter_sidecar(self, staging, label, expected_session_count):
        append(self, staging, label, expected_session_count)
        sidecar = staging.with_name(staging.name + ".cache_full")
        for entry in sidecar.iterdir():
            entry.write_bytes(b"wrong full content")

    monkeypatch.setattr(ISOBackend, "append", alter_sidecar)
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso)) == 0
    assert _entry(iso, 1).type == "delta"
    assert cache.get("data.bin", 1) is None
    assert any("cache full content checksum did not match for data.bin" in r.message for r in caplog.records)


def _assert_source_change_report(capsys, records) -> None:
    """
    Input: captured stderr and log records
    Output: None
    Details: Both channels must identify the changed source file.
    """
    assert (
        "Warning: data.bin changed while it was read. "
        "The archive holds the version that was read."
    ) in capsys.readouterr().err
    assert any("source changed while it was read: data.bin" in r.getMessage() for r in records)


def test_source_changes_between_scan_and_staging(archive, tmp_path, monkeypatch, caplog, capsys):
    """
    Input: source bytes change after the scan
    Output: a new session that holds the bytes that staging read
    Details: A later sync restores the content that the next scan sees.
    """
    source, iso, cache, path = archive
    intended = b"B" + b"A" * 33999
    path.write_bytes(intended)
    original = Path.read_bytes
    changed = b"C" + b"A" * 33999
    reads = 0

    def changed_read(self):
        nonlocal reads
        if self == path:
            reads += 1
            return original(self) if reads == 1 else changed
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", changed_read)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso)) == 0
    assert reads == 2
    _assert_source_change_report(capsys, caplog.records)
    assert ISOBackend(iso, disc_size=DISC_BYTES).mediainfo().session_count == 2
    assert cache.get("data.bin", 1) == changed
    monkeypatch.setattr(Path, "read_bytes", original)
    _restore_matches(iso, tmp_path / "restored_read", changed)
    assert dispatch(_args("sync", source, iso)) == 0
    _restore_matches(iso, tmp_path / "restored", intended)


def test_sigint_after_delta_sidecar_creation_removes_it(archive, tmp_path, monkeypatch):
    """
    Input: SIGINT during staging after a delta sidecar is made
    Output: no sidecar and no new cache entry
    Details: A second staged file triggers the interrupt check.
    """
    import oddarchiver.session as session

    source, iso, cache, path = archive
    path.write_bytes(b"B" + b"A" * 33999)
    (source / "extra.bin").write_bytes(b"extra")
    old = cache.get("data.bin", 0)
    original = session.shutil.move
    sidecar_path = tmp_path / "xdg_runtime" / "oddarchiver" / "oddarchiver_staging_001.cache_full"
    observed = []

    def interrupt_after_sidecar(src, dest):
        result = original(src, dest)
        if str(dest).startswith(str(sidecar_path)):
            observed.append(sidecar_path.is_dir())
            session._handle_sigint(2, None)
        return result

    monkeypatch.setattr(session.shutil, "move", interrupt_after_sidecar)
    assert dispatch(_args("sync", source, iso)) == 1
    assert observed == [True]
    assert not sidecar_path.exists()
    assert cache.get("data.bin", 0) == old
    assert cache.get("data.bin", 1) is None
