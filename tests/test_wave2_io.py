"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Regression tests for restore I/O: atomic dest writes, split-file
    O_NOFOLLOW appends, --all-disks --read-mode, real-size extract
    budgets, one dest hash per decision, and blob extract without a
    RAM dict of payloads.
"""
# Imports
from __future__ import annotations

import errno
import hashlib
import logging
import os
import subprocess
from pathlib import Path

import pytest

import oddarchiver.diskread as diskread_mod
import oddarchiver.restore as restore_mod
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import DiscBackend, ISOBackend
from oddarchiver.diskread import _materialize
from oddarchiver.filehash import hash_file
from oddarchiver.restore import run_session_replay
from oddarchiver.restore_plan import restore_archive
from oddarchiver.restore_state import load_restore_state, write_restore_state
from tests.test_restore_plan import Acquisition, _CRYPTO, _source
from tests.test_restore_plan_safety import _one_disc_sessions
from tests.helpers import _entry

# Globals
logger = logging.getLogger(__name__)
_log = logger

_HASH_CHUNK = 1024 * 1024

# Functions
class _Source:
    """Input: session root. Output: session roots. Details: Supply local blobs."""

    def __init__(self, root: Path) -> None:
        """Input: blob root. Output: None. Details: Keep the root for every session."""
        self.root = root

    def open_session(self, session_n: int) -> Path:
        """Input: session number. Output: blob root. Details: Share the root."""
        return self.root


@pytest.fixture(autouse=True)
def _state_isolation(tmp_path, monkeypatch):
    """Input: isolated test directory. Output: None. Details: Isolate state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _replay(tmp_path: Path, entries, *, recover=False, prior=None):
    """Input: entries and mode. Output: replay result. Details: Use local blobs."""
    return run_session_replay(
        tmp_path / "dest", _Source(tmp_path / "disc"), NullCrypto(),
        {"file.bin": entries}, {}, entries[-1][0], True, None,
        prior or {}, False, recover=recover,
    )


def _blob_batches_from_paths(paths) -> list[str]:
    """Input: disc paths. Output: blob paths only."""
    return [path for path in paths if "/full/" in path or "/deltas/" in path]


def _track_blob_extracts(monkeypatch) -> list[list[str]]:
    """Input: monkeypatch. Output: blob path batches from extract or read."""
    batches: list[list[str]] = []

    def note(paths):
        blobs = _blob_batches_from_paths(paths)
        if blobs:
            batches.append(list(blobs))

    orig_rp = ISOBackend.read_paths

    def track_rp(self, paths):
        note(paths)
        return orig_rp(self, paths)

    monkeypatch.setattr(ISOBackend, "read_paths", track_rp)
    orig_ep = getattr(ISOBackend, "extract_paths", None)
    if orig_ep is not None:
        def track_ep(self, paths, dest_root, max_bytes=None):
            note(paths)
            return orig_ep(self, paths, dest_root, max_bytes=max_bytes)

        monkeypatch.setattr(ISOBackend, "extract_paths", track_ep)
    return batches


def test_finalize_write_error_keeps_old_dest_bytes(tmp_path, monkeypatch):
    """An injected dest write error must leave the old destination bytes."""
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "file.bin"
    original = b"original destination"
    target.write_bytes(original)
    entry = _entry(tmp_path, 0, b"new restored payload")
    real_wb = Path.write_bytes
    real_replace = os.replace

    def truncating_write(self, data):
        if self.resolve() == target.resolve():
            real_wb(self, b"")
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_wb(self, data)

    def failing_replace(src, dst):
        if Path(dst).resolve() == target.resolve():
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(Path, "write_bytes", truncating_write)
    monkeypatch.setattr(os, "replace", failing_replace)
    restored, failed, _, _, _, _, _ = _replay(tmp_path, [(0, entry)])
    assert restored == 0
    assert failed == 1
    assert target.read_bytes() == original


