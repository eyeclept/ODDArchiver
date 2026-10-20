"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Check restore failures preserve destination files and report data loss.
"""
# Imports
import hashlib
import logging
import os
import shutil
from pathlib import Path

import pytest

import oddarchiver.restore as restore_mod
from oddarchiver.cache import CacheManager
from oddarchiver.cli import build_parser, dispatch
from oddarchiver.crypto import NullCrypto
from oddarchiver.manifest import ManifestEntry
from oddarchiver.restore import run_session_replay
from tests.helpers import _entry

# Globals
logger = logging.getLogger(__name__)
_log = logger


# Functions
class _Source:
    """Input: session root. Output: session roots. Details: Supply local blobs."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def open_session(self, session_n: int) -> Path:
        """Input: session number. Output: blob root. Details: Share the root."""
        return self.root


@pytest.fixture(autouse=True)
def _state_isolation(tmp_path, monkeypatch):
    """Input: isolated test directory. Output: None. Details: Isolate state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache))
    monkeypatch.setattr("oddarchiver.ledger.ledger_dir", lambda: tmp_path / "state" / "oddarchiver")


def _replay(tmp_path: Path, entries: list[tuple[int, ManifestEntry]], *, recover=False):
    """Input: entries and mode. Output: replay result. Details: Use local blobs."""
    return run_session_replay(
        tmp_path / "dest", _Source(tmp_path / "disc"), NullCrypto(),
        {"file.bin": entries}, {}, entries[-1][0], True, None, {}, False,
        recover=recover,
    )


def _dispatch(argv: list[str]) -> int:
    """Input: CLI arguments. Output: exit code. Details: Exercise real dispatch."""
    return dispatch(build_parser().parse_args(argv))


@pytest.mark.parametrize("streamed,chunk", [(False, None), (True, None), (False, 0)])
def test_replay_creates_private_nested_output_directories(
    tmp_path, streamed, chunk,
):
    """Create every missing output directory with mode 0700 under umask 022."""
    dest = tmp_path / "dest"
    dest.mkdir(mode=0o755)
    dest.chmod(0o755)
    entry = _entry(
        tmp_path, 0, b"private data", streamed=streamed,
        chunk=chunk, count=2 if chunk is not None else None,
    )
    entry.path = "one/two/file.bin"
    old_umask = os.umask(0o022)
    try:
        result = run_session_replay(
            dest, _Source(tmp_path / "disc"), NullCrypto(),
            {entry.path: [(0, entry)]}, {}, 0, True, None, {}, False,
        )
    finally:
        os.umask(old_umask)
    assert result[0:2] == (1, 0)
    assert dest.stat().st_mode & 0o777 == 0o755
    for directory in (dest / "one", dest / "one" / "two"):
        assert directory.stat().st_mode & 0o777 == 0o700
    if chunk is not None:
        assert (dest / "one" / "two" / "file.bin.oddarchiver-partial").exists()
    else:
        assert (dest / entry.path).read_bytes() == b"private data"



def test_single_disc_restore_creates_private_destination_components(
    tmp_path, capsys,
):
    """Use a saved plaintext mode and keep a pre-existing parent unchanged."""
    source = tmp_path / "src"
    (source / "one" / "two").mkdir(parents=True)
    (source / "one" / "two" / "file.txt").write_bytes(b"safe")
    iso = tmp_path / "nested.iso"
    assert _dispatch([
        "init", str(source), "--test-iso", str(iso),
        "--disc-size", "50mb", "--label", "TEST",
    ]) == 0
    capsys.readouterr()
    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    dest = parent / "dest" / "nested"
    old_umask = os.umask(0o022)
    try:
        assert _dispatch([
            "restore", str(dest), "--test-iso", str(iso),
        ]) == 0
    finally:
        os.umask(old_umask)
    assert (dest / "one" / "two" / "file.txt").read_bytes() == b"safe"
    assert parent.stat().st_mode & 0o777 == 0o755
    for directory in (
        parent / "dest", dest, dest / "one", dest / "one" / "two",
    ):
        assert directory.stat().st_mode & 0o777 == 0o700

def test_corrupt_streamed_blob_force_preserves_existing_file(tmp_path, caplog):
    """A streamed checksum failure must not replace the existing file."""
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "file.bin"
    target.write_bytes(b"original destination")
    entry = _entry(tmp_path, 0, b"correct data", streamed=True)
    (tmp_path / "disc" / entry.file).write_bytes(b"corrupted data")
    with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
        restored, failed, state, _, _, _, missing = _replay(tmp_path, [(0, entry)])
    assert (restored, failed, state, missing) == (0, 1, {}, [])
    assert target.read_bytes() == b"original destination"
    assert not list(dest.glob(".oddarchiver_restore_*"))
    assert any("Checksum mismatch" in record.message for record in caplog.records)


def _build_refuse_scratch(scratch: Path, real_rmtree):
    """Input: scratch path and original remover. Output: selective fake remover."""
    def refuse_scratch(path, *args, **kwargs):
        if Path(path) == scratch:
            raise OSError("forced cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    return refuse_scratch


def test_streamed_plaintext_cleanup_failure_counts_as_failure(tmp_path, monkeypatch, caplog):
    """A temp directory cleanup error must not disappear after a good decrypt."""
    dest = tmp_path / "dest"
    dest.mkdir()
    entry = _entry(tmp_path, 0, b"new content", streamed=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real_rmtree = restore_mod.shutil.rmtree

    refuse_scratch = _build_refuse_scratch(scratch, real_rmtree)

    with monkeypatch.context() as patch:
        patch.setattr(restore_mod.tempfile, "mkdtemp", lambda **kwargs: str(scratch))
        patch.setattr(restore_mod.shutil, "rmtree", refuse_scratch)
        with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
            restored, failed, _, _, _, _, _ = _replay(tmp_path, [(0, entry)])
    assert restored == 0
    assert failed == 1
    assert (dest / "file.bin").read_bytes() == b"new content"
    assert any("forced cleanup failure" in record.message for record in caplog.records)
    shutil.rmtree(scratch)


def test_streamed_replace_not_finished_keeps_old_dest(tmp_path, monkeypatch, caplog):
    """If replace does not finish, the old dest stays and the file counts as failed."""
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "file.bin"
    target.write_bytes(b"original destination")
    entry = _entry(tmp_path, 0, b"new content", streamed=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real_rmtree = restore_mod.shutil.rmtree

    refuse_scratch = _build_refuse_scratch(scratch, real_rmtree)

    def fail_replace(src, dst):
        raise OSError("forced replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(restore_mod.tempfile, "mkdtemp", lambda **kwargs: str(scratch))
        patch.setattr(restore_mod.shutil, "rmtree", refuse_scratch)
        patch.setattr(restore_mod.os, "replace", fail_replace)
        with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
            restored, failed, _, _, _, _, _ = _replay(tmp_path, [(0, entry)])
    assert restored == 0
    assert failed == 1
    assert target.read_bytes() == b"original destination"
    assert any("forced replace failure" in record.message for record in caplog.records)
    shutil.rmtree(scratch)


def test_corrupt_available_blob_in_recovery_counts_as_failure(tmp_path, caplog):
    """An available corrupt blob is failed, not missing, in recovery mode."""
    dest = tmp_path / "dest"
    dest.mkdir()
    entry = _entry(tmp_path, 0, b"correct data")
    (tmp_path / "disc" / entry.file).write_bytes(b"wrong data")
    with caplog.at_level(logging.WARNING, logger="oddarchiver.restore"):
        restored, failed, updated, _, _, _, missing = _replay(
            tmp_path, [(0, entry)], recover=True,
        )
    assert (restored, failed, updated, missing) == (0, 1, {}, [])
    assert not (dest / "file.bin").exists()
    assert any("file.bin" in record.message for record in caplog.records)


def test_cli_restore_exit_one_names_suspect_session(tmp_path, capsys):
    """Restore must fail with a nonzero exit and name the suspect session."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "test.iso"
    assert _dispatch([
        "init", str(source), "--test-iso", str(iso),
        "--disc-size", "50mb", "--label", "TEST",
    ]) == 0
    from oddarchiver.disc import ISOBackend
    backend = ISOBackend(iso, disc_size=50 * 2**20)
    manifest_path = backend._sessions_root / "session_000" / "manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b"tamper")
    capsys.readouterr()
    rc = _dispatch(["restore", str(tmp_path / "dest"), "--test-iso", str(iso)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "session 0" in captured.err
    assert "SUSPECT" in captured.err


def test_recovery_decrypt_error_on_available_blob_counts_as_failure(tmp_path, monkeypatch):
    """A decrypt exception on an available blob is a failure, not missing."""
    dest = tmp_path / "dest"
    dest.mkdir()
    entry = _entry(tmp_path, 0, b"correct data")

    class _BadCrypto(NullCrypto):
        def decrypt(self, ciphertext: bytes, aad: bytes | None = None) -> bytes:
            raise ValueError("forced decrypt failure")

    result = run_session_replay(
        dest, _Source(tmp_path / "disc"), _BadCrypto(),
        {"file.bin": [(0, entry)]}, {}, 0, True, None, {}, False, recover=True,
    )
    assert result[0:2] == (0, 1)
    assert result[6] == []
    assert not (dest / "file.bin").exists()


def test_failed_chunk_write_keeps_target_and_does_not_record_chunk(tmp_path, monkeypatch):
    """A write error removes the partial file and does not update state."""
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "file.bin"
    target.write_bytes(b"original destination")
    entry = _entry(tmp_path, 0, b"first chunk", chunk=0, count=2)

    def fail_copy(source, output, *args, **kwargs):
        output.write(b"incomplete")
        raise OSError("forced write failure")

    monkeypatch.setattr(restore_mod.shutil, "copyfileobj", fail_copy)
    restored, failed, updated, _, _, _, missing = _replay(tmp_path, [(0, entry)])
    assert (restored, failed, updated, missing) == (0, 1, {}, [])
    assert target.read_bytes() == b"original destination"
    assert not (dest / "file.bin.oddarchiver-partial").exists()


def test_chunk_prefix_is_private_until_final_checksum(tmp_path):
    """The first chunk stays private and has mode 0600 until completion."""
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "file.bin"
    target.write_bytes(b"original destination")
    entry = _entry(tmp_path, 0, b"first", chunk=0, count=2)
    payload = b"firstsecond"
    entry.result_checksum = hashlib.sha256(payload).hexdigest()
    restored, failed, updated, _, _, _, _ = _replay(tmp_path, [(0, entry)])
    partial = dest / "file.bin.oddarchiver-partial"
    assert (restored, failed) == (1, 0)
    assert updated["file.bin"].volume_chunk == 0
    assert target.read_bytes() == b"original destination"
    assert partial.read_bytes() == b"first"
    assert partial.stat().st_mode & 0o777 == 0o600
    second = _entry(tmp_path, 1, b"second", chunk=1, count=2)
    second.result_checksum = entry.result_checksum
    result = run_session_replay(
        dest, _Source(tmp_path / "disc"), NullCrypto(),
        {"file.bin": [(1, second)]}, {}, 1, True, None,
        updated, False,
    )
    assert result[0:2] == (1, 0)
    assert target.read_bytes() == payload
    assert not partial.exists()
