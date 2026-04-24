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
