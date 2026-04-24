"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for Task 18: Multi-Drive Redundancy.
    Covers --mirror flag on init and sync, mirror burn failure behaviour,
    and mirror health display in status.
"""
# Imports
import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from oddarchiver.cli import dispatch
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import read_manifest

# Globals
SMALL_DISC = 50 * 2**20  # 50 MiB


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs — field overrides
    Output: argparse.Namespace with sensible defaults
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
        "mirror": None,
        "session": None,
        "force": False,
        "level": "fast",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# Tests


def test_init_mirror_writes_session0_to_both_backends(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        init --mirror must burn session 0 to both primary and mirror ISOBackends.
        Verified by checking that both ISOs have session_000/manifest.json.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"data")

    iso = tmp_path / "primary.iso"
    mirror_iso = tmp_path / "mirror.iso"

    args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        mirror=str(mirror_iso),
    )
    rc = dispatch(args)

    assert rc == 0
    primary = ISOBackend(iso, disc_size=SMALL_DISC)
    mirror = ISOBackend(mirror_iso, disc_size=SMALL_DISC)
    assert primary.mediainfo().session_count == 1
    assert mirror.mediainfo().session_count == 1

    primary_manifest = json.loads(primary.read_path("session_000/manifest.json"))
    mirror_manifest = json.loads(mirror.read_path("session_000/manifest.json"))
    assert primary_manifest["session"] == 0
    assert mirror_manifest["session"] == 0
    assert str(iso) in primary_manifest["drives"]
    assert str(mirror_iso) in primary_manifest["drives"]


def test_sync_mirror_writes_session_to_both_backends(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        After init with --mirror, sync with --mirror must burn session 1 to both
        primary and mirror ISOBackends.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"v1")

    iso = tmp_path / "primary.iso"
    mirror_iso = tmp_path / "mirror.iso"

    init_args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        mirror=str(mirror_iso),
    )
    assert dispatch(init_args) == 0

    (source / "a.txt").write_bytes(b"v2 changed content")

    sync_args = _make_args(
        command="sync",
        source=str(source),
        test_iso=str(iso),
        mirror=str(mirror_iso),
    )
    rc = dispatch(sync_args)

    assert rc == 0
    primary = ISOBackend(iso, disc_size=SMALL_DISC)
    mirror = ISOBackend(mirror_iso, disc_size=SMALL_DISC)
    assert primary.mediainfo().session_count == 2
    assert mirror.mediainfo().session_count == 2


def test_mirror_burn_failure_logs_error_and_exits_1(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        If the primary burn succeeds but mirror raises RuntimeError, init must
        exit 1 and log an ERROR. The command must not silently succeed.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"data")

    iso = tmp_path / "primary.iso"
    mirror_iso = tmp_path / "mirror.iso"

    args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        mirror=str(mirror_iso),
    )

    original_init = ISOBackend.init

    call_count = {"n": 0}

    def _failing_mirror_init(self, staging, label, expected_session_count=0):
        if str(self.iso_path) == str(mirror_iso):
            raise RuntimeError("simulated mirror burn failure")
        return original_init(self, staging, label, expected_session_count)

    with patch.object(ISOBackend, "init", _failing_mirror_init):
        rc = dispatch(args)

    assert rc == 1
    # Primary ISO was written; mirror must not have a sessions directory
    primary = ISOBackend(iso, disc_size=SMALL_DISC)
    assert primary.mediainfo().session_count == 1
    mirror = ISOBackend(mirror_iso, disc_size=SMALL_DISC)
    assert mirror.mediainfo().session_count == 0


def test_status_shows_mirror_ok_when_mirror_exists(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        After init --mirror, status must show mirror health section with OK
        for the mirror drive that exists.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"data")

    iso = tmp_path / "primary.iso"
    mirror_iso = tmp_path / "mirror.iso"

    assert dispatch(_make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        mirror=str(mirror_iso),
    )) == 0

    capsys.readouterr()
    rc = dispatch(_make_args(command="status", test_iso=str(iso)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Mirror health" in out
    assert "OK" in out
    assert str(mirror_iso) in out


def test_status_shows_mirror_missing_when_mirror_deleted(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        After init --mirror, if the mirror ISO is deleted, status must report
        the mirror drive as MISSING.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"data")

    iso = tmp_path / "primary.iso"
    mirror_iso = tmp_path / "mirror.iso"

    assert dispatch(_make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        mirror=str(mirror_iso),
    )) == 0

    # Delete mirror ISO and its sessions directory to simulate missing mirror
    mirror_iso.unlink(missing_ok=True)
    mirror_sessions = mirror_iso.with_suffix(".d")
    if mirror_sessions.exists():
        import shutil
        shutil.rmtree(mirror_sessions)

    capsys.readouterr()
    rc = dispatch(_make_args(command="status", test_iso=str(iso)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Mirror health" in out
    assert "MISSING" in out


if __name__ == "__main__":
    pass
