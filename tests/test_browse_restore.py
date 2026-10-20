"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for archive listing, selective restore --path, and browse.
    Covers the multi-session ISOBackend fixture from TODO section 24.
"""
# Imports
import argparse
import logging
from pathlib import Path

import pytest

from oddarchiver.cli import dispatch
from oddarchiver.crypto import NullCrypto
from oddarchiver.cache import CacheManager
from oddarchiver.disc import ISOBackend
from oddarchiver.restore import restore
from tests.helpers import _make_staging

# Globals
SMALL_DISC = 20 * 2**20  # 20 MiB - fast ISO builds
_crypto = NullCrypto()
logger = logging.getLogger(__name__)
_log = logger


# Functions

@pytest.fixture(autouse=True)
def isolated_cli_state(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Keep CLI cache and state files in the test directory.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache))



def _burn(backend: ISOBackend, staging: Path, session_n: int) -> None:
    """Burn a staging directory via init (session 0) or append."""
    if session_n == 0:
        backend.init(staging, label="TEST", expected_session_count=0)
    else:
        backend.append(staging, label="TEST", expected_session_count=session_n)


def _build_archive(tmp_path: Path) -> tuple[Path, ISOBackend]:
    """
    Input:  tmp_path - pytest temp directory
    Output: (iso_path, backend) with two sessions and one deletion
    Details:
        Session 0: a/b.txt, a/c.txt, d/e.log, gone.txt.
        Session 1: updates a/b.txt, adds docs/readme.txt, deletes gone.txt.
    """
    iso = tmp_path / "archive.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(
        backend,
        _make_staging(
            tmp_path,
            0,
            {
                "a/b.txt": b"bravo-0",
                "a/c.txt": b"charlie",
                "d/e.log": b"echo",
                "gone.txt": b"gone",
            },
            timestamp="2026-01-01T00:00:00Z",
        ),
        session_n=0,
    )
    _burn(
        backend,
        _make_staging(
            tmp_path,
            1,
            {
                "a/b.txt": b"bravo-1-updated",
                "docs/readme.txt": b"readme",
            },
            deleted=["gone.txt"],
            based_on=0,
            timestamp="2026-01-02T00:00:00Z",
        ),
        session_n=1,
    )
    return iso, backend


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides for the Namespace
    Output: argparse.Namespace with defaults for ls/restore/browse tests
    """
    defaults = {
        "command": "ls",
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
        "path": None,
        "paths": None,
        "show_deleted": False,
        "recover_scan": False,
        "mirror": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# Tests


def _assert_default_listing(out: str) -> None:
    """Input: default ls output. Output: live-path and metadata assertions."""
    assert "a/b.txt" in out
    assert "a/c.txt" in out
    assert "d/e.log" in out
    assert "docs/readme.txt" in out
    assert "gone.txt" not in out
    assert "15.0 B" in out
    assert "001" in out
    assert "2026-01-02T00:00:00Z" in out


def test_ls_default_excludes_deleted_show_deleted_includes(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        Default ls lists live paths with size/origin/timestamp and omits
        a path deleted in a later session. --show-deleted includes it
        marked with the removal session.
    """
    iso, _backend = _build_archive(tmp_path)

    rc = dispatch(_make_args(command="ls", test_iso=str(iso)))
    assert rc == 0
    out = capsys.readouterr().out
    _assert_default_listing(out)

    rc = dispatch(_make_args(command="ls", test_iso=str(iso), show_deleted=True))
    assert rc == 0
    shown = capsys.readouterr().out
    gone_lines = [ln for ln in shown.splitlines() if "gone.txt" in ln]
    assert gone_lines
    assert "DELETED at session 001" in gone_lines[0]


def _assert_scoped_listing(scoped: str) -> None:
    """Input: path-scoped ls output. Output: prefix-filter assertions."""
    assert "a/b.txt" in scoped
    assert "a/c.txt" in scoped
    assert "d/e.log" not in scoped
    assert "docs/readme.txt" not in scoped


def _assert_session_zero_listing(at_zero: str) -> None:
    """Input: session-zero ls output. Output: point-in-time assertions."""
    assert "a/b.txt" in at_zero
    assert "gone.txt" in at_zero
    assert "DELETED" not in at_zero
    assert "docs/readme.txt" not in at_zero
    assert "7.0 B" in at_zero
    assert "2026-01-01T00:00:00Z" in at_zero


def test_ls_path_prefix_and_session_point_in_time(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        ls --path PREFIX returns only entries under that prefix.
        ls --session N reflects archive state as of session N.
    """
    iso, _backend = _build_archive(tmp_path)

    rc = dispatch(_make_args(command="ls", test_iso=str(iso), path="a"))
    assert rc == 0
    scoped = capsys.readouterr().out
    _assert_scoped_listing(scoped)

    rc = dispatch(_make_args(command="ls", test_iso=str(iso), session=0))
    assert rc == 0
    at_zero = capsys.readouterr().out
    _assert_session_zero_listing(at_zero)

def test_legacy_ls_output_unchanged_and_no_notice(tmp_path, capsys):
    """
    Input:  Legacy ISO with two sessions
    Output: Original stdout table and no marked-disc notice
    """
    iso, _backend = _build_archive(tmp_path)
    assert dispatch(_make_args(command="ls", test_iso=str(iso))) == 0
    captured = capsys.readouterr()
    header = f"{'Path':<40} {'Size':>10} {'Origin Session':<14} {'Last Modified':<22}"
    expected = "\n".join([
        header,
        "-" * len(header),
        f"{'a/b.txt':<40} {'15.0 B':>10} 001           {'2026-01-02T00:00:00Z':<22}",
        f"{'a/c.txt':<40} {'7.0 B':>10} 000           {'2026-01-01T00:00:00Z':<22}",
        f"{'d/e.log':<40} {'4.0 B':>10} 000           {'2026-01-01T00:00:00Z':<22}",
        f"{'docs/readme.txt':<40} {'6.0 B':>10} 001           {'2026-01-02T00:00:00Z':<22}",
        "",
    ])
    assert captured.out == expected
    assert "Note: This list shows this disc only." not in captured.err



def test_restore_path_single_file(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        restore --path a/b.txt writes only that file under dest.
    """
    iso, _backend = _build_archive(tmp_path)
    dest = tmp_path / "dest_one"

    rc = dispatch(_make_args(
        command="restore",
        dest=str(dest),
        test_iso=str(iso),
        paths=["a/b.txt"],
    ))
    assert rc == 0
    assert (dest / "a" / "b.txt").read_bytes() == b"bravo-1-updated"
    assert not (dest / "a" / "c.txt").exists()
    assert not (dest / "d" / "e.log").exists()
    assert not (dest / "docs" / "readme.txt").exists()
    assert not (dest / "gone.txt").exists()


def test_restore_path_file_and_directory_prefix(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        --path a/b.txt --path d/ restores that combined set in one
        invocation. a/c.txt (sibling of a/b.txt) is not leaked in.
    """
    iso, _backend = _build_archive(tmp_path)
    dest = tmp_path / "dest_combo"

    rc = dispatch(_make_args(
        command="restore",
        dest=str(dest),
        test_iso=str(iso),
        paths=["a/b.txt", "d/"],
    ))
    assert rc == 0
    assert (dest / "a" / "b.txt").read_bytes() == b"bravo-1-updated"
    assert (dest / "d" / "e.log").read_bytes() == b"echo"
    assert not (dest / "a" / "c.txt").exists()
    assert not (dest / "docs" / "readme.txt").exists()


def test_restore_path_combined_with_session(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        restore --path with --session N restores the selected set as of
        session N, not the latest content.
    """
    iso, _backend = _build_archive(tmp_path)
    dest = tmp_path / "dest_s0"

    rc = dispatch(_make_args(
        command="restore",
        dest=str(dest),
        test_iso=str(iso),
        paths=["a/b.txt"],
        session=0,
    ))
    assert rc == 0
    assert (dest / "a" / "b.txt").read_bytes() == b"bravo-0"
    assert not (dest / "docs" / "readme.txt").exists()


def _assert_mixed_restore(dest: Path, output: str) -> None:
    """Input: mixed-path restore and output. Output: matched-path assertions."""
    assert "warning: no archive entry matches 'nosuch.txt' as of session 1" in output
    assert (dest / "a" / "b.txt").read_bytes() == b"bravo-1-updated"
    mixed_files = {
        str(path.relative_to(dest)): path.read_bytes()
        for path in dest.rglob("*") if path.is_file()
    }
    assert mixed_files == {"a/b.txt": b"bravo-1-updated"}


def _assert_unmatched_restore(dest: Path, output: str) -> None:
    """Input: unmatched restore and output. Output: warning and empty-tree assertions."""
    assert "warning: no archive entry matches 'nosuch.txt' as of session 1" in output
    none_files = [
        path for path in (dest.rglob("*") if dest.exists() else [])
        if path.is_file()
    ]
    assert none_files == []


def test_restore_unmatched_path_warning_and_exit(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        An unmatched --path prints the explicit warning. Exit 0 when
        another requested path matched. Exit nonzero if none matched.
        The matched file is restored as exact source bytes.
    """
    iso, _backend = _build_archive(tmp_path)

    dest_mixed = tmp_path / "dest_mixed"
    rc = dispatch(_make_args(
        command="restore",
        dest=str(dest_mixed),
        test_iso=str(iso),
        paths=["nosuch.txt", "a/b.txt"],
    ))
    assert rc == 0
    mixed_out = capsys.readouterr().out
    _assert_mixed_restore(dest_mixed, mixed_out)

    dest_none = tmp_path / "dest_none"
    rc = dispatch(_make_args(
        command="restore",
        dest=str(dest_none),
        test_iso=str(iso),
        paths=["nosuch.txt"],
    ))
    assert rc == 1
    none_out = capsys.readouterr().out
    _assert_unmatched_restore(dest_none, none_out)


def test_browse_scripted_stdin_restores_marked_set(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        Scripted navigation selects a/b.txt and d/e.log, then r restores
        exactly that set to the destination given at the prompt.
    """
    iso, _backend = _build_archive(tmp_path)
    dest = tmp_path / "browse_dest"
    answers = iter(["1", "1", "..", "2", "1", "r", str(dest)])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    captured: dict = {}
    import oddarchiver.restore as restore_mod
    real_restore = restore_mod.restore

    def _spy(*args, **kwargs):
        captured["paths"] = kwargs.get("paths")
        captured["dest"] = kwargs.get("dest", args[0] if args else None)
        return real_restore(*args, **kwargs)

    monkeypatch.setattr(restore_mod, "restore", _spy)

    rc = dispatch(_make_args(command="browse", test_iso=str(iso)))
    assert rc == 0
    assert captured.get("paths") == ["a/b.txt", "d/e.log"]
    assert (dest / "a" / "b.txt").read_bytes() == b"bravo-1-updated"
    assert (dest / "d" / "e.log").read_bytes() == b"echo"
    assert not (dest / "a" / "c.txt").exists()
    assert not (dest / "docs" / "readme.txt").exists()


def _assert_full_tree_bytes(dest: Path) -> None:
    """Input: restored destination. Output: exact full-tree byte assertions."""
    assert (dest / "a" / "b.txt").read_bytes() == b"bravo-1-updated"
    assert (dest / "a" / "c.txt").read_bytes() == b"charlie"
    assert (dest / "d" / "e.log").read_bytes() == b"echo"
    assert (dest / "docs" / "readme.txt").read_bytes() == b"readme"
    assert not (dest / "gone.txt").exists()
    live = {
        str(path.relative_to(dest)): path.read_bytes()
        for path in dest.rglob("*") if path.is_file()
    }
    assert live == {
        "a/b.txt": b"bravo-1-updated",
        "a/c.txt": b"charlie",
        "d/e.log": b"echo",
        "docs/readme.txt": b"readme",
    }


def test_restore_paths_none_keeps_full_tree(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        paths=None keeps full-tree restore. Deleted gone.txt stays absent.
        Every restored file matches the source bytes.
    """
    iso, backend = _build_archive(tmp_path)
    dest = tmp_path / "dest_full"
    restored, failures = restore(dest, backend, _crypto)
    assert failures == 0
    assert restored == 4
    _assert_full_tree_bytes(dest)
