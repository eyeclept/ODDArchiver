"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for batched disc reads (read_paths), the shared
    load_session_manifests scanner, restore dropping its separate
    max-session probe, post-burn fast verify using in-memory
    manifests instead of re-reading the drive, session-ordered
    restore/verify (23c/23d), cache cold-miss batching (23f), and
    delta.process_files wiring (23g).
"""
# Imports
import argparse
import hashlib
import logging
import subprocess
from pathlib import Path

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.cli import _crypto_and_manifests, _run_init, _run_sync, build_parser
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import DiscBackend, ISOBackend, parse_disc_size
from oddarchiver.diskread import SequentialSource, make_session_source
from oddarchiver.manifest import (
    Manifest, ManifestEntry, build_disc_origin, build_disc_state,
    load_session_manifests, write_manifest,
)
from oddarchiver.restore import restore
from oddarchiver.session import build_staging
from oddarchiver.verify import verify
import oddarchiver.restore as restore_mod
import oddarchiver.delta as delta_mod
import oddarchiver.session as session_mod

# Globals
logger = logging.getLogger(__name__)
SMALL_DISC = 20 * 2**20  # 20 MiB - fast ISO builds
_crypto = NullCrypto()


# Functions


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_id(session_n: int, rel_path: str) -> str:
    """Input: session and path. Output: a legacy blob name for this fixture."""
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _make_staging(
    tmp_path: Path,
    session_n: int,
    files: dict[str, bytes],
    deleted: list[str] | None = None,
    based_on: int | None = None,
) -> Path:
    """
    Input:  tmp_path, session_n, files, deleted, based_on
    Output: Path to a staging directory ready for backend.init/append
    Details:
        Writes each file as an unencrypted full blob and a valid manifest.
    """
    session_name = f"session_{session_n:03d}"
    staging = tmp_path / f"staging_{session_n}"
    session_dir = staging / session_name
    full_dir = session_dir / "full"
    full_dir.mkdir(parents=True)

    entries = []
    for rel_path, content in files.items():
        blob_name = _blob_id(session_n, rel_path)
        (full_dir / blob_name).write_bytes(_crypto.encrypt(content))
        entries.append(ManifestEntry(
            path=rel_path,
            type="full",
            result_checksum=_sha256(content),
            full_size_bytes=len(content),
            file=f"{session_name}/full/{blob_name}",
        ))

    manifest = Manifest(
        version=1,
        session=session_n,
        timestamp=f"2026-04-23T00:00:0{session_n}Z",
        source="/src",
        label="TEST",
        based_on_session=based_on,
        encryption={},
        entries=entries,
        deleted=deleted or [],
        manifest_checksum="",
    )
    write_manifest(session_dir, manifest)
    return staging


def _burn(backend: ISOBackend, staging: Path, session_n: int) -> None:
    """Burn a staging directory via init (session 0) or append."""
    if session_n == 0:
        backend.init(staging, label="TEST", expected_session_count=0)
    else:
        backend.append(staging, label="TEST", expected_session_count=session_n)


def _make_args(**kwargs) -> argparse.Namespace:
    """Build a Namespace with the flags _run_init/_run_sync read."""
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "TEST",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "20mb",
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
        "mirror": None,
        "read_mode": "sequential",
        "yubikey": False,
        "yubikey_slot": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _xorriso_result(returncode, stderr=""):
    """Build a subprocess.CompletedProcess like xorriso -extract would return."""
    return subprocess.CompletedProcess(
        args=["xorriso"], returncode=returncode, stdout=b"", stderr=stderr.encode(),
    )


def _write_extract_dests(cmd, contents: dict[str, bytes]) -> None:
    """Honor every -extract src dest pair in a mocked xorriso argv."""
    i = 0
    while i < len(cmd):
        if cmd[i] == "-extract":
            src = cmd[i + 1].lstrip("/")
            dest = Path(cmd[i + 2])
            if src in contents:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(contents[src])
            i += 3
        else:
            i += 1


class _FakeBackend:
    """In-memory BurnBackend stand-in that records read_path/read_paths calls."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.read_path_calls: list[str] = []
        self.read_paths_calls: list[list[str]] = []

    def read_paths(self, paths):
        self.read_paths_calls.append(list(paths))
        return {p: self.files[p] for p in paths if p in self.files}

    def read_path(self, path):
        self.read_path_calls.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class _CountingISO(ISOBackend):
    """ISOBackend that records read_paths calls and post-burn read counts."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.read_paths_calls: list[list[str]] = []
        self.extract_paths_calls: list[list[str]] = []
        self.reads_after_burn = 0
        self._armed = False

    def init(self, staging, label, expected_session_count=0):
        super().init(staging, label, expected_session_count)
        self._armed = True

    def append(self, staging, label, expected_session_count):
        super().append(staging, label, expected_session_count)
        self._armed = True

    def read_paths(self, paths):
        self.read_paths_calls.append(list(paths))
        if self._armed:
            self.reads_after_burn += 1
        return super().read_paths(paths)

    def extract_paths(self, paths, dest_root, max_bytes=None):
        """Input: disc paths and target. Output: extracted bytes. Record each batch."""
        self.extract_paths_calls.append(list(paths))
        return super().extract_paths(paths, dest_root, max_bytes=max_bytes)




def _blob_batches(calls: list[list[str]]) -> list[list[str]]:
    """Return extract_paths calls that requested at least one blob path."""
    batches = []
    for call in calls:
        blobs = [p for p in call if "/full/" in p or "/deltas/" in p]
        if blobs:
            batches.append(blobs)
    return batches


def _multi_session_iso(tmp_path: Path) -> tuple[ISOBackend, dict[str, bytes]]:
    """
    Input:  tmp_path
    Output: (backend, source_bytes) for a 2-session archive
    Details:
        Session 0 writes a.txt and b.txt; session 1 writes c.txt and a
        changed a.txt. Source bytes are the latest reconstructed tree.
    """
    iso = tmp_path / "t.iso"
    backend = _CountingISO(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {
        "a.txt": b"one",
        "b.txt": b"two",
    }), 0)
    _burn(backend, _make_staging(tmp_path, 1, {
        "a.txt": b"one-changed",
        "c.txt": b"three",
    }, based_on=0), 1)
    latest = {"a.txt": b"one-changed", "b.txt": b"two", "c.txt": b"three"}
    return backend, latest

# Tests


def test_iso_read_paths_matches_read_path(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        ISOBackend.read_paths() must return byte-identical content to
        calling read_path() once per path, for a multi-file batch.
    """
    backend = ISOBackend(tmp_path / "t.iso", disc_size=SMALL_DISC)
    root = backend._sessions_root
    blob_name = "a" * 64
    (root / "session_000" / "full").mkdir(parents=True)
    files = {
        "session_000/manifest.json": b'{"session": 0}',
        "session_000/enc_mode.json": b'{"mode": "none"}',
        f"session_000/full/{blob_name}": b"blob-bytes",
    }
    for rel, data in files.items():
        (root / rel).write_bytes(data)

    paths = list(files)
    batched = backend.read_paths(paths)
    singles = {p: backend.read_path(p) for p in paths}
    assert batched == singles == files


