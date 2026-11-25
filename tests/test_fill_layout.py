"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Fill layout stubs and catalog snapshots. A later sync from one disc
    plans only the new file with no sibling ISO access.
"""
# Imports
import json
import logging
import shutil
from pathlib import Path

from oddarchiver.cli import build_parser, dispatch
from oddarchiver.disc import ISOBackend
from oddarchiver.volume import read_fill_intent, read_layout_stub, read_volume_baseline
from tests.helpers import fill
from tests.test_volume_staging import (
    _FILL_DISC,
    _FILL_DISC_BYTES,
    _build_two_volume_fill_archive,
    _fill_argv,
    _fill_dispatch,
    _iso_sessions,
)

logger = logging.getLogger(__name__)


class IsolatedDrive:
    """Drive-like backend bound to one ISO. Not an ISOBackend subclass."""

    device = None

    def __init__(self, iso_path: Path, disc_size: int) -> None:
        self.disc_size = disc_size
        self._session_numbers_cache: list[int] | None = None
        self.load(iso_path)

    def load(self, iso_path: Path) -> None:
        """Input: ISO path. Output: None. Details: Bind this drive to that image."""
        self.iso_path = iso_path
        self._inner = ISOBackend(iso_path, disc_size=self.disc_size)

    def list_session_numbers(self) -> list[int]:
        """Input: None. Output: cached root sessions of the loaded medium."""
        if self._session_numbers_cache is None:
            self._session_numbers_cache = self._inner.list_session_numbers()
        return list(self._session_numbers_cache)

    def invalidate_session_list(self) -> None:
        """Input: None. Output: None. Details: Drop the cached listing."""
        self._session_numbers_cache = None
        self._inner.invalidate_session_list()

    def __getattr__(self, name):
        return getattr(self._inner, name)



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


def _copy_isolated_iso(iso: Path, dest_dir: Path) -> Path:
    """Input: ISO path and empty dest dir. Output: copied ISO with its .d tree."""
    dest_dir.mkdir(parents=True)
    dest = dest_dir / iso.name
    shutil.copy2(iso, dest)
    src_d = iso.with_suffix(".d")
    if src_d.is_dir():
        shutil.copytree(src_d, dest.with_suffix(".d"))
    return dest


def _dry_sync(source: Path, iso: Path, capsys) -> tuple[int, str, str]:
    """Input: source, inserted ISO, capsys. Output: rc, stdout, stderr."""
    (source / "later.bin").write_bytes(fill(b"L", 20_000))
    capsys.readouterr()
    rc = dispatch(build_parser().parse_args([
        "sync", str(source), "--test-iso", str(iso), "--disc-size", _FILL_DISC,
        "--dry-run",
    ]))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _install_isolated_drive(monkeypatch, iso: Path) -> None:
    """Input: monkeypatch and isolated ISO. Output: None. Details: No sibling access."""

    def _make(_args):
        return IsolatedDrive(iso, _FILL_DISC_BYTES)

    monkeypatch.setattr("oddarchiver.cli._make_backend", _make)


def test_fill_off_without_flag_even_with_tty(monkeypatch):
    """
    Input: TTY stdin and no --fill
    Output: fill stays off
    """
    from oddarchiver.cli import _fill_enabled

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    args = build_parser().parse_args(["sync", "/src"])
    assert _fill_enabled(args) is False  # scbc ignore[is-true-false-strict] asserts an exact bool, not truthiness


def test_sync_from_filled_volume0_without_siblings(tmp_path, monkeypatch, capsys):
    """
    Input: fill on volume 0, isolated copy, fake drive with no sibling access
    Output: later sync plans only the new file
    """
    source, iso, _vol1 = _filled_two_volume_archive(tmp_path, monkeypatch)
    isolated = _copy_isolated_iso(iso, tmp_path / "only0")
    _install_isolated_drive(monkeypatch, isolated)
    rc, out, err = _dry_sync(source, isolated, capsys)
    assert rc == 0, err
    assert "1 new" in out
    assert "later.bin" in out
    assert "fit.bin" not in out.split("Changes")[-1]


def test_sync_from_volume1_with_stub(tmp_path, monkeypatch, capsys):
    """
    Input: fill on volume 0, stub on volume 1, isolated volume 1
    Output: later sync plans only the new file
    """
    source, _iso, vol1 = _filled_two_volume_archive(tmp_path, monkeypatch)
    stub = read_layout_stub(ISOBackend(vol1, disc_size=_FILL_DISC_BYTES), None)
    assert stub is not None
    assert stub.layout is not None
    isolated = _copy_isolated_iso(vol1, tmp_path / "only1")
    _install_isolated_drive(monkeypatch, isolated)
    rc, out, err = _dry_sync(source, isolated, capsys)

    assert rc == 0, err
    assert "1 new" in out
    assert "later.bin" in out
    assert "fit.bin" not in out.split("Changes")[-1]


def test_stub_records_not_stubbed_volumes(tmp_path, monkeypatch):
    """
    Input: fill on volume 0 with an outcome stub on volume 1
    Output: fill layout does not list volume 1; volume 1 holds fill intent
    """
    _source, iso, vol1 = _filled_two_volume_archive(tmp_path, monkeypatch)
    stub = read_layout_stub(ISOBackend(vol1, disc_size=_FILL_DISC_BYTES), None)
    assert stub is not None
    assert stub.layout is not None
    fill_base = read_volume_baseline(ISOBackend(iso, disc_size=_FILL_DISC_BYTES), None)
    assert fill_base is not None
    assert fill_base.layout is not None
    assert 1 not in fill_base.layout.not_stubbed
    intent = read_fill_intent(ISOBackend(vol1, disc_size=_FILL_DISC_BYTES), None)
    assert intent is not None



def test_fill_skips_volume_with_too_little_recorded_space(tmp_path, monkeypatch, capsys):
    """
    Input: two volumes, volume 0 remaining_bytes too small for the new blob
    Output: fill skips volume 0 without using it and appends to volume 1
    """
    _isolate_state(tmp_path, monkeypatch)
    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    vol0_sessions = _iso_sessions(iso)
    vol1_sessions = _iso_sessions(vol1)
    state_dir = tmp_path / "state" / "oddarchiver"
    for record in state_dir.glob("archive-*.json"):
        payload = json.loads(record.read_text(encoding="utf-8"))
        disc = payload["discs"]["0"]
        disc["remaining_bytes"] = 10
        disc["measured_at"] = "2099-01-01T00:00:00Z"
        payload["discs"]["0"] = disc
        record.write_text(json.dumps(payload), encoding="utf-8")
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    capsys.readouterr()
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "skipping volume 0" in captured.out
    assert "cannot hold any planned blob" in captured.out
    assert _iso_sessions(iso) == vol0_sessions
    assert max(_iso_sessions(vol1)) > max(vol1_sessions)