def test_split_chunk_symlink_leaves_target_unchanged(tmp_path):
    """A symlink at the partial path between chunks must not change its target."""
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "file.bin"
    target.write_bytes(b"original destination")
    victim = tmp_path / "victim.bin"
    victim_bytes = b"do-not-touch-this-target"
    victim.write_bytes(victim_bytes)
    first = _entry(tmp_path, 0, b"first", chunk=0, count=2)
    payload = b"firstsecond"
    first.result_checksum = hashlib.sha256(payload).hexdigest()
    restored, failed, updated, _, _, _, _ = _replay(tmp_path, [(0, first)])
    assert (restored, failed) == (1, 0)
    partial = dest / "file.bin.oddarchiver-partial"
    assert partial.is_file()
    assert target.read_bytes() == b"original destination"
    partial.unlink()
    partial.symlink_to(victim)
    second = _entry(tmp_path, 1, b"second", chunk=1, count=2)
    second.result_checksum = first.result_checksum
    result = run_session_replay(
        dest, _Source(tmp_path / "disc"), NullCrypto(),
        {"file.bin": [(1, second)]}, {}, 1, True, None, updated, False,
    )
    assert result[1] >= 1
    assert victim.read_bytes() == victim_bytes
    assert target.read_bytes() == b"original destination"
    assert partial.is_symlink()


def test_all_disks_sequential_one_blob_batch_per_session(tmp_path, monkeypatch):
    """--read-mode sequential must extract one blob batch per session."""
    files = [("a.txt", b"A" * 100), ("b.txt", b"B" * 100), ("c.txt", b"C" * 100)]
    iso = _one_disc_sessions(tmp_path, files)
    backend, catalog, manifests = _source(iso)
    batches = _track_blob_extracts(monkeypatch)
    dest = tmp_path / "seq"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=Acquisition([iso], []), read_mode="sequential",
    )
    assert result.failures == 0
    assert {name: (dest / name).read_bytes() for name, _content in files} == dict(files)
    assert len(batches) == 3
    assert all(len(batch) == 1 for batch in batches)


def test_understated_sizes_abort_before_budget(tmp_path, monkeypatch):
    """Understated manifest sizes must stop before written bytes pass the budget."""
    content = b"X" * 8000
    iso = _one_disc_sessions(tmp_path, [("big.txt", content)])
    backend, catalog, manifests = _source(iso)
    catalog.history["big.txt"][-1].full_size_bytes = 1
    for manifest in manifests:
        for entry in manifest.entries:
            entry.full_size_bytes = 1
    usage = type("Usage", (), {"free": 80})()
    monkeypatch.setattr(
        "oddarchiver.restore_plan.shutil.disk_usage", lambda path: usage,
    )
    monkeypatch.setattr(
        "oddarchiver.diskread.shutil.disk_usage", lambda path: usage,
    )
    landed = {"n": 0}
    real_materialize = diskread_mod._materialize

    def counting_materialize(*args, **kwargs):
        try:
            return real_materialize(*args, **kwargs)
        finally:
            root = Path(args[2])
            if root.exists():
                landed["n"] = sum(
                    path.stat().st_size for path in root.rglob("*") if path.is_file()
                )

    monkeypatch.setattr(diskread_mod, "_materialize", counting_materialize)
    dest = tmp_path / "space"
    with pytest.raises(RuntimeError, match="Not enough local space"):
        restore_archive(
            dest, backend, _CRYPTO, catalog, paths=["big.txt"],
            manifests=manifests, acquisition=Acquisition([iso], []),
        )
    budget = int(80 * 0.95)
    assert landed["n"] <= budget
    assert not (dest / "big.txt").exists()