def test_disc_read_paths_matches_read_path(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        DiscBackend.read_paths() must return byte-identical content to
        calling read_path() once per path, for a multi-file batch, using
        one xorriso invocation with multiple -extract pairs.
    """
    backend = DiscBackend("/dev/sr0")
    blob_name = "b" * 64
    contents = {
        "session_000/manifest.json": b"manifest-bytes",
        "session_000/enc_mode.json": b'{"mode":"none"}',
        f"session_000/full/{blob_name}": b"blob-bytes",
    }
    xorriso_calls = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "xorriso":
            xorriso_calls.append(cmd)
            _write_extract_dests(cmd, contents)
            return _xorriso_result(0)
        return _xorriso_result(0)

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)

    paths = list(contents)
    batched = backend.read_paths(paths)
    n_batch_calls = len(xorriso_calls)
    singles = {p: backend.read_path(p) for p in paths}

    assert batched == singles == contents
    assert n_batch_calls == 1
    extract_flags = [c for c in xorriso_calls[0] if c == "-extract"]
    assert len(extract_flags) == len(paths)


def test_load_session_manifests_scans_once(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        load_session_manifests() must call backend.read_paths exactly once
        and must not call read_path. Replaces the four duplicated
        per-session .enc-then-.json probes.
    """
    files: dict[str, bytes] = {}
    for session_n in (0, 1):
        session_dir = tmp_path / f"m{session_n}"
        session_dir.mkdir()
        manifest = Manifest(
            version=1,
            session=session_n,
            timestamp=f"2026-04-23T00:00:0{session_n}Z",
            source="/src",
            label="TEST",
            based_on_session=None if session_n == 0 else session_n - 1,
            encryption={},
            entries=[],
            deleted=[],
            manifest_checksum="",
        )
        write_manifest(session_dir, manifest)
        files[f"session_{session_n:03d}/manifest.json"] = (
            session_dir / "manifest.json"
        ).read_bytes()

    backend = _FakeBackend(files)
    loaded = load_session_manifests(backend, crypto=_crypto)

    assert [m.session for m in loaded] == [0, 1]
    assert len(backend.read_paths_calls) == 1
    assert backend.read_path_calls == []
    requested = backend.read_paths_calls[0]
    assert "session_000/manifest.json" in requested
    assert "session_001/manifest.json" in requested
    assert "session_000/enc_mode.json" in requested


def _assert_one_manifest_batch(backend: _CountingISO) -> None:
    """Input: backend read history. Output: assert one manifest read batch."""
    manifest_calls = [
        call for call in backend.read_paths_calls
        if any("manifest." in p for p in call)
    ]
    assert len(manifest_calls) == 1


def test_restore_does_not_probe_max_session_separately(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        restore() must not call a separate _resolve_max_session
        extract-and-discard pass. Manifest files are loaded through one
        read_paths() batch, not by probing read_path() per session.
    """
    iso = tmp_path / "t.iso"
    backend = _CountingISO(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"one"}), 0)
    _burn(backend, _make_staging(tmp_path, 1, {"b.txt": b"two"}, based_on=0), 1)

    backend.read_paths_calls.clear()
    dest = tmp_path / "out"
    restored, failures = restore(dest, backend, _crypto)

    assert failures == 0
    assert restored == 2
    assert (dest / "a.txt").read_bytes() == b"one"
    assert (dest / "b.txt").read_bytes() == b"two"
    assert not hasattr(restore_mod, "_resolve_max_session")
    assert not hasattr(restore_mod, "_read_manifests")
    _assert_one_manifest_batch(backend)


def _make_counting_backend(args: argparse.Namespace, holder: dict) -> _CountingISO:
    """Input: CLI args and holder. Output: recorded counting ISO backend."""
    backend = _CountingISO(
        Path(args.test_iso),
        disc_size=parse_disc_size(getattr(args, "disc_size", "20mb")),
    )
    holder["backend"] = backend
    return backend


def test_post_burn_fast_verify_init_makes_zero_backend_reads(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        After init burns, fast verify must checksum the in-memory
        staging manifest and make zero additional read_path/read_paths
        calls to the backend for the session just burned.
    """
    holder: dict = {}

    def fake_make(args):
        return _make_counting_backend(args, holder)

    monkeypatch.setattr("oddarchiver.cli._make_backend", fake_make)

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(tmp_path / "t.iso"),
    )
    rc = _run_init(args)
    assert rc == 0
    assert holder["backend"].reads_after_burn == 0


def test_post_burn_fast_verify_sync_makes_zero_backend_reads(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        After sync burns, fast verify must checksum the in-memory
        staging manifest and make zero additional backend reads for
        the session just burned.
    """
    holder: dict = {}

    def fake_make(args):
        return _make_counting_backend(args, holder)

    monkeypatch.setattr("oddarchiver.cli._make_backend", fake_make)

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    iso = tmp_path / "t.iso"
    args = _make_args(command="init", source=str(source), test_iso=str(iso))
    assert _run_init(args) == 0

    (source / "a.txt").write_text("hello-changed")
    args.command = "sync"
    rc = _run_sync(args)
    assert rc == 0
    assert holder["backend"].reads_after_burn == 0


def test_verify_full_one_optical_pass_not_two(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        verify --level full must not call restore() as a black box, and
        its blob extract_paths count must match --level checksum exactly
        (one optical pass, reused by reconstruction).
    """
    backend, _latest = _multi_session_iso(tmp_path)
    restore_calls = {"n": 0}

    def spy_restore(*args, **kwargs):
        restore_calls["n"] += 1
        raise AssertionError("verify --level full must not call restore()")

    monkeypatch.setattr(restore_mod, "restore", spy_restore)

    backend.extract_paths_calls.clear()
    assert verify(backend, _crypto, level="checksum")
    checksum_blobs = _blob_batches(backend.extract_paths_calls)

    backend.extract_paths_calls.clear()
    assert verify(backend, _crypto, level="full")
    full_blobs = _blob_batches(backend.extract_paths_calls)

    assert restore_calls["n"] == 0
    assert full_blobs == checksum_blobs
    assert full_blobs, "checksum/full must read blobs from the backend"


def _assert_session_bytes(source, backend: ISOBackend, expected: dict, mode: str) -> None:
    """Input: source, backend, blob paths, mode. Output: assert matching bytes."""
    for session_n, paths in expected.items():
        root = source.open_session(session_n)
        batched = backend.read_paths(paths)
        for path, data in batched.items():
            assert (root / path).read_bytes() == data, (mode, path)


def test_session_source_open_session_matches_read_paths(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        FullImageSource and SequentialSource against ISOBackend must
        materialize the same bytes backend.read_paths() returns for
        that session's blobs.
    """
    backend, _latest = _multi_session_iso(tmp_path)
    manifests = load_session_manifests(backend, _crypto)
    expected = {}
    for m in manifests:
        for entry in m.entries:
            blob = entry.file if entry.type == "full" else entry.delta_file
            expected.setdefault(m.session, []).append(blob)

    for mode in ("sequential", "full-copy"):
        work = tmp_path / f"src_{mode}"
        source = make_session_source(backend, manifests, work, mode=mode)
        _assert_session_bytes(source, backend, expected, mode)


def test_restore_either_read_mode_matches_source(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Restore via sequential and full-copy reconstructs the same
        latest tree, byte-for-byte.
    """
    backend, latest = _multi_session_iso(tmp_path)
    trees = {}
    for mode in ("sequential", "full-copy"):
        dest = tmp_path / f"out_{mode}"
        restored, failures = restore(dest, backend, _crypto, read_mode=mode)
        assert failures == 0
        assert restored == len(latest)
        trees[mode] = {rel: (dest / rel).read_bytes() for rel in latest}
        assert trees[mode] == latest
    assert trees["sequential"] == trees["full-copy"]


def _assert_single_session_prefixes(root: Path, seen: list[set[str]]) -> None:
    """Input: scratch root and observations. Output: assert one session."""
    prefixes = set()
    if root.exists():
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(root).as_posix()
            if rel.startswith("session_"):
                prefixes.add(rel.split("/")[0])
    seen.append(prefixes)
    assert len(prefixes) <= 1, prefixes


def test_sequential_source_holds_one_session_at_a_time(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Across a multi-session restore, SequentialSource's scratch
        directory never contains blobs from more than one session.
    """
    backend, latest = _multi_session_iso(tmp_path)
    seen: list[set[str]] = []
    orig = SequentialSource.open_session

    def wrapped(self, session_n):
        root = orig(self, session_n)
        _assert_single_session_prefixes(root, seen)
        return root

    monkeypatch.setattr(SequentialSource, "open_session", wrapped)
    dest = tmp_path / "out"
    restored, failures = restore(dest, backend, _crypto, read_mode="sequential")
    assert failures == 0
    assert restored == len(latest)
    assert seen, "open_session must run"
    assert all(len(p) <= 1 for p in seen)


def test_restore_visits_sessions_ascending_never_revisits(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A restore driven by SessionSource visits sessions in strictly
        ascending order and never reopens a closed-out session.
    """
    backend, latest = _multi_session_iso(tmp_path)
    order: list[int] = []
    real_make = restore_mod.make_session_source

    def wrapping(*args, **kwargs):
        source = real_make(*args, **kwargs)
        inner = source.open_session

        def tracked(session_n):
            order.append(session_n)
            return inner(session_n)

        source.open_session = tracked
        return source

    monkeypatch.setattr(restore_mod, "make_session_source", wrapping)
    dest = tmp_path / "out"
    restored, failures = restore(dest, backend, _crypto)
    assert failures == 0
    assert restored == len(latest)
    assert order == sorted(order)
    assert len(order) == len(set(order))
    assert order == [0, 1]


def _assert_control_batch(backend: _CountingISO) -> None:
    """Input: backend read history. Output: assert shared control batch."""
    enc_calls = [
        c for c in backend.read_paths_calls
        if any(p.endswith("enc_mode.json") for p in c)
    ]
    assert len(enc_calls) == 1
    assert any("manifest.json" in p or "manifest.enc" in p for p in enc_calls[0])


def test_crypto_and_manifests_one_control_batch(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        _crypto_for_disc and load_session_manifests share one
        read_paths() of the control files. enc_mode.json is requested
        in that same batch as the manifests, not a second round trip.
    """
    backend, _latest = _multi_session_iso(tmp_path)
    backend.read_paths_calls.clear()
    crypto, manifests, raw = _crypto_and_manifests(backend)
    assert crypto is not None
    assert [m.session for m in manifests] == [0, 1]
    _assert_control_batch(backend)
    assert any(k.endswith("enc_mode.json") or "manifest." in k for k in raw)


def test_read_mode_flag_on_restore_and_verify():
    """
    Input:  None
    Output: None
    Details:
        restore and verify accept --read-mode sequential|full-copy.
        Without a flag, all-disc restore selects a mode from free space.
        Single-disc restore and verify use sequential. Unknown modes fail.
    """
    parser = build_parser()
    restore_args = parser.parse_args(["restore", "/dest"])
    assert restore_args.read_mode is None
    restore_copy = parser.parse_args(
        ["restore", "/dest", "--read-mode", "full-copy"]
    )
    assert restore_copy.read_mode == "full-copy"
    verify_args = parser.parse_args(["verify"])
    assert verify_args.read_mode is None
    verify_copy = parser.parse_args(["verify", "--read-mode", "full-copy"])
    assert verify_copy.read_mode == "full-copy"


def _assert_origin_batches(backend: _CountingISO) -> None:
    """Input: backend reads. Output: assert ordered batched origin blobs."""
    batches = _blob_batches(backend.read_paths_calls)
    session_ids = []
    for batch in batches:
        prefixes = {p.split("/")[0] for p in batch}
        assert len(prefixes) == 1, batch
        session_ids.append(int(list(prefixes)[0].split("_")[1]))
    assert session_ids == sorted(session_ids)
    # two origin sessions, one batch each, not one per file
    assert session_ids == [0, 1], session_ids
    assert len(batches[session_ids.index(1)]) == 2, batches


def test_cold_cache_sync_batches_origin_session_blobs(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Cold-cache build_staging of many changed files spanning several
        origin sessions batch-extracts each origin session's blobs in
        one read_paths() and does not re-extract that session's manifest
        per changed file.
    """
    backend, _latest = _multi_session_iso(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    # Change files whose origin sessions are 0 (b.txt) and 1 (a.txt, c.txt)
    (source / "a.txt").write_bytes(b"one-changed-again")
    (source / "b.txt").write_bytes(b"two-changed")
    (source / "c.txt").write_bytes(b"three-changed")

    manifests = load_session_manifests(backend, _crypto)
    disc_state = build_disc_state(manifests)
    disc_origin = build_disc_origin(manifests)
    assert disc_origin["b.txt"] == 0
    assert disc_origin["a.txt"] == 1
    assert disc_origin["c.txt"] == 1

    backend.read_paths_calls.clear()
    cache = CacheManager(cache_dir=tmp_path / "cache")
    chunks = build_staging(
        session_n=2,
        source=source,
        disc_state=disc_state,
        disc_origin=disc_origin,
        backend=backend,
        cache=cache,
        crypto=_crypto,
        _staging_root=tmp_path / "staging",
        manifests=manifests,
    )
    try:
        manifest_calls = [
            c for c in backend.read_paths_calls
            if any("manifest." in p for p in c)
        ]
        assert manifest_calls == [], (
            "origin manifests were already loaded; prefetch must not "
            f"re-extract them, got {manifest_calls}"
        )
        _assert_origin_batches(backend)
    finally:
        import shutil
        for staging, _n, _new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def _spy_job_batches(calls: list[int], real):
    """Input: observed counts and worker. Output: recording worker spy."""
    def spy(jobs, max_workers=4):
        calls.append(len(jobs))
        return real(jobs, max_workers)

    return spy


def test_build_staging_calls_process_bytes_files_once(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        build_staging must call delta.process_bytes_files once for a
        multi-file changeset with every captured-content job in that batch.
    """
    old = b"The quick brown fox jumps over the lazy dog.\n" * 200
    new_a = b"The quick brown fox jumps over the LAZY dog.\n" * 200
    new_b = b"The quick brown fox jumps over the lazy DOG.\n" * 200
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(new_a)
    (source / "b.txt").write_bytes(new_b)

    calls: list[int] = []
    spy = _spy_job_batches(calls, delta_mod.process_bytes_files)

    monkeypatch.setattr(session_mod, "process_bytes_files", spy)

    from unittest.mock import MagicMock
    cache = MagicMock()
    cache.get_with_fallback.side_effect = lambda *a, **k: old
    backend = MagicMock()
    from oddarchiver.disc import DiscInfo
    backend.mediainfo.return_value = DiscInfo(
        session_count=1, remaining_bytes=25 * 2**30, used_bytes=0, label="TEST",
    )

    chunks = build_staging(
        session_n=1,
        source=source,
        disc_state={
            "a.txt": _sha256(old),
            "b.txt": _sha256(old),
        },
        disc_origin={"a.txt": 0, "b.txt": 0},
        backend=backend,
        cache=cache,
        crypto=_crypto,
        _staging_root=tmp_path / "staging",
    )
    try:
        assert calls == [2], calls
        assert not hasattr(session_mod, "delta_or_full")
    finally:
        import shutil
        for staging, _n, _new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_dry_run_calls_process_files_once(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        _run_dry_run must batch changed files through process_files
        once per pass, not call delta_or_full per file.
    """
    from oddarchiver.cli import _run_dry_run
    import oddarchiver.delta as delta_mod

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("hello")
    (source / "b.txt").write_text("world")
    iso = tmp_path / "t.iso"
    args = _make_args(command="init", source=str(source), test_iso=str(iso))
    assert _run_init(args) == 0
    (source / "a.txt").write_text("hello-changed")
    (source / "b.txt").write_text("world-changed")

    calls: list[int] = []
    spy = _spy_job_batches(calls, delta_mod.process_files)

    monkeypatch.setattr(delta_mod, "process_files", spy)
    args.command = "sync"
    args.dry_run = True
    rc = _run_dry_run(args, is_init=False)
    assert rc == 0
    assert calls == [2], calls
