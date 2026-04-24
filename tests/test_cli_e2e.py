"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    End-to-end tests for CLI dispatch: init, sync, history, status commands
    exercised against ISOBackend (no physical disc required).
"""
# Imports
import argparse
import hashlib
import json
from pathlib import Path

import pytest

from oddarchiver.cli import dispatch
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import read_manifest

# Globals
SMALL_DISC = 50 * 2**20  # 50 MiB — room for two sessions


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs — field overrides for the Namespace
    Output: argparse.Namespace with sensible defaults for test commands
    Details:
        Provides defaults so callers only specify what differs.
    """
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
        "disc_size": "50mb",
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# Tests


def test_init_exits_0_and_produces_session0_manifest(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        init against a fresh ISOBackend must exit 0 and write session_000/manifest.json.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")

    iso = tmp_path / "test.iso"
    args = _make_args(command="init", source=str(source), test_iso=str(iso))

    rc = dispatch(args)

    assert rc == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    manifest_bytes = backend.read_path("session_000/manifest.json")
    manifest_data = json.loads(manifest_bytes)
    assert manifest_data["session"] == 0
    assert manifest_data["label"] == "TEST"
    assert len(manifest_data["entries"]) == 1


def test_sync_no_changes_exits_0_no_second_burn(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        sync with identical source after init must exit 0 and leave session count at 1.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"stable")

    iso = tmp_path / "test.iso"
    init_args = _make_args(command="init", source=str(source), test_iso=str(iso))
    assert dispatch(init_args) == 0

    sync_args = _make_args(command="sync", source=str(source), test_iso=str(iso))
    rc = dispatch(sync_args)

    assert rc == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 1


def test_sync_changed_file_exits_0_and_produces_session1_manifest(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        sync after modifying a file must exit 0 and produce session_001/manifest.json.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"version 1")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "a.txt").write_bytes(b"version 2 - different content")

    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))

    assert rc == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 2
    manifest_bytes = backend.read_path("session_001/manifest.json")
    manifest_data = json.loads(manifest_bytes)
    assert manifest_data["session"] == 1


def test_history_prints_one_row_per_session(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        After init + sync, history must print two rows with correct timestamps.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"v1")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "b.txt").write_bytes(b"new file")
    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0

    capsys.readouterr()  # clear init/sync output
    rc = dispatch(_make_args(command="history", test_iso=str(iso)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "000" in out
    assert "001" in out


def test_sync_deletion_only_writes_session_and_restore_has_no_ghost(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        B4 regression: init with file1.txt + file2.txt, delete file1.txt,
        sync must detect the deletion and write session 1.
        Subsequent restore must not contain file1.txt.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "file1.txt").write_bytes(b"will be deleted")
    (source / "file2.txt").write_bytes(b"stays")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    # Delete file1.txt — only change is a deletion
    (source / "file1.txt").unlink()
    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))

    assert rc == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 2, "deletion-only sync must write a new session"

    # Verify the session 1 manifest records the deletion
    manifest_bytes = backend.read_path("session_001/manifest.json")
    manifest_data = json.loads(manifest_bytes)
    assert "file1.txt" in manifest_data["deleted"]

    # Restore — file1.txt must not appear
    dest = tmp_path / "dest"
    from oddarchiver.crypto import NullCrypto
    from oddarchiver.restore import restore
    restore(dest, backend, NullCrypto())
    assert not (dest / "file1.txt").exists(), "ghost file: deleted file must not appear in restore"
    assert (dest / "file2.txt").exists()


def test_status_shows_suspect_on_bad_checksum(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        After init, tamper with session 0 manifest; status must print SUSPECT.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    manifest_path = backend._sessions_root / "session_000" / "manifest.json"
    text = manifest_path.read_text()
    manifest_path.write_text(text.replace("TEST", "TAMPERED"))

    capsys.readouterr()
    rc = dispatch(_make_args(command="status", test_iso=str(iso)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "SUSPECT" in out
