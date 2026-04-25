"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Security regression tests — cover findings from Assist/security_fixes.md.
    H1: no plaintext temp files during delta operations.
    H2: path traversal rejected in restore, verify, and disc backends.
    M1: default staging root is user-private (0o700).
    M2: keyfile CLI wiring end-to-end.
"""
# Imports
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# H1 — no plaintext temp files during delta
# ---------------------------------------------------------------------------

def _snapshot_tmp_files():
    """Return sets of filenames currently in /tmp and /dev/shm."""
    try:
        shm = set(os.listdir("/dev/shm"))
    except FileNotFoundError:
        shm = set()
    return set(os.listdir("/tmp")), shm


def test_compute_delta_writes_no_plaintext_tmpfile(tmp_path):
    """compute_delta must not leave any *.odd.src file in /tmp or /dev/shm."""
    from oddarchiver.delta import compute_delta

    old_data = os.urandom(10 * 1024)
    new_file = tmp_path / "new.bin"
    new_file.write_bytes(old_data[:9000] + os.urandom(1024))

    before_tmp, before_shm = _snapshot_tmp_files()
    compute_delta(old_data, new_file)
    after_tmp, after_shm = _snapshot_tmp_files()

    new_in_tmp = after_tmp - before_tmp
    new_in_shm = after_shm - before_shm
    odd_files = {f for f in new_in_tmp | new_in_shm if ".odd." in f}
    assert not odd_files, f"Unexpected temp files: {odd_files}"


def test_apply_delta_writes_no_plaintext_tmpfile(tmp_path):
    """apply_delta must not leave any *.odd.base file in /tmp or /dev/shm."""
    from oddarchiver.delta import compute_delta, apply_delta

    old_data = os.urandom(10 * 1024)
    new_file = tmp_path / "new.bin"
    new_file.write_bytes(old_data[:9000] + os.urandom(1024))
    delta = compute_delta(old_data, new_file)

    before_tmp, before_shm = _snapshot_tmp_files()
    apply_delta(old_data, delta)
    after_tmp, after_shm = _snapshot_tmp_files()

    new_in_tmp = after_tmp - before_tmp
    new_in_shm = after_shm - before_shm
    odd_files = {f for f in new_in_tmp | new_in_shm if ".odd." in f}
    assert not odd_files, f"Unexpected temp files: {odd_files}"


# ---------------------------------------------------------------------------
# H2 — path traversal rejected
# ---------------------------------------------------------------------------

SMALL_DISC = 20 * 2**20


def _blob_id(session_n: int, rel_path: str) -> str:
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _build_traversal_iso(tmp_path: Path, entry_path: str, entry_file: str | None = None) -> object:
    """Build an ISO whose manifest contains a traversal path."""
    from oddarchiver.crypto import NullCrypto
    from oddarchiver.disc import ISOBackend
    from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest

    crypto = NullCrypto()
    content = b"innocent content"
    session_n = 0
    session_name = "session_000"

    iso = tmp_path / "traversal.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    backend._sessions_root.mkdir(parents=True, exist_ok=True)
    session_dir = backend._sessions_root / session_name
    full_dir = session_dir / "full"
    full_dir.mkdir(parents=True)

    blob_name = _blob_id(session_n, "safe.txt")
    (full_dir / blob_name).write_bytes(content)

    file_ref = entry_file if entry_file is not None else f"{session_name}/full/{blob_name}"
    entry = ManifestEntry(
        path=entry_path,
        type="full",
        result_checksum=hashlib.sha256(content).hexdigest(),
        full_size_bytes=len(content),
        file=file_ref,
    )
    manifest = Manifest(
        version=1, session=0, timestamp="2026-04-25T00:00:00Z",
        source="/src", label="TEST", based_on_session=None,
        encryption={}, entries=[entry], deleted=[], manifest_checksum="",
    )
    write_manifest(session_dir, manifest)

    from oddarchiver.disc import _copy_staging
    # Sessions root already IS the target; write a stub ISO so mediainfo works.
    import subprocess, tempfile
    staging = tmp_path / "staging"
    staging.mkdir()
    subprocess.run(
        ["genisoimage", "-udf", "-R", "-V", "TEST", "-o", str(iso),
         str(backend._sessions_root)],
        capture_output=True,
    )
    return backend


def test_restore_rejects_traversal_in_entry_path(tmp_path):
    """Manifest with entry.path='../escape' must not write outside dest."""
    from oddarchiver.crypto import NullCrypto
    from oddarchiver.restore import restore

    backend = _build_traversal_iso(tmp_path, entry_path="../escape")
    dest = tmp_path / "dest"
    dest.mkdir()
    _, failures = restore(dest, backend, NullCrypto())
    assert failures > 0
    assert not (tmp_path / "escape").exists()


def test_restore_rejects_traversal_in_entry_file(tmp_path):
    """Manifest with entry.file='../../etc/passwd' must cause restore failure."""
    from oddarchiver.crypto import NullCrypto
    from oddarchiver.restore import restore

    backend = _build_traversal_iso(
        tmp_path, entry_path="safe.txt", entry_file="../../etc/passwd"
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    _, failures = restore(dest, backend, NullCrypto())
    assert failures > 0


def test_iso_backend_read_path_rejects_dotdot(tmp_path):
    """ISOBackend.read_path with a dotdot path must raise ValueError."""
    from oddarchiver.disc import ISOBackend
    from oddarchiver.manifest import validate_disc_read_path

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    with pytest.raises(ValueError, match="Refusing"):
        backend.read_path("../etc/passwd")


def test_validate_blob_path_accepts_legitimate_values():
    """validate_blob_path must accept a correctly-formed blob path."""
    from oddarchiver.manifest import validate_blob_path

    good = "session_001/full/" + "a" * 64
    assert validate_blob_path(good) == good


# ---------------------------------------------------------------------------
# M2 — keyfile CLI wiring
# ---------------------------------------------------------------------------

def test_init_keyfile_round_trip(tmp_path):
    """init --encrypt keyfile + verify --level fast must succeed without TypeError."""
    from oddarchiver.crypto import generate_keyfile
    from oddarchiver.cli import build_parser, dispatch

    keyfile = tmp_path / "test.key"
    generate_keyfile(str(keyfile))
    iso = tmp_path / "kf_test.iso"
    source = tmp_path / "src"
    source.mkdir()
    (source / "file.txt").write_bytes(b"hello keyfile world")

    parser = build_parser()
    init_args = parser.parse_args([
        "init", str(source),
        "--test-iso", str(iso),
        "--encrypt", "keyfile",
        "--key", str(keyfile),
    ])
    result = dispatch(init_args)
    assert result == 0, "init --encrypt keyfile should exit 0"
    assert iso.exists(), "ISO should be created"

    verify_args = parser.parse_args([
        "verify",
        "--test-iso", str(iso),
        "--level", "fast",
        "--key", str(keyfile),
    ])
    result = dispatch(verify_args)
    assert result == 0, "verify --level fast on keyfile disc should exit 0"


def test_keyfile_missing_key_arg_exits_1(tmp_path):
    """verify on a keyfile-encrypted disc without --key must exit 1."""
    from oddarchiver.crypto import generate_keyfile
    from oddarchiver.cli import build_parser, dispatch

    keyfile = tmp_path / "test.key"
    generate_keyfile(str(keyfile))
    iso = tmp_path / "kf_test.iso"
    source = tmp_path / "src"
    source.mkdir()
    (source / "f.txt").write_bytes(b"data")

    parser = build_parser()
    init_args = parser.parse_args([
        "init", str(source),
        "--test-iso", str(iso),
        "--encrypt", "keyfile",
        "--key", str(keyfile),
    ])
    assert dispatch(init_args) == 0

    verify_args = parser.parse_args([
        "verify",
        "--test-iso", str(iso),
        "--level", "fast",
        # intentionally omit --key
    ])
    result = dispatch(verify_args)
    assert result == 1, "verify without --key on keyfile disc must exit 1"


# ---------------------------------------------------------------------------
# M1 — default staging root is user-private
# ---------------------------------------------------------------------------

def test_default_staging_root_is_user_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    from oddarchiver.session import _default_staging_root
    root = _default_staging_root()
    assert root.exists()
    assert (os.stat(root).st_mode & 0o777) == 0o700
    assert str(root).startswith(str(tmp_path))
