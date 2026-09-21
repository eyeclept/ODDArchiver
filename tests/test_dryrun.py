"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for --dry-run mode: no ISO written, no cache updated, correct output,
    space overage reported without exit 1.
    Tests call _run_dry_run directly so they can pass a test ISO path
    without going through dispatch() write paths.
"""
# Imports
import argparse
import json

from pathlib import Path

from oddarchiver.cache import DEFAULT_CACHE_DIR
from oddarchiver.cli import dispatch, _run_dry_run
from oddarchiver.disc import DiscInfo, ISOBackend

# Globals
SMALL_DISC = 50 * 2**20  # 50 MiB


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides
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
        _run_dry_run must not call backend.append(). Session count stays at 1.
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


def test_dry_run_next_session_ignores_mmc_session_count(
    tmp_path, capsys, monkeypatch,
):
    """
    Input:  tmp_path, capsys, monkeypatch
    Output: None
    Details:
        BD-R SRM+POW reports Number of Sessions 1 after many appends.
        With archive sessions 000 and 001 on the medium, sync --dry-run
        must announce session_002, matching the real burn path.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"version 1")
    iso = tmp_path / "test.iso"
    _init(source, iso)
    (source / "a.txt").write_bytes(b"version 2 enough change for a new session")
    assert dispatch(_make_args(
        command="sync", dry_run=False, source=str(source), test_iso=str(iso),
    )) == 0

    class _PowLyingISO(ISOBackend):
        def mediainfo(self):
            info = super().mediainfo()
            return DiscInfo(
                session_count=1,
                remaining_bytes=info.remaining_bytes,
                used_bytes=info.used_bytes,
                label=info.label,
            )

    monkeypatch.setattr(
        "oddarchiver.cli._make_backend",
        lambda args: _PowLyingISO(
            Path(args.test_iso), disc_size=SMALL_DISC,
        ),
    )
    (source / "a.txt").write_bytes(b"version 3 dry-run pending change")
    capsys.readouterr()
    rc = _run_dry_run(
        _make_args(command="sync", source=str(source), test_iso=str(iso)),
        is_init=False,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Would burn as: session_002" in out
    assert "Would burn as: session_001" not in out


def test_dry_run_disc_state_line_ignores_mmc_session_count(
    tmp_path, capsys, monkeypatch,
):
    """
    Input:  tmp_path, capsys, monkeypatch
    Output: None
    Details:
        BD-R SRM+POW pins mediainfo session_count at 1. With archive
        sessions 000 and 001 on the medium, the disc-state line must not
        print that MMC count as a session number.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"version 1")
    iso = tmp_path / "test.iso"
    _init(source, iso)
    (source / "a.txt").write_bytes(b"version 2 enough change for a new session")
    assert dispatch(_make_args(
        command="sync", dry_run=False, source=str(source), test_iso=str(iso),
    )) == 0

    class _PowLyingISO(ISOBackend):
        def mediainfo(self):
            info = super().mediainfo()
            return DiscInfo(
                session_count=1,
                remaining_bytes=info.remaining_bytes,
                used_bytes=info.used_bytes,
                label=info.label,
            )

    monkeypatch.setattr(
        "oddarchiver.cli._make_backend",
        lambda args: _PowLyingISO(
            Path(args.test_iso), disc_size=SMALL_DISC,
        ),
    )
    (source / "a.txt").write_bytes(b"version 3 dry-run pending change")
    capsys.readouterr()
    rc = _run_dry_run(
        _make_args(command="sync", source=str(source), test_iso=str(iso)),
        is_init=False,
    )
    assert rc == 0
    out = capsys.readouterr().out
    state_line = [ln for ln in out.splitlines() if "Disc state:" in ln]
    assert len(state_line) == 1
    assert "session 1" not in state_line[0]
    assert "next session 2" in state_line[0]


def test_dry_run_init_disc_state_blank_disc(tmp_path, capsys, monkeypatch):
    """
    Input:  tmp_path, capsys, monkeypatch
    Output: None
    Details:
        The is_init path has no marker and no manifests. The disc-state
        line must still print, with next session 0, and must not raise
        NameError or UnboundLocalError.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"first version")
    iso = tmp_path / "test.iso"
    capsys.readouterr()
    rc = _run_dry_run(
        _make_args(command="init", source=str(source), test_iso=str(iso)),
        is_init=True,
    )
    assert rc == 0
    out = capsys.readouterr().out
    state_line = [ln for ln in out.splitlines() if "Disc state:" in ln]
    assert len(state_line) == 1
    assert "next session 0" in state_line[0]


if __name__ == "__main__":
    pass
