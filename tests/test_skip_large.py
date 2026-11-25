"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for --skip-large on init and sync: a file that exceeds the
    local staging budget is left out and listed. A file that is larger
    than one disc is not skipped; staging splits it. A skipped changed
    file keeps its previous version on the archive.
"""
# Imports
import argparse
import logging
import os
from pathlib import Path

import oddarchiver.session as session_mod
from oddarchiver.cli import build_parser, dispatch
from oddarchiver.disc import ISOBackend, parse_disc_size

# Globals
DISC = "4mb"
BIG = 8 * 2**20  # bigger than a 1 MiB staging budget
MEDIUM = 100_000  # fits a 1 MiB staging budget and a 4 MB disc
logger = logging.getLogger(__name__)


# Functions


def _args(command: str, **kwargs) -> argparse.Namespace:
    """
    Input:  command - init, sync or restore
            kwargs  - overrides for the Namespace
    Output: argparse.Namespace for dispatch()
    """
    defaults = {
        "command": command,
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "TEST",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "disc_size": DISC,
        "prefill": None,
        "session": None,
        "force": False,
        "slow": False,
        "skip_large": True,
        "yubikey": False,
        "yubikey_slot": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _restore(iso: Path, dest: Path) -> None:
    """
    Input:  iso  - test ISO path
            dest - restore destination
    Output: None
    Details:
        Asserts that restore exits 0.
    """
    assert dispatch(_args("restore", test_iso=str(iso), dest=str(dest))) == 0


def _tiny_staging(monkeypatch, tmp_path, free_bytes: int = 1 * 2**20) -> None:
    """Input: pytest patcher, temp dir, fake free bytes. Output: None."""
    real_disk_usage = session_mod.shutil.disk_usage
    tiny_usage = real_disk_usage(tmp_path)._replace(free=free_bytes)
    monkeypatch.setattr(session_mod.shutil, "disk_usage", lambda path: tiny_usage)


# Tests


def test_parser_accepts_skip_large_on_init_and_sync():
    """
    Input:  None
    Output: None
    Details:
        Both burn commands must accept the flag and default it to False.
    """
    parser = build_parser()
    assert parser.parse_args(["init", "/s", "--skip-large"]).skip_large is True  # scbc ignore[is-true-false-strict] argparse.Namespace has no bool annotation
    assert parser.parse_args(["sync", "/s", "--skip-large"]).skip_large is True  # scbc ignore[is-true-false-strict] argparse.Namespace has no bool annotation
    assert parser.parse_args(["sync", "/s"]).skip_large is False  # scbc ignore[is-true-false-strict] argparse.Namespace has no bool annotation


def test_init_skips_file_bigger_than_staging_and_lists_it(tmp_path, capsys, monkeypatch):
    """
    Input:  tmp_path, capsys, monkeypatch
    Output: None
    Details:
        init exits 0, names the oversized file, and restores only the file
        that fits. The archive holds one volume.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "small.txt").write_bytes(b"small")
    (source / "big.bin").write_bytes(os.urandom(BIG))
    iso = tmp_path / "a.iso"
    _tiny_staging(monkeypatch, tmp_path)

    rc = dispatch(_args("init", source=str(source), test_iso=str(iso)))

    assert rc == 0
    assert "big.bin" in capsys.readouterr().out
    dest = tmp_path / "dest"
    _restore(iso, dest)
    assert (dest / "small.txt").read_bytes() == b"small"
    assert not (dest / "big.bin").exists()
    assert not list(tmp_path.glob("a.vol*"))


def test_sync_skipped_changed_file_keeps_old_version_and_is_not_deleted(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A file that fit at init grows past the staging budget. sync
        --skip-large must leave the old version restorable (no tombstone)
        while still burning other changes.
    """
    source = tmp_path / "src"
    source.mkdir()
    old = os.urandom(MEDIUM)
    (source / "grow.bin").write_bytes(old)
    (source / "note.txt").write_bytes(b"v1")
    iso = tmp_path / "b.iso"
    assert dispatch(_args("init", source=str(source), test_iso=str(iso))) == 0

    _tiny_staging(monkeypatch, tmp_path)
    (source / "grow.bin").write_bytes(os.urandom(BIG))
    (source / "note.txt").write_bytes(b"v2")
    rc = dispatch(_args("sync", source=str(source), test_iso=str(iso)))

    assert rc == 0
    dest = tmp_path / "dest"
    _restore(iso, dest)
    assert (dest / "grow.bin").read_bytes() == old
    assert (dest / "note.txt").read_bytes() == b"v2"


def test_sync_with_only_oversized_change_burns_nothing(tmp_path, capsys, monkeypatch):
    """
    Input:  tmp_path, capsys, monkeypatch
    Output: None
    Details:
        When every change is skipped and nothing was deleted, sync exits 0
        and adds no session.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    iso = tmp_path / "c.iso"
    assert dispatch(_args("init", source=str(source), test_iso=str(iso))) == 0

    _tiny_staging(monkeypatch, tmp_path)
    (source / "big.bin").write_bytes(os.urandom(BIG))
    capsys.readouterr()
    rc = dispatch(_args("sync", source=str(source), test_iso=str(iso)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "big.bin" in out
    assert "Nothing left to burn" in out
    backend = ISOBackend(iso, disc_size=parse_disc_size(DISC))
    assert backend.mediainfo().session_count == 1
