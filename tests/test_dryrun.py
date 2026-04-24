"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for --dry-run mode: no ISO written, no cache updated, correct output,
    space overage reported without exit 1.
    Tests call _run_dry_run directly because --dry-run and --test-iso are
    mutually exclusive in dispatch() by design; _run_dry_run itself accepts any
    backend so ISOBackend can serve as a read-only disc-state source in tests.
"""
# Imports
import argparse
import json

from oddarchiver.cache import DEFAULT_CACHE_DIR
from oddarchiver.cli import dispatch, _run_dry_run
from oddarchiver.disc import ISOBackend

# Globals
SMALL_DISC = 50 * 2**20  # 50 MiB


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs — field overrides
    Output: argparse.Namespace with sensible defaults for dry-run tests
    """
    defaults = {
        "command": "sync",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "TEST",
        "encrypt": "none",
        "key": None,
        "dry_run": True,
        "no_cache": False,
        "disc_size": "50mb",
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _init(source, iso) -> None:
    """Run a real (non-dry) init for test setup."""
    args = _make_args(command="init", dry_run=False, source=str(source), test_iso=str(iso))
    assert dispatch(args) == 0


# Tests


def test_sync_dry_run_exits_0_and_prints_report(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        sync --dry-run must exit 0 and print a changed-file report containing
        the changed filename, DRY RUN header, and "No disc written" footer.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"version 1")
    iso = tmp_path / "test.iso"
    _init(source, iso)

    (source / "a.txt").write_bytes(b"version 2 -- different content here")
    capsys.readouterr()

    args = _make_args(command="sync", source=str(source), test_iso=str(iso))
    rc = _run_dry_run(args, is_init=False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "a.txt" in out
    assert "No disc written" in out


def test_sync_dry_run_iso_not_modified(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        _run_dry_run must not call backend.append(); session count stays at 1.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"v1")
    iso = tmp_path / "test.iso"
    _init(source, iso)

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    count_before = backend.mediainfo().session_count

    (source / "a.txt").write_bytes(b"v2 modified content here for dry run test")
    args = _make_args(command="sync", source=str(source), test_iso=str(iso))
    _run_dry_run(args, is_init=False)

    assert backend.mediainfo().session_count == count_before


def test_sync_dry_run_cache_not_updated(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        _run_dry_run must not add blobs for the pending session to the cache manifest.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "b.txt").write_bytes(b"original content")
    iso = tmp_path / "test.iso"
    _init(source, iso)

    (source / "b.txt").write_bytes(b"modified content for cache test run")

    manifest_path = DEFAULT_CACHE_DIR / "cache_manifest.json"
    entries_before: dict = (
        json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    )

    args = _make_args(command="sync", source=str(source), test_iso=str(iso))
    _run_dry_run(args, is_init=False)

    entries_after: dict = (
        json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    )
    new_keys = set(entries_after) - set(entries_before)
    session1_keys = {k for k in new_keys if ":1:" in k or k.startswith("1:")}
    assert not session1_keys


def test_sync_dry_run_space_overage_no_exit1(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        When staging size exceeds remaining capacity, _run_dry_run must print
        OVERAGE and still return 0 (not exit 1).
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "small.txt").write_bytes(b"x")
    iso = tmp_path / "test.iso"
    _init(source, iso)

    (source / "large.bin").write_bytes(b"A" * (45 * 2**20))  # 45 MiB > 50mb*0.95 margin
    capsys.readouterr()

    args = _make_args(
        command="sync", source=str(source), test_iso=str(iso), disc_size="50mb"
    )
    rc = _run_dry_run(args, is_init=False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERAGE" in out


if __name__ == "__main__":
    pass
