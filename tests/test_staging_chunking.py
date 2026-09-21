"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for TODO 27: automatic staging-space chunking. When the local
    staging filesystem is smaller than the changeset being archived,
    session.partition_by_staging_budget and cli._mask_diff_to_group let
    init/sync split the changeset into automatic sequential sessions on
    one volume, with no user interaction and no SystemExit.
"""
# Imports
import argparse
import json
from pathlib import Path

import pytest

import oddarchiver.session as session_mod
from oddarchiver.cli import _run_init, _run_sync, dispatch
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.restore import restore

# Globals
SMALL_DISC = 50 * 2**20  # 50 MiB -- plenty of disc room; only staging is tiny.


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """Return an argparse.Namespace with defaults for init/sync commands."""
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
        "mirror": None,
        "recover_scan": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _shrink_staging_budget(monkeypatch, tmp_path, free_bytes: int) -> None:
    """
    Input:  monkeypatch, tmp_path, free_bytes - simulated free staging bytes
    Output: None
    Details:
        Matches tests/test_session.py's own staging-space-check convention
        (test_staging_space_check_exits_1_before_any_blob_written): patch
        session.shutil.disk_usage to report a fixed, tiny free value
        regardless of path, so partition_by_staging_budget and
        build_staging's own safety-net check see the same tiny budget.
    """
    real_disk_usage = session_mod.shutil.disk_usage
    tiny_usage = real_disk_usage(tmp_path)._replace(free=free_bytes)
    monkeypatch.setattr(session_mod.shutil, "disk_usage", lambda path: tiny_usage)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Return {relative path: contents} for every file under root."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }


def _manifest_deleted(iso: Path, session_n: int) -> list[str]:
    """Return the `deleted` list from session_{session_n:03d}/manifest.json."""
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    data = json.loads(backend.read_path(f"session_{session_n:03d}/manifest.json"))
    return data["deleted"]


# Tests


def test_chunked_init_produces_multiple_sessions_and_restores_exactly(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Four 20 KB files against a staging budget that holds about two of
        them force init to split into 2+ automatic sessions with no
        SystemExit and no user interaction. A subsequent restore()
        reconstructs the source tree byte-for-byte.
    """
    source = tmp_path / "src"
    source.mkdir()
    for i in range(4):
        (source / f"n{i}.bin").write_bytes(bytes([i]) * 20_000)

    _shrink_staging_budget(monkeypatch, tmp_path, free_bytes=60_000)

    iso = tmp_path / "test.iso"
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))
    assert rc == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count >= 2

    dest = tmp_path / "dest"
    result = restore(dest, backend, NullCrypto())
    assert not result.failures
    assert _tree_bytes(dest) == _tree_bytes(source)


def test_chunked_sync_against_initialized_archive_produces_correct_sessions(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        init with one small file that fits comfortably, then sync adds
        four new 20 KB files whose combined size alone exceeds a shrunk
        staging budget. sync must automatically split into 2+ additional
        sessions and a final restore must reconstruct the whole tree.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    _shrink_staging_budget(monkeypatch, tmp_path, free_bytes=60_000)
    for i in range(4):
        (source / f"n{i}.bin").write_bytes(bytes([i + 1]) * 20_000)

    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))
    assert rc == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count >= 3  # session 0 plus 2+ automatic passes

    dest = tmp_path / "dest"
    result = restore(dest, backend, NullCrypto())
    assert not result.failures
    assert _tree_bytes(dest) == _tree_bytes(source)


def test_single_oversized_file_raises_via_init_and_burns_nothing(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A single file bigger than the entire staging budget on its own
        raises RuntimeError naming that file, before any staging directory
        or burn is attempted. _run_init is called directly (bypassing
        dispatch's generic exception-to-exit-1 handling) so the exception
        itself can be asserted, matching test_volume_staging.py's
        oversized-single-file convention.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "huge.bin").write_bytes(b"H" * 20_000)

    _shrink_staging_budget(monkeypatch, tmp_path, free_bytes=10_000)

    iso = tmp_path / "test.iso"
    args = _make_args(command="init", source=str(source), test_iso=str(iso))
    with pytest.raises(RuntimeError, match="huge.bin"):
        _run_init(args)

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 0


def test_single_oversized_file_raises_via_sync_and_burns_nothing_new(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Same oversized-file guarantee, reached through sync against an
        already-initialized archive. No additional session is written.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    _shrink_staging_budget(monkeypatch, tmp_path, free_bytes=10_000)
    (source / "huge2.bin").write_bytes(b"H" * 20_000)

    args = _make_args(command="sync", source=str(source), test_iso=str(iso))
    with pytest.raises(RuntimeError, match="huge2.bin"):
        _run_sync(args)

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 1


def test_changeset_fitting_one_pass_is_unchanged_from_today(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        Regression coverage for the common, non-chunked case: with no
        staging-budget pressure, init produces exactly one session and
        prints no "Splitting into N automatic passes" notice.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    (source / "b.txt").write_bytes(b"world")

    iso = tmp_path / "test.iso"
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))
    assert rc == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 1

    out = capsys.readouterr().out
    assert "automatic passes" not in out


def test_chunked_sync_deletion_applies_exactly_once(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A chunked sync (2+ automatic passes) that also includes a deletion
        must record that deletion in exactly one pass's manifest, not
        duplicated across passes and not silently dropped. restore()
        confirms the deleted file is gone and every surviving file is
        correct.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"stays")
    (source / "del.bin").write_bytes(b"D" * 500)
    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    _shrink_staging_budget(monkeypatch, tmp_path, free_bytes=60_000)
    (source / "del.bin").unlink()
    for i in range(4):
        (source / f"n{i}.bin").write_bytes(bytes([i + 1]) * 20_000)

    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))
    assert rc == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    session_count = backend.mediainfo().session_count
    assert session_count >= 3  # session 0 plus 2+ automatic passes

    deleted_counts = [
        len(_manifest_deleted(iso, s)) for s in range(1, session_count)
    ]
    assert sum(deleted_counts) == 1, (
        f"del.bin must be recorded as deleted exactly once, saw {deleted_counts}"
    )
    sessions_with_deletion = [
        s for s in range(1, session_count)
        if "del.bin" in _manifest_deleted(iso, s)
    ]
    assert sessions_with_deletion == [1]

    dest = tmp_path / "dest"
    result = restore(dest, backend, NullCrypto())
    assert not result.failures
    assert not (dest / "del.bin").exists()
    assert _tree_bytes(dest) == _tree_bytes(source)


def test_followup_sync_after_chunked_init_reports_up_to_date(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        After a fully chunked init (2+ automatic sessions), a follow-up
        plain sync with no further source changes must report "All files
        up to date." -- proving the cache/disc-state bookkeeping across
        automatic passes leaves no file looking stale or double-counted.
    """
    source = tmp_path / "src"
    source.mkdir()
    for i in range(4):
        (source / f"n{i}.bin").write_bytes(bytes([i]) * 20_000)

    _shrink_staging_budget(monkeypatch, tmp_path, free_bytes=60_000)

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    session_count_after_init = backend.mediainfo().session_count
    assert session_count_after_init >= 2

    capsys.readouterr()  # clear init output
    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))
    assert rc == 0

    out = capsys.readouterr().out
    assert "All files up to date." in out
    assert backend.mediainfo().session_count == session_count_after_init


if __name__ == "__main__":
    pass
