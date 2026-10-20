"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for the atomic session publish of ISOBackend (_copy_staging).
    A copy that fails partway through a session must leave no
    session_NNN directory, must fail the command, and must not change
    prior sessions. A plain retry must then publish a complete session.
    Temp items that a hard kill leaves behind must not count as
    sessions, and the next write must remove them.
"""
# Imports
import argparse
import errno
import logging
import shutil
from pathlib import Path

import pytest

from oddarchiver import disc as disc_mod
from oddarchiver.cli import dispatch
from oddarchiver.disc import ISOBackend, archive_session_count

# Globals
logger = logging.getLogger(__name__)
SMALL_DISC = 100 * 2**20
_REAL_COPYFILE = shutil.copyfile


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides
    Output: argparse.Namespace with test-safe defaults
    """
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "ATOMIC",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "100mb",
        "prefill": None,
        "session": None,
        "force": True,
        "level": "fast",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class _FailingBlobCopy:
    """
    Input:  sessions_root - the .d directory of the test ISO
    Output: N/A (callable that replaces shutil.copyfile)
    Details:
        While armed, the first copy of a session blob (full/ or deltas/)
        into sessions_root writes half of the bytes, then raises OSError.
        It fires once. copytree then copies the other files and raises
        shutil.Error at the end. Other copies use the real shutil.copyfile.
    """

    def __init__(self, sessions_root: Path) -> None:
        self.sessions_root = sessions_root
        self.armed = True
        self.fired = 0

    def __call__(self, src, dst, *, follow_symlinks=True):
        dst_path = Path(dst)
        if (
            self.armed
            and self.fired == 0
            and self.sessions_root in dst_path.parents
            and Path(src).parent.name in ("full", "deltas")
        ):
            data = Path(src).read_bytes()
            dst_path.write_bytes(data[: len(data) // 2])
            self.fired += 1
            raise OSError(errno.EIO, "simulated I/O error during session copy")
        return _REAL_COPYFILE(src, dst, follow_symlinks=follow_symlinks)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """
    Input:  root - directory
    Output: map of relative path to file bytes for every file under root
    """
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _session_dirs(sessions_root: Path) -> list[str]:
    """
    Input:  sessions_root - the .d directory of the test ISO
    Output: sorted names of the session_NNN directories
    """
    if not sessions_root.exists():
        return []
    return sorted(
        p.name for p in sessions_root.iterdir()
        if p.is_dir() and p.name.startswith("session_")
    )


def _restore_matches(tmp_path: Path, iso: Path, source: Path, name: str) -> None:
    """
    Input:  tmp_path - pytest temp directory
            iso      - test ISO path
            source   - source tree to compare against
            name     - name of the restore directory
    Output: None
    Details: Restores the newest state and asserts byte equality with source.
    """
    dest = tmp_path / name
    rc = dispatch(_make_args(command="restore", dest=str(dest), test_iso=str(iso)))
    assert rc == 0
    assert _tree_bytes(dest) == _tree_bytes(source)


def _status_sessions(iso: Path, capsys) -> int:
    """
    Input:  iso    - test ISO path
            capsys - pytest capsys fixture
    Output: session count that the status command prints
    """
    capsys.readouterr()
    rc = dispatch(_make_args(command="status", test_iso=str(iso)))
    assert rc == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("Sessions:"))
    return int(line.split(":", 1)[1])


# Tests


def test_interrupted_init_publishes_no_session_and_retry_succeeds(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        init fails partway through the session_000 blob copy. The command
        must fail, no session_000 may exist, and the session count stays
        0. A plain retry must then succeed and restore byte-identically.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"alpha " * 2000)
    (source / "b.bin").write_bytes(bytes(range(256)) * 64)
    iso = tmp_path / "archive.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    failing = _FailingBlobCopy(backend._sessions_root)
    monkeypatch.setattr(shutil, "copyfile", failing)

    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))

    assert failing.fired == 1
    assert rc != 0
    assert _session_dirs(backend._sessions_root) == []
    assert ISOBackend(iso, disc_size=SMALL_DISC).mediainfo().session_count == 0
    assert list((backend._sessions_root).glob(".*partial-*")) == []

    failing.armed = False
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))

    assert rc == 0
    assert _session_dirs(backend._sessions_root) == ["session_000"]
    assert _status_sessions(iso, capsys) == 1
    _restore_matches(tmp_path, iso, source, "restored")


