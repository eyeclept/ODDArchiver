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

    chunks = session_mod.build_staging(
        session_n=0,
        source=tmp_path,
        disc_state={},
        disc_origin={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    staging = chunks[0][0]
    try:
        from oddarchiver.manifest import read_manifest
        manifest = read_manifest(staging / "session_000" / "manifest.json")
        assert len(manifest.entries) == 2
        for entry in manifest.entries:
            assert (staging / entry.file).exists(), f"blob missing: {entry.file}"
        assert chunks[0][1] == 0
        assert chunks[0][2] is False
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

    chunks = session_mod.build_staging(
        session_n=0,
        source=tmp_path,               # staging_root below is nested inside this
        disc_state={},
        disc_origin={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    staging = chunks[0][0]
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
    disc_origin = {"doc.txt": 0}

    chunks = session_mod.build_staging(
        session_n=1,
        source=tmp_path,
        disc_state=disc_state,
        disc_origin=disc_origin,
        backend=_make_backend(),
        cache=_make_cache(old_content),   # cache returns the old plaintext (NullCrypto)
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    staging = chunks[0][0]
    try:
        from oddarchiver.manifest import read_manifest
        manifest = read_manifest(staging / "session_001" / "manifest.json")
        delta_entries = [e for e in manifest.entries if e.type == "delta"]
        assert len(delta_entries) == 1, "expected one delta entry"
        assert (staging / delta_entries[0].delta_file).exists(), \
            f"delta blob missing: {delta_entries[0].delta_file}"
        assert chunks[0][1] == 1
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_changed_file_uses_true_origin_session_not_session_n_minus_1(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Regression for RuntimeError: No entry for '<path>' in session <N>
        manifest. A file's checksum in disc_state/disc_origin reflects
        session 0 (its true last write); sessions 1 and 2 never touched it.
        Building session 3 with a new checksum for that file must fetch the
        old blob from session 0 (its recorded origin), not session_n - 1 (2),
        which would have no entry for the file and crash in the real cache.
    """
    old_content = b"original content"
    new_content = b"changed content"
    (tmp_path / "doc.txt").write_bytes(new_content)

    disc_state = {"doc.txt": hashlib.sha256(old_content).hexdigest()}
    disc_origin = {"doc.txt": 0}
    cache = _make_cache(old_content)

    chunks = session_mod.build_staging(
        session_n=3,
        source=tmp_path,
        disc_state=disc_state,
        disc_origin=disc_origin,
        backend=_make_backend(),
        cache=cache,
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    staging = chunks[0][0]
    try:
        cache.get_with_fallback.assert_called_once()
        call_args = cache.get_with_fallback.call_args
        called_path = call_args.args[0] if call_args.args else call_args.kwargs["rel_path"]
        called_session = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["session"]
        assert called_path == "doc.txt"
        assert called_session == 0, (
            f"expected origin session 0 (true last write), got {called_session} "
            "(session_n - 1 would be 2, the bug this test guards against)"
        )
        assert chunks[0][1] == 3
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
    disc_origin = {"kept.txt": 1, "gone.txt": 1}

    chunks = session_mod.build_staging(
        session_n=2,
        source=tmp_path,
        disc_state=disc_state,
        disc_origin=disc_origin,
        backend=_make_backend(),
        cache=_make_cache(b"old kept"),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    staging = chunks[0][0]
    try:
        manifest = read_manifest(staging / "session_002" / "manifest.json")
        assert "gone.txt" in manifest.deleted
        assert "kept.txt" not in manifest.deleted
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_space_check_splits_when_over_remaining(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        When remaining_bytes * SPACE_SAFETY_MARGIN cannot hold the
        changeset but a full medium can, build_staging splits into 2+
        chunks instead of exiting. The first chunk is the current
        volume (is_new_volume=False); later chunks are new volumes
        with contiguous session numbers. Combined file content covers
        every new file exactly once.
    """
    (tmp_path / "big.bin").write_bytes(b"X" * 1024 * 64)   # 64 KB

    # remaining=1 makes the current-medium budget 0, so the 64 KB file
    # cannot land on volume 0. A mock backend has no int disc_size, so
    # the next volume uses BDXL_DISC_BYTES * SPACE_SAFETY_MARGIN.
    tiny_backend = _make_backend(remaining_bytes=1)

    chunks = session_mod.build_staging(
        session_n=0,
        source=tmp_path,
        disc_state={},
        disc_origin={},
        backend=tiny_backend,
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        assert len(chunks) >= 2
        assert chunks[0][2] is False
        assert all(is_new for _, _, is_new in chunks[1:])
        assert [n for _, n, _ in chunks] == list(range(chunks[0][1], chunks[0][1] + len(chunks)))
        staged_paths: list[str] = []
        for staging, session_n, _is_new in chunks:
            manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
            staged_paths.extend(entry.path for entry in manifest.entries)
        assert staged_paths.count("big.bin") == 1
        assert "big.bin" in staged_paths
    finally:
        import shutil
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_append_merge_overhead_forces_split_even_when_raw_bytes_fit(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Regression for a real crash: growisofs -M failed with "No space left
        on device" appending session 004 onto a disc that already held
        thousands of files, even though the packing step had already
        approved the changeset as fitting the disc's remaining_bytes budget.
        Root cause: ISO9660 multisession append rebuilds the whole disc's
        cumulative directory tree, which costs real space beyond the new
        files' own bytes, growing with how many files are already on the
        disc -- confirmed by direct reproduction (appending 3085 files onto
        a real UDF image already holding 8000 files needed 19.9 MB more than
        a fresh build of the same 3085 files alone). 10 prior files here
        deduct 10 * MERGE_OVERHEAD_PER_PRIOR_FILE_BYTES (81,920 bytes) from
        the 200,000-byte remaining budget. The new file (150,000 bytes)
        fits the old, undeducted budget (0.95 * 200,000 = 190,000) but not
        the corrected one (0.95 * (200,000 - 81,920) = 112,176), so it must
        land on a fresh second volume instead of the current, nearly-full
        medium.
    """
    (tmp_path / "new_big_file.bin").write_bytes(b"X" * 150_000)

    disc_state = {f"prior_{i:04d}.txt": "checksum" for i in range(10)}
    disc_origin = {p: 0 for p in disc_state}
    tight_backend = _make_backend(remaining_bytes=200_000)

    chunks = session_mod.build_staging(
        session_n=4,
        source=tmp_path,
        disc_state=disc_state,
        disc_origin=disc_origin,
        backend=tight_backend,
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        assert len(chunks) >= 2, "expected a split onto a new volume"
        assert chunks[0][2] is False
        assert all(is_new for _, _, is_new in chunks[1:])
        staged_paths: list[str] = []
        for staging, session_n, _is_new in chunks:
            manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
            staged_paths.extend(entry.path for entry in manifest.entries)
        assert staged_paths.count("new_big_file.bin") == 1
        # The new file must not land on chunk 0 (the nearly-full current medium).
        chunk0_staging, chunk0_session, _ = chunks[0]
        chunk0_manifest = read_manifest(
            chunk0_staging / f"session_{chunk0_session:03d}" / "manifest.json"
        )
        assert "new_big_file.bin" not in {e.path for e in chunk0_manifest.entries}
    finally:
        import shutil
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)



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
            disc_origin={},
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
            disc_origin={},
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

    chunks = session_mod.build_staging(
        session_n=0,
        source=tmp_path,
        disc_state={},
        disc_origin={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    staging = chunks[0][0]
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

    chunks = session_mod.build_staging(
        session_n=0,
        source=tmp_path,
        disc_state={},
        disc_origin={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=staging_root,
    )
    staging = chunks[0][0]
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
            disc_origin={},
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
