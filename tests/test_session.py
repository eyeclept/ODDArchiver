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
import logging
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import oddarchiver.session as session_mod
from oddarchiver.crypto import (
    KeyfileCrypto, NullCrypto, PassphraseCrypto, generate_keyfile,
)
from oddarchiver.disc import DiscInfo
from oddarchiver.manifest import read_manifest
from tests.helpers import _assert_build_staging_interrupted, fill

# Globals
logger = logging.getLogger(__name__)
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


@pytest.mark.parametrize("mode", ["keyfile", "passphrase"])
def test_encrypted_staging_never_writes_plaintext_manifest(tmp_path, monkeypatch, mode):
    """
    Input: tmp_path, monkeypatch, encryption mode
    Output: None
    Details: Staging and manifest patching never write a plaintext manifest.
    """
    from oddarchiver.cli import _patch_manifest

    if mode == "keyfile":
        key = tmp_path / "key.bin"
        generate_keyfile(str(key))
        crypto = KeyfileCrypto(str(key))
    else:
        crypto = PassphraseCrypto(passphrase="test passphrase")
    source = tmp_path / "source"
    source.mkdir()
    (source / "secret.txt").write_bytes(b"private content")
    original_write_bytes = Path.write_bytes
    plaintext_writes = []

    def track_write(path, data):
        if path.name in ("manifest.json", "manifest.json.tmp"):
            plaintext_writes.append(path)
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", track_write)
    chunks = session_mod.build_staging(
        session_n=0, source=source, disc_state={}, disc_origin={},
        backend=_make_backend(), cache=_make_cache(), crypto=crypto,
        _staging_root=tmp_path / "staging",
    )
    staging, session_n, _ = chunks[0]
    session_dir = staging / "session_000"
    assert not plaintext_writes
    assert not (session_dir / "manifest.json").exists()
    assert (session_dir / "manifest.enc").exists()
    manifest = _patch_manifest(
        staging, session_n, "TEST", {"mode": mode}, crypto=crypto,
    )
    assert manifest.entries[0].path == "secret.txt"
    assert not plaintext_writes
    assert not (session_dir / "manifest.json").exists()
    assert (session_dir / "manifest.enc").exists()


def test_plaintext_staging_keeps_manifest_json(tmp_path):
    """
    Input: tmp_path
    Output: None
    Details: The none mode writes and patches a plaintext manifest.
    """
    from oddarchiver.cli import _patch_manifest

    source = tmp_path / "source"
    source.mkdir()
    (source / "public.txt").write_bytes(b"public content")
    chunks = session_mod.build_staging(
        session_n=0, source=source, disc_state={}, disc_origin={},
        backend=_make_backend(), cache=_make_cache(), crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    staging, session_n, _ = chunks[0]
    session_dir = staging / "session_000"
    assert (session_dir / "manifest.json").exists()
    assert not (session_dir / "manifest.enc").exists()
    manifest = _patch_manifest(
        staging, session_n, "TEST", {"mode": "none"}, crypto=NullCrypto(),
    )
    assert manifest.entries[0].path == "public.txt"
    assert (session_dir / "manifest.json").exists()
    assert not (session_dir / "manifest.enc").exists()


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
        assert not chunks[0][2]
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


def _staged_paths(chunks: list[tuple[Path, int, bool]]) -> list[str]:
    """
    Input:  chunks - staged sessions
    Output: Paths of all manifest entries in chunk order
    """
    staged_paths: list[str] = []
    for staging, session_n, _is_new in chunks:
        manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
        staged_paths.extend(entry.path for entry in manifest.entries)
    return staged_paths


def _assert_new_volume_chunks(chunks: list[tuple[Path, int, bool]]) -> None:
    """
    Input:  chunks - staged sessions
    Output: None
    """
    assert not chunks[0][2]
    assert all(is_new for _, _, is_new in chunks[1:])


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
    (tmp_path / "big.bin").write_bytes(fill(b"X", 1024) * 64)   # 64 KB

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
        _assert_new_volume_chunks(chunks)
        assert [n for _, n, _ in chunks] == list(range(chunks[0][1], chunks[0][1] + len(chunks)))
        staged_paths = _staged_paths(chunks)
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
        land on a fresh volume. Nothing fits the current medium, so sync
        skips it: no empty session is burned there, the sessions renumber
        from 4, and the only chunk is a new volume.
    """
    (tmp_path / "new_big_file.bin").write_bytes(fill(b"X", 150_000))

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
        assert len(chunks) == 1, (
            "expected no empty session on the full current medium"
        )
        staging, session, is_new = chunks[0]
        assert is_new, "the only chunk must burn on a fresh volume"
        assert session == 4, "sessions renumber from the first session"
        assert _staged_paths(chunks) == ["new_big_file.bin"]
        manifest = read_manifest(
            staging / f"session_{session:03d}" / "manifest.json"
        )
        assert [entry.path for entry in manifest.entries] == ["new_big_file.bin"]
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
    (tmp_path / "big.bin").write_bytes(fill(b"X", 1024) * 64)   # 64 KB source file

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
    real_hash = session_mod.hash_file

    def hash_then_signal(path, chunk_size, digest=None):
        session_mod._sigint_received = True
        return real_hash(path, chunk_size, digest)

    monkeypatch.setattr(session_mod, "hash_file", hash_then_signal)

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    expected_staging = staging_root / "oddarchiver_staging_001"

    _assert_build_staging_interrupted(
        tmp_path, _make_backend(), _make_cache(), staging_root, session_n=1,
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
    Input:  missing lockfile and corrupt lockfile under a staging root
    Output: _check_staging_lock does not raise; later build_staging proceeds
    """
    no_lock_dir = tmp_path / "no_lock"
    no_lock_dir.mkdir()
    session_mod._check_staging_lock(no_lock_dir, session_n=0)

    staging_root = tmp_path / "staging"
    corrupt_dir = staging_root / "oddarchiver_staging_000"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / ".lock").write_text("not-a-pid")
    session_mod._check_staging_lock(corrupt_dir, session_n=0)

    (tmp_path / "file.txt").write_bytes(b"content")
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
        assert staging.is_dir()
        assert (staging / ".lock").read_text().strip()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def test_next_archive_session_uses_catalog_max_not_disc_length(tmp_path, monkeypatch):
    """
    Input:  volume 0 with 42 local sessions and catalog last session 50
    Output: next session is 51, not 42
    """
    from oddarchiver.catalog import CatalogDisc, remember_discs, ArchiveCatalog, next_archive_session
    from oddarchiver.manifest import Manifest

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    remember_discs(ArchiveCatalog(
        "two-vol", 1, 50,
        {
            0: CatalogDisc(0, "V0", 0, 41),
            1: CatalogDisc(1, "V1", 42, 50),
        },
        {},
        True,
    ))
    local = [
        Manifest(
            version=3, session=n, timestamp="t", source="s", label="V0",
            based_on_session=None if n == 0 else n - 1,
            encryption={"mode": "none"}, entries=[], deleted=[],
            manifest_checksum="",
        )
        for n in range(42)
    ]
    assert next_archive_session("two-vol", local, 0) == 51