def test_dest_file_hashed_at_most_once_per_disc(tmp_path, monkeypatch):
    """Each destination file must be hashed at most once on a resume disc."""
    files = [("a.txt", b"alpha"), ("b.txt", b"bravo")]
    iso = _one_disc_sessions(tmp_path, files)
    backend, catalog, manifests = _source(iso)
    dest = tmp_path / "hash-once"
    first = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=Acquisition([iso], []),
    )
    assert first.failures == 0
    assert (dest / "a.txt").read_bytes() == b"alpha"
    state = load_restore_state(dest, catalog.archive_id)
    assert state is not None
    state.done_files.clear()
    write_restore_state(dest, state)
    opens: dict[str, int] = {}
    dest_files = {(dest / name).resolve() for name, _content in files}
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        resolved = self.resolve()
        if resolved in dest_files:
            key = str(resolved)
            opens[key] = opens.get(key, 0) + 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    second = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=Acquisition([iso], []),
    )
    assert second.failures == 0
    assert second.restored == 0
    for name, _content in files:
        path = str((dest / name).resolve())
        assert opens.get(path, 0) <= 1, (name, opens)


def test_sha256_path_reads_one_mib_chunks(tmp_path, monkeypatch):
    """Destination hashing must read 1 MiB chunks, not 256 MiB chunks."""
    path = tmp_path / "hashed.bin"
    path.write_bytes(b"x" * (_HASH_CHUNK + 10))
    sizes: list[int] = []
    real_open = Path.open

    def tracking_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self.resolve() != path.resolve():
            return handle
        orig_read = handle.read

        def read(size=-1):
            sizes.append(size)
            return orig_read(size)

        handle.read = read
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)
    digest = hash_file(path, restore_mod.HASH_CHUNK_SIZE).hexdigest()
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert sizes[0] == _HASH_CHUNK
    assert 256 * 2**20 not in sizes
    assert restore_mod.HASH_CHUNK_SIZE == _HASH_CHUNK

def test_blob_extract_does_not_hold_payload_dict(tmp_path, monkeypatch):
    """Blob extraction must not keep payloads in a dict[str, bytes]."""
    blob_name = "ab" * 32
    blob_path = f"session_000/full/{blob_name}"
    payload = b"P" * 4096
    dest_root = tmp_path / "extracted"
    dest_root.mkdir()
    reads: list[str] = []
    real_read = Path.read_bytes

    def spy_read(self):
        reads.append(self.name)
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", spy_read)

    def fake_run(cmd, **kwargs):
        index = 0
        while index < len(cmd):
            if cmd[index] == "-extract":
                dest = Path(cmd[index + 2])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(payload)
                index += 3
            else:
                index += 1
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    backend = DiscBackend("/dev/sr0")
    reads.clear()
    result = backend.extract_paths([blob_path], dest_root)
    assert not isinstance(result, dict)
    assert blob_name not in reads
    assert (dest_root / blob_path).read_bytes() == payload


def test_materialize_uses_extract_paths_not_read_paths(tmp_path, monkeypatch):
    """Blob materialize must call extract_paths and not read_paths."""
    files = [("only.txt", b"payload-bytes")]
    iso = _one_disc_sessions(tmp_path, files)
    backend = ISOBackend(iso, disc_size=20 * 2**20)
    manifests = _source(iso)[2]
    blob = manifests[0].entries[0].file
    rp_calls: list[list[str]] = []
    ep_calls: list[list[str]] = []
    orig_rp = ISOBackend.read_paths
    orig_ep = getattr(ISOBackend, "extract_paths", None)

    def track_rp(self, paths):
        blobs = _blob_batches_from_paths(paths)
        if blobs:
            rp_calls.append(list(blobs))
        return orig_rp(self, paths)

    monkeypatch.setattr(ISOBackend, "read_paths", track_rp)
    if orig_ep is not None:
        def track_ep(self, paths, dest_root, max_bytes=None):
            blobs = _blob_batches_from_paths(paths)
            if blobs:
                ep_calls.append(list(blobs))
            return orig_ep(self, paths, dest_root, max_bytes=max_bytes)

        monkeypatch.setattr(ISOBackend, "extract_paths", track_ep)

    root = tmp_path / "mat"
    _materialize(backend, [blob], root)
    assert (root / blob).read_bytes() == b"payload-bytes"
    assert rp_calls == []
    assert orig_ep is not None
    assert ep_calls == [[blob]]
