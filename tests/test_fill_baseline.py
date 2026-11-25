"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    After a fill session lands on an earlier volume, later sync uses the
    highest global session as the catalog, not the highest volume_index.
"""
# Imports
import json
import logging
import types
from pathlib import Path

from oddarchiver.cli import _print_need_catalog_disc, build_parser, dispatch
from tests.helpers import fill
from tests.test_volume_staging import (
    _FILL_DISC,
    _build_two_volume_fill_archive,
    _fill_argv,
    _fill_dispatch,
    _iso_sessions,
)

logger = logging.getLogger(__name__)


def _silence_iso_input(monkeypatch) -> None:
    """Input: pytest monkeypatch. Output: None. Details: ISO runs must not prompt."""

    def _boom(prompt=""):
        raise AssertionError(f"ISOBackend must not call input(): {prompt!r}")

    monkeypatch.setattr("builtins.input", _boom)


def _isolate_state(tmp_path: Path, monkeypatch) -> None:
    """Input: tmp path and monkeypatch. Output: None. Details: Isolate cache and state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    _silence_iso_input(monkeypatch)


def _filled_two_volume_archive(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """
    Input: tmp_path, monkeypatch
    Output: source, volume-0 ISO, volume-1 ISO
    Details: Volume 0 holds a fill session after volume 1 exists.
    """
    _isolate_state(tmp_path, monkeypatch)
    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    assert _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    )) == 0
    assert max(_iso_sessions(iso)) > max(_iso_sessions(vol1))
    return source, iso, vol1


def _dry_sync(source: Path, iso: Path, capsys) -> tuple[int, str, str]:
    """Input: source, inserted ISO, capsys. Output: rc, stdout, stderr."""
    (source / "d.bin").write_bytes(fill(b"D", 20_000))
    capsys.readouterr()
    rc = dispatch(build_parser().parse_args([
        "sync", str(source), "--test-iso", str(iso), "--disc-size", _FILL_DISC,
        "--fill", "--dry-run",
    ]))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_fill_baseline_from_volume1_with_local_history(tmp_path, monkeypatch, capsys):
    """
    Input: fill session on volume 0, sync from volume 1 with local history
    Output: fit.bin unchanged; only d.bin is new; next session is max+1
    """
    source, iso, vol1 = _filled_two_volume_archive(tmp_path, monkeypatch)
    next_n = max(_iso_sessions(iso) + _iso_sessions(vol1)) + 1
    rc, out, err = _dry_sync(source, vol1, capsys)
    assert rc == 0, err
    assert f"next session {next_n}" in out
    assert "1 new" in out
    assert "unchanged" in out
    assert "d.bin" in out
    assert "fit.bin" not in out.split("Changes")[-1]


def test_fill_baseline_from_volume0_highest_session(tmp_path, monkeypatch, capsys):
    """
    Input: fill session on volume 0, sync from that disc
    Output: accepted; same plan as volume 1 with local history
    """
    source, iso, vol1 = _filled_two_volume_archive(tmp_path, monkeypatch)
    next_n = max(_iso_sessions(iso) + _iso_sessions(vol1)) + 1
    rc, out, err = _dry_sync(source, iso, capsys)
    assert rc == 0, err
    assert "Fill needs the newest catalog disc" not in err
    assert f"next session {next_n}" in out
    assert "1 new" in out
    assert "d.bin" in out
    assert "fit.bin" not in out.split("Changes")[-1]


def test_fill_baseline_without_local_history_uses_stub(tmp_path, monkeypatch, capsys):
    """
    Input: fill session on volume 0, local archive record removed, volume 1 inserted
    Output: layout stub on volume 1 completes the baseline
    Details:
        Reason: owner decision 5. A stub makes the baseline complete
        without local memory or sibling ISO reads of the change set.
    """
    source, iso, vol1 = _filled_two_volume_archive(tmp_path, monkeypatch)
    state_dir = tmp_path / "state" / "oddarchiver"
    for record in state_dir.glob("archive-*.json"):
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["discs"] = {}
        payload["highest_volume_index"] = 0
        record.write_text(json.dumps(payload), encoding="utf-8")
    rc, out, err = _dry_sync(source, vol1, capsys)
    assert rc == 0, err
    assert "1 new" in out
    assert "d.bin" in out
    assert "fit.bin" not in out.split("Changes")[-1]

def _strip_volume1_snapshot(vol1: Path) -> None:
    """Input: volume-1 ISO path. Output: None. Details: Drop catalog_snapshot."""
    marker_path = vol1.with_suffix(".d") / "volume.json"
    raw = json.loads(marker_path.read_text(encoding="utf-8"))
    raw.pop("catalog_snapshot", None)
    raw.pop("catalog_snapshot_enc", None)
    marker_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def test_fill_baseline_without_stub_or_history_refuses(tmp_path, monkeypatch, capsys):
    """
    Input: fill on volume 0, stub removed from volume 1, local record cleared
    Output: refuse and name volume 0; plan nothing
    Details:
        Never plan against an incomplete baseline. No stub, no local
        history, and no snapshot covering the fill session.
    """
    source, iso, vol1 = _filled_two_volume_archive(tmp_path, monkeypatch)
    _strip_volume1_snapshot(vol1)
    state_dir = tmp_path / "state" / "oddarchiver"
    for record in state_dir.glob("archive-*.json"):
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["discs"] = {}
        payload["highest_volume_index"] = 0
        record.write_text(json.dumps(payload), encoding="utf-8")
    rc, out, err = _dry_sync(source, vol1, capsys)
    assert rc == 1
    assert "volume 0" in err or "highest session" in err
    assert "fit.bin" not in out
    assert "1 new" not in out
    assert "Changes" not in out
    assert "d.bin" not in out



def test_fill_own_highest_incomplete_catalog_message(capsys):
    """
    Input: inserted volume holds the highest session; baseline is incomplete
    Output: own-disc gap text names missing sessions; no Insert volume N
    """
    marker = types.SimpleNamespace(volume_index=0, archive_id="a")
    rc = _print_need_catalog_disc(marker, None, True, {1})
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot supply a complete catalog" in err
    assert "Missing sessions: 1." in err
    assert "older build" in err
    assert "Run sync --fill" in err
    assert "Insert ARCHIVE (volume" not in err
    assert "Insert the disc that holds the highest session" not in err
    rc = _print_need_catalog_disc(marker, (2, 0, "ARCHIVE"), False, {1})
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot supply a complete catalog" in err
    assert "wait for --fill" in err
    assert "Insert ARCHIVE (volume 0)" not in err


def test_fill_other_volume_still_names_holder(capsys):
    """
    Input: a different volume holds the highest session
    Output: Insert <label> (volume N) for fill and no-fill
    """
    marker = types.SimpleNamespace(volume_index=1, archive_id="a")
    holder = (2, 0, "ARCHIVE")
    rc = _print_need_catalog_disc(marker, holder, True, {2})
    err = capsys.readouterr().err
    assert rc == 1
    assert "Fill needs the newest catalog disc" in err
    assert "Insert ARCHIVE (volume 0)" in err
    assert "cannot supply a complete catalog" not in err
    rc = _print_need_catalog_disc(marker, holder, False, {2})
    err = capsys.readouterr().err
    assert rc == 1
    assert "older volume of the archive" in err
    assert "Insert ARCHIVE (volume 0), or wait for --fill" in err