def _assert_failed_sync_keeps_prior_session(
    iso: Path, backend: ISOBackend, session0: Path, before: dict[str, bytes],
    capsys, failing: _FailingBlobCopy, rc: int,
) -> None:
    """
    Input:  iso, backend, session0, before, capsys, failing, rc - failed sync state
    Output: None
    """
    assert failing.fired == 1
    assert rc != 0
    assert _session_dirs(backend._sessions_root) == ["session_000"]
    fresh = ISOBackend(iso, disc_size=SMALL_DISC)
    assert fresh.mediainfo().session_count == 1
    assert archive_session_count(fresh) == 1
    assert _status_sessions(iso, capsys) == 1
    assert _tree_bytes(session0) == before
    assert list((backend._sessions_root).glob(".*partial-*")) == []


def _assert_successful_sync_publishes_session(
    tmp_path: Path, iso: Path, source: Path, backend: ISOBackend,
    session0: Path, before: dict[str, bytes], capsys, rc: int,
) -> None:
    """
    Input:  tmp_path, iso, source, backend, session0, before, capsys, rc - retry state
    Output: None
    """
    assert rc == 0
    assert _session_dirs(backend._sessions_root) == ["session_000", "session_001"]
    assert _status_sessions(iso, capsys) == 2
    assert _tree_bytes(session0) == before
    _restore_matches(tmp_path, iso, source, "restored")


def test_interrupted_sync_publishes_no_session_and_retry_succeeds(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        After a good init, sync fails partway through the session_001
        blob copy. The command must fail, no session_001 may exist, the
        session count stays 1, and session_000 stays byte-identical. A
        plain retry must then succeed and restore byte-identically.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"alpha " * 2000)
    iso = tmp_path / "archive.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    session0 = backend._sessions_root / "session_000"
    before = _tree_bytes(session0)
    assert before

    (source / "c.bin").write_bytes(bytes(range(256)) * 64)
    (source / "a.txt").write_bytes(b"alpha changed " * 2000)
    failing = _FailingBlobCopy(backend._sessions_root)
    monkeypatch.setattr(shutil, "copyfile", failing)

    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))

    _assert_failed_sync_keeps_prior_session(
        iso, backend, session0, before, capsys, failing, rc,
    )

    failing.armed = False
    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))

    _assert_successful_sync_publishes_session(
        tmp_path, iso, source, backend, session0, before, capsys, rc,
    )


def _assert_stale_session_is_ignored(iso: Path, blob: Path, capsys) -> None:
    """
    Input:  iso, blob, capsys - archive with a stale partial session
    Output: None
    """
    fresh = ISOBackend(iso, disc_size=SMALL_DISC)
    assert fresh.mediainfo().session_count == 1
    assert fresh.list_session_numbers() == [0]
    assert archive_session_count(fresh) == 1
    assert _status_sessions(iso, capsys) == 1
    with pytest.raises(ValueError):
        fresh.read_path(f".session_001.partial-k1ll3d_x/full/{blob.name}")


def _assert_stale_items_removed_after_sync(
    tmp_path: Path, iso: Path, source: Path, root: Path, session0: Path,
    before: dict[str, bytes], stale: Path, stale_file: Path, rc: int,
) -> None:
    """
    Input:  tmp_path, iso, source, root, session0, before, stale, stale_file, rc
            - state after sync
    Output: None
    """
    assert rc == 0
    assert not stale.exists()
    assert not stale_file.exists()
    assert _session_dirs(root) == ["session_000", "session_001"]
    assert _tree_bytes(session0) == before
    _restore_matches(tmp_path, iso, source, "restored")