def test_archive_sync_lock_rejects_second_process(tmp_path, monkeypatch):
    """
    Input:  held archive sync lock
    Output: a second process raises RuntimeError naming the lock path
    """
    import subprocess
    import sys

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = str(Path(__file__).resolve().parents[1])
    with session_mod.archive_sync_lock("lock-arch"):
        child = subprocess.run(
            [
                sys.executable, "-c",
                "from oddarchiver.session import archive_sync_lock\n"
                "try:\n"
                "    with archive_sync_lock('lock-arch'):\n"
                "        pass\n"
                "    raise SystemExit(0)\n"
                "except RuntimeError as exc:\n"
                "    print(exc)\n"
                "    raise SystemExit(2)\n",
            ],
            cwd=root,
            env={**__import__("os").environ, "PYTHONPATH": root,
                 "XDG_STATE_HOME": str(tmp_path / "state")},
            capture_output=True,
            text=True,
        )
    assert child.returncode == 2
    assert "lock-arch" in child.stdout or "sync.lock" in child.stdout


def test_sync_refuses_older_volume_of_two_volume_iso(tmp_path, monkeypatch, capsys):
    """
    Input:  two-volume ISO, then sync against volume 0
    Output: refuse; stderr does not propose first_session plus manifest count
    """
    from oddarchiver.cli import dispatch
    from oddarchiver.catalog import next_archive_session
    from oddarchiver.disc import ISOBackend
    from oddarchiver.manifest import load_session_manifests
    from oddarchiver.volume import read_volume_marker
    from tests.helpers import _make_args

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = tmp_path / "src"
    source.mkdir()
    (source / "seed.txt").write_bytes(b"seed")
    iso = tmp_path / "s.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size="100000",
    )) == 0
    (source / "a.bin").write_bytes(b"A" * 80_000)
    (source / "b.bin").write_bytes(b"B" * 80_000)
    assert dispatch(_make_args(
        command="sync", source=str(source), test_iso=str(iso),
        disc_size="100000",
    )) == 0
    assert (tmp_path / "s.vol1.iso").is_file()
    vol0 = ISOBackend(iso, disc_size=100000)
    marker = read_volume_marker(vol0)
    assert marker is not None
    manifests = load_session_manifests(vol0, marker=marker)
    next_n = next_archive_session(marker.archive_id, manifests, marker.first_session)
    bogus = marker.first_session + len(manifests)
    assert next_n != bogus

    rc = dispatch(_make_args(
        command="sync", source=str(source), test_iso=str(iso),
        disc_size="100000",
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "older volume" in err
    assert f"session {bogus}" not in err.lower()
    assert f"session_{bogus:03d}" not in err


