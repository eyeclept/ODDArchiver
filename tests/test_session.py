"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for session.py. build_staging, scan/diff/stage, space check,
    manifest deleted list, and SIGINT cleanup.
"""
# Imports
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import oddarchiver.session as session_mod
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import DiscInfo
from oddarchiver.manifest import read_manifest

# Globals
_LARGE_REMAINING = 25 * 2**30   # 25 GB - passes space check for tiny staging dirs


# Functions


def _make_backend(remaining_bytes: int = _LARGE_REMAINING) -> MagicMock:
    """Return a mock BurnBackend whose mediainfo() returns the given remaining_bytes."""
    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=1,
        remaining_bytes=remaining_bytes,
        used_bytes=0,
        label="TEST",
    )
    return backend


def _make_cache(old_bytes: bytes = b"") -> MagicMock:
    """Return a mock CacheManager whose get_with_fallback returns old_bytes."""
    cache = MagicMock()
    cache.get_with_fallback.return_value = old_bytes
    return cache


def test_new_files_staged_in_full_dir(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        With an empty disc_state, all source files are new.
        build_staging must write them under session_NNN/full/.
    """
    (tmp_path / "hello.txt").write_bytes(b"hello world")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "data.bin").write_bytes(b"\x00\x01\x02")

    staging = session_mod.build_staging(
        session_n=0,
        source=tmp_path,
        disc_state={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        from oddarchiver.manifest import read_manifest
        manifest = read_manifest(staging / "session_000" / "manifest.json")
        assert len(manifest.entries) == 2
        for entry in manifest.entries:
            assert (staging / entry.file).exists(), f"blob missing: {entry.file}"
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_build_staging_excludes_nested_staging_tree_from_source_scan(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Regression: if the staging root ends up nested inside source (e.g.
        the home-directory fallback staging root when a user syncs a tree
        that contains their home directory), the staging dir's own
        bookkeeping files -- starting with its PID .lock file, written
        before the source scan runs -- must never be swept up and archived
        as if they were real source content.
    """
    (tmp_path / "hello.txt").write_bytes(b"hello world")

    staging = session_mod.build_staging(
        session_n=0,
        source=tmp_path,               # staging_root below is nested inside this
        disc_state={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        from oddarchiver.manifest import read_manifest
        manifest = read_manifest(staging / "session_000" / "manifest.json")
        staged_paths = {entry.path for entry in manifest.entries}
        assert staged_paths == {"hello.txt"}
        assert not any(".lock" in p for p in staged_paths)
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_changed_files_staged_in_deltas_dir(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A file present in disc_state with a different checksum is classified
        as changed. build_staging must produce a .xdelta entry under
        session_NNN/deltas/ for it (assuming delta < threshold).
    """
    # Write a large repetitive file so xdelta3 produces a small delta
    old_content = b"The quick brown fox jumps over the lazy dog.\n" * 200
    new_content = b"The quick brown fox jumps over the LAZY dog.\n" * 200

    src = tmp_path / "doc.txt"
    src.write_bytes(new_content)

    old_checksum = hashlib.sha256(old_content).hexdigest()
    disc_state = {"doc.txt": old_checksum}

    staging = session_mod.build_staging(
        session_n=1,
        source=tmp_path,
        disc_state=disc_state,
        backend=_make_backend(),
        cache=_make_cache(old_content),   # cache returns the old plaintext (NullCrypto)
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        from oddarchiver.manifest import read_manifest
        manifest = read_manifest(staging / "session_001" / "manifest.json")
        delta_entries = [e for e in manifest.entries if e.type == "delta"]
        assert len(delta_entries) == 1, "expected one delta entry"
        assert (staging / delta_entries[0].delta_file).exists(), \
            f"delta blob missing: {delta_entries[0].delta_file}"
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_deleted_list_in_manifest(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Files present in disc_state but absent from source must appear in
        the manifest's deleted list.
    """
    (tmp_path / "kept.txt").write_bytes(b"still here")

    disc_state = {
        "kept.txt": hashlib.sha256(b"old kept").hexdigest(),
        "gone.txt": hashlib.sha256(b"was here").hexdigest(),
    }

    staging = session_mod.build_staging(
        session_n=2,
        source=tmp_path,
        disc_state=disc_state,
        backend=_make_backend(),
        cache=_make_cache(b"old kept"),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        manifest = read_manifest(staging / "session_002" / "manifest.json")
        assert "gone.txt" in manifest.deleted
        assert "kept.txt" not in manifest.deleted
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_space_check_exits_1_when_over_limit(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        When staging size is at or above remaining_bytes * SPACE_SAFETY_MARGIN,
        build_staging must raise SystemExit(1).
    """
    # Write enough data to exceed a tiny remaining_bytes limit
    (tmp_path / "big.bin").write_bytes(b"X" * 1024 * 64)   # 64 KB

    # Set remaining so that 64 KB staging exceeds SPACE_SAFETY_MARGIN * remaining
    # remaining = 1 byte, limit about 0.95 bytes, staging far above limit
    tiny_backend = _make_backend(remaining_bytes=1)

    with pytest.raises(SystemExit) as exc_info:
        session_mod.build_staging(
            session_n=0,
            source=tmp_path,
            disc_state={},
            backend=tiny_backend,
            cache=_make_cache(),
            crypto=NullCrypto(),
            _staging_root=tmp_path / "staging",
        )

    assert exc_info.value.code == 1


def test_staging_space_check_exits_1_before_any_blob_written(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        The staging filesystem (often a small, RAM-backed tmpfs -- see
        _default_staging_root -- entirely separate from the target disc/ISO
        capacity) is checked before any file is hashed for a blob or staged.
        When free space there is below what the new/changed files need,
        build_staging must raise SystemExit(1) before writing a single blob,
        even though the disc/ISO backend reports plenty of remaining room.
        Regression for bug: init on a real 8.3 GB source directory crashed
        1300+ files into a 6497-file job with "OSError: [Errno 28] No space
        left on device" writing an encrypted blob to a full tmpfs staging
        dir, after already hashing all 6497 files and staging 5842 of them.
    """
    (tmp_path / "big.bin").write_bytes(b"X" * 1024 * 64)   # 64 KB source file

    real_disk_usage = session_mod.shutil.disk_usage
    tiny_usage = real_disk_usage(tmp_path)._replace(free=1)   # 1 byte free
    monkeypatch.setattr(session_mod.shutil, "disk_usage", lambda path: tiny_usage)

    with pytest.raises(SystemExit) as exc_info:
        session_mod.build_staging(
            session_n=0,
            source=tmp_path,
            disc_state={},
            backend=_make_backend(),   # plenty of disc room -- this is not that check
            cache=_make_cache(),
            crypto=NullCrypto(),
            _staging_root=tmp_path / "staging",
        )

    assert exc_info.value.code == 1


def test_sigint_during_staging_cleans_up_temp_dir(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        If _sigint_received is set during the staging scan, build_staging raises
        KeyboardInterrupt and the staging directory is removed.
    """
    (tmp_path / "file.txt").write_bytes(b"content")

    # Simulate SIGINT arriving during the sha256 scan
    real_sha256 = session_mod._sha256_file

    def sha256_then_signal(path: Path) -> str:
        session_mod._sigint_received = True
        return real_sha256(path)

    monkeypatch.setattr(session_mod, "_sha256_file", sha256_then_signal)

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    expected_staging = staging_root / "oddarchiver_staging_001"

    with pytest.raises(KeyboardInterrupt):
        session_mod.build_staging(
            session_n=1,
            source=tmp_path,
            disc_state={},
            backend=_make_backend(),
            cache=_make_cache(),
            crypto=NullCrypto(),
            _staging_root=staging_root,
        )

    assert not expected_staging.exists(), "staging dir must be removed after SIGINT"


def test_build_staging_writes_own_pid_to_lockfile(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A successful build_staging() call must leave a .lock file inside the
        staging dir naming the current process, so a later collision can be
        distinguished from a genuinely stale leftover.
    """
    (tmp_path / "file.txt").write_bytes(b"content")

    staging = session_mod.build_staging(
        session_n=0,
        source=tmp_path,
        disc_state={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        import os
        assert (staging / ".lock").read_text().strip() == str(os.getpid())
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_build_staging_removes_stale_dir_with_dead_pid_lock(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A staging dir left behind by a crashed process (lockfile names a PID
        that no longer exists) is safe to remove and rebuild, same as a
        pre-lock-era leftover with no .lock file at all.
    """
    (tmp_path / "file.txt").write_bytes(b"content")

    staging_root = tmp_path / "staging"
    stale_dir = staging_root / "oddarchiver_staging_000"
    stale_dir.mkdir(parents=True)
    (stale_dir / ".lock").write_text("999999999")  # guaranteed-dead pid
    sentinel = stale_dir / "old_leftover.txt"
    sentinel.write_text("crash leftover")

    staging = session_mod.build_staging(
        session_n=0,
        source=tmp_path,
        disc_state={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=staging_root,
    )
    try:
        import os
        assert not sentinel.exists(), "dead-pid-locked stale dir must be removed"
        assert (staging / ".lock").read_text().strip() == str(os.getpid())
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_build_staging_raises_when_locked_by_live_pid(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Regression for the real disc-corruption incident: two processes
        racing to (re)build the same deterministically-named staging
        directory must never result in one silently deleting the other's
        in-flight files. If the lockfile names a PID that is still alive
        (here, this very test process), build_staging must raise
        RuntimeError and leave the directory and its contents untouched.
    """
    (tmp_path / "file.txt").write_bytes(b"content")

    import os
    staging_root = tmp_path / "staging"
    live_dir = staging_root / "oddarchiver_staging_000"
    live_dir.mkdir(parents=True)
    (live_dir / ".lock").write_text(str(os.getpid()))
    in_flight = live_dir / "session_000" / "full" / "still_being_read.txt"
    in_flight.parent.mkdir(parents=True)
    in_flight.write_bytes(b"do not delete me")

    with pytest.raises(RuntimeError, match=r"in use by pid \d+"):
        session_mod.build_staging(
            session_n=0,
            source=tmp_path,
            disc_state={},
            backend=_make_backend(),
            cache=_make_cache(),
            crypto=NullCrypto(),
            _staging_root=staging_root,
        )

    assert in_flight.exists(), "live in-flight build must not be deleted"


def test_check_staging_lock_treats_missing_or_corrupt_lockfile_as_stale(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        _check_staging_lock must return quietly (no exception) both when
        there is no .lock file at all and when its contents are not a valid
        integer PID, since both are safe-to-remove cases rather than an
        active collision.
    """
    no_lock_dir = tmp_path / "no_lock"
    no_lock_dir.mkdir()
    session_mod._check_staging_lock(no_lock_dir, session_n=0)  # must not raise

    corrupt_dir = tmp_path / "corrupt_lock"
    corrupt_dir.mkdir()
    (corrupt_dir / ".lock").write_text("not-a-pid")
    session_mod._check_staging_lock(corrupt_dir, session_n=0)  # must not raise