def test_stale_partial_items_are_ignored_and_removed_on_next_write(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        Simulates a SIGKILL during publish: a ".session_001.partial-*"
        directory with a manifest and a truncated blob, plus a partial
        root file. Session counting and reads must ignore them. The next
        sync must remove them and publish a complete session_001.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"alpha " * 2000)
    iso = tmp_path / "archive.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    root = backend._sessions_root
    session0 = root / "session_000"
    before = _tree_bytes(session0)

    stale = root / ".session_001.partial-k1ll3d_x"
    (stale / "full").mkdir(parents=True)
    shutil.copy2(session0 / "manifest.json", stale / "manifest.json")
    blob = next((session0 / "full").iterdir())
    (stale / "full" / blob.name).write_bytes(blob.read_bytes()[:3])
    stale_file = root / ".volume.json.partial-k1ll3d_y"
    stale_file.write_bytes(b"{")

    _assert_stale_session_is_ignored(iso, blob, capsys)

    (source / "c.bin").write_bytes(bytes(range(256)) * 64)
    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))

    _assert_stale_items_removed_after_sync(
        tmp_path, iso, source, root, session0, before, stale, stale_file, rc,
    )


def test_killed_swap_restores_prior_tree(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Simulates a SIGKILL between the two renames of a re-publish: the
        old tree sits at ".<name>.prior-*" and <name> is missing. The
        next write recovery moves the old tree back and removes the
        partial tree.
    """
    iso = tmp_path / "archive.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    root = backend._sessions_root
    prior = root / ".oddarchiver-software.prior-abc_12"
    prior.mkdir(parents=True)
    (prior / "VERSION").write_text("1.0\nminimal\n")
    partial = root / ".oddarchiver-software.partial-def_34"
    partial.mkdir()
    (partial / "VERSION").write_text("2.0\nminimal\n")

    backend._recover_interrupted_publish()

    assert (root / "oddarchiver-software" / "VERSION").read_text() == "1.0\nminimal\n"
    assert not prior.exists()
    assert not partial.exists()


def _assert_tree_and_no_publish_temps(
    existing: Path, dest_root: Path, expected: dict[str, bytes],
) -> None:
    """
    Input:  existing, dest_root, expected - published tree and expected bytes
    Output: None
    """
    assert _tree_bytes(existing) == expected
    assert sorted(p.name for p in dest_root.iterdir() if p.name.startswith(".")) == []


def test_failed_republish_keeps_existing_tree(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A staging directory whose name already exists under dest_root
        merges into the existing tree. The staging tree has two files
        and the copy of README fails. The existing tree must stay
        unchanged (no new VERSION) and no temp items may remain. A good
        copy must give the merged result.
    """
    dest_root = tmp_path / "root"
    existing = dest_root / "bundle"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_bytes(b"old only")
    (existing / "VERSION").write_bytes(b"old")
    staging = tmp_path / "staging"
    (staging / "bundle").mkdir(parents=True)
    (staging / "bundle" / "VERSION").write_bytes(b"new")
    (staging / "bundle" / "README").write_bytes(b"readme")
    (staging / "volume.json").write_bytes(b"{}")

    def failing(src, dst, *, follow_symlinks=True):
        if Path(src) == staging / "bundle" / "README":
            raise OSError(errno.EIO, "simulated I/O error during bundle copy")
        return _REAL_COPYFILE(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copyfile", failing)
    with pytest.raises(OSError):
        disc_mod._copy_staging(staging, dest_root)

    _assert_tree_and_no_publish_temps(
        existing, dest_root, {"VERSION": b"old", "keep.txt": b"old only"},
    )

    monkeypatch.setattr(shutil, "copyfile", _REAL_COPYFILE)
    disc_mod._copy_staging(staging, dest_root)

    assert _tree_bytes(existing) == {
        "README": b"readme", "VERSION": b"new", "keep.txt": b"old only",
    }
    assert (dest_root / "volume.json").read_bytes() == b"{}"
    assert sorted(p.name for p in dest_root.iterdir() if p.name.startswith(".")) == []
