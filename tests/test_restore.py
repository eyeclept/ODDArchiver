"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for oddarchiver.restore - full restore, point-in-time, non-destructive,
    force overwrite, and checksum-mismatch handling.
"""
# Imports
import hashlib
import logging
import os
import time
from pathlib import Path

import pytest

from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.restore import restore
from tests.helpers import _make_staging

# Globals
SMALL_DISC = 20 * 2**20  # 20 MiB - fast ISO builds
_crypto = NullCrypto()
logger = logging.getLogger(__name__)


# Functions


def _blob_id(session_n: int, rel_path: str) -> str:
    """Input: session and path. Output: a legacy blob name for this fixture."""
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _burn(backend: ISOBackend, staging: Path, session_n: int) -> None:
    """Burn a staging directory via init (session 0) or append."""
    if session_n == 0:
        backend.init(staging, label="TEST", expected_session_count=0)
    else:
        backend.append(staging, label="TEST", expected_session_count=session_n)


# Tests


def test_full_restore_matches_source(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Single session with two files. Restore must reproduce exact bytes.
    """
    files = {"a.txt": b"hello archiver", "sub/b.bin": b"\x00\xff\xde\xad"}
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, files), session_n=0)

    dest = tmp_path / "dest"
    restored, failures = restore(dest, backend, _crypto)

    assert failures == 0
    assert restored == 2
    assert (dest / "a.txt").read_bytes() == b"hello archiver"
    assert (dest / "sub" / "b.bin").read_bytes() == b"\x00\xff\xde\xad"


def test_session_arg_stops_at_s(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Session 0 has a.txt. Session 1 adds c.txt.
        restore(session=0) must not include c.txt.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"alpha"}), session_n=0)
    _burn(backend, _make_staging(tmp_path, 1, {"c.txt": b"charlie"}, based_on=0), session_n=1)

    dest = tmp_path / "dest"
    restored, failures = restore(dest, backend, _crypto, session=0)

    assert failures == 0
    assert (dest / "a.txt").exists()
    assert not (dest / "c.txt").exists()


def test_non_destructive_skips_matching_file(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Pre-populate dest/a.txt with the correct content.
        restore() must not rewrite it (assert mtime is unchanged).
    """
    content = b"stable content"
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": content}), session_n=0)

    dest = tmp_path / "dest"
    dest.mkdir()
    dest_file = dest / "a.txt"
    dest_file.write_bytes(content)
    mtime_before = dest_file.stat().st_mtime_ns

    time.sleep(0.01)
    restore(dest, backend, _crypto, force=False)

    assert dest_file.stat().st_mtime_ns == mtime_before


def test_force_overwrites_matching_file(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Pre-populate dest/a.txt with correct content.
        restore(force=True) must rewrite the file (mtime changes).
    """
    content = b"rewrite me"
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": content}), session_n=0)

    dest = tmp_path / "dest"
    dest.mkdir()
    dest_file = dest / "a.txt"
    dest_file.write_bytes(content)
    mtime_before = dest_file.stat().st_mtime_ns

    time.sleep(0.01)
    restore(dest, backend, _crypto, force=True)

    assert dest_file.stat().st_mtime_ns != mtime_before
    assert dest_file.read_bytes() == content


def test_checksum_mismatch_logs_error_no_corrupt_file(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        Burn a session normally, then corrupt the stored blob.
        restore() must log ERROR and not write the corrupt file.
    """
    import logging
    content = b"original content"
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": content}), session_n=0)

    # corrupt the blob on the sessions_root
    blob_path = backend._sessions_root / "session_000" / "full" / _blob_id(0, "a.txt")
    blob_path.write_bytes(b"CORRUPTED GARBAGE")

    dest = tmp_path / "dest"
    with caplog.at_level(logging.ERROR, logger="oddarchiver.restore"):
        restored, failures = restore(dest, backend, _crypto)

    assert failures == 1
    assert restored == 0
    assert not (dest / "a.txt").exists()
    assert any("Checksum mismatch" in r.message for r in caplog.records)


def test_suspect_manifest_in_range_counts_as_failure(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        A suspect manifest inside the requested session range must make
        restore report a failure. It must not silently skip its files.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"alpha"}), session_n=0)
    _burn(backend, _make_staging(tmp_path, 1, {"b.txt": b"bravo"}, based_on=0), session_n=1)

    manifest_path = backend._sessions_root / "session_001" / "manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b"tamper")

    dest = tmp_path / "dest"
    with caplog.at_level(logging.ERROR, logger="oddarchiver.restore"):
        restored, failures = restore(dest, backend, _crypto)

    assert failures >= 1
    assert not (dest / "b.txt").exists()
    assert (dest / "a.txt").read_bytes() == b"alpha"
    assert any("session 1" in r.message for r in caplog.records)


def test_deleted_files_not_restored(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Session 0 has a.txt and b.txt. Session 1 deletes b.txt.
        Full restore must not include b.txt.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"alpha", "b.txt": b"beta"}), session_n=0)
    _burn(backend, _make_staging(tmp_path, 1, {}, deleted=["b.txt"], based_on=0), session_n=1)

    dest = tmp_path / "dest"
    restore(dest, backend, _crypto)

    assert (dest / "a.txt").exists()
    assert not (dest / "b.txt").exists()
