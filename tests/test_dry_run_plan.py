"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Dry-run volume plan uses the real packer. ISO backend, small disc size.
"""
# Imports
import argparse
import logging
import os

from oddarchiver.cli import _run_dry_run
from oddarchiver.disc import ISOBackend
from oddarchiver.session import _default_staging_root

logger = logging.getLogger(__name__)

# Globals
_DISC_THAT_SPLITS = 100_000
_FILE_BYTES = 60_000
_TINY_DISC = 50_000
_HUGE_BYTES = 80_000
_FIT_DISC = 200_000


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """Input: field overrides. Output: dry-run Namespace for ISO tests."""
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "TEST",
        "encrypt": "none",
        "key": None,
        "dry_run": True,
        "no_cache": False,
        "disc_size": str(_DISC_THAT_SPLITS),
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
        "yubikey": False,
        "yubikey_slot": None,
        "skip_large": False,
        "split_large": False,
        "fill": False,
        "no_fill": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _write_pair(source, size: int = _FILE_BYTES) -> None:
    """Input: source dir and size. Output: two files of that size."""
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * size)
    (source / "b.bin").write_bytes(b"B" * size)


def test_default_policy_plans_whole_file_volumes(tmp_path, capsys, monkeypatch):
    """
    Input: two files that each fit one disc and together exceed one disc
    Output: N whole-file volumes, no OVERAGE, and no staging children
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    source = tmp_path / "src"
    _write_pair(source)
    iso = tmp_path / "test.iso"
    args = _make_args(source=str(source), test_iso=str(iso))
    rc = _run_dry_run(args, is_init=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERAGE" not in out
    assert "session_000" in out
    assert "session_001" in out
    assert "inserted disc" in out
    assert "new volume" in out
    assert "a.bin" in out
    assert "b.bin" in out
    assert not iso.exists() or ISOBackend(
        iso, disc_size=_DISC_THAT_SPLITS,
    ).mediainfo().session_count == 0
    staging = _default_staging_root()
    present = list(staging.iterdir()) if staging.exists() else []
    assert present == []


def test_split_large_plans_greedy_split(tmp_path, capsys, monkeypatch):
    """
    Input: two files with --split-large
    Output: session_000 and session_001; b.bin split across both; a.bin listed
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    source = tmp_path / "src"
    _write_pair(source)
    iso = tmp_path / "test.iso"
    args = _make_args(
        source=str(source), test_iso=str(iso), split_large=True,
    )
    rc = _run_dry_run(args, is_init=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERAGE" not in out
    assert "split" in out
    assert "chunks" in out
    assert "a.bin" in out
    plan = out.split("Volume plan:")[-1]
    assert "session_000" in plan
    assert "session_001" in plan
    assert "b.bin split 2 chunks" in plan



def test_plan_that_fits_one_disc_has_no_overage(tmp_path, capsys, monkeypatch):
    """
    Input: two small files on a disc that holds both
    Output: one volume and no OVERAGE
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    source = tmp_path / "src"
    _write_pair(source, size=1000)
    iso = tmp_path / "test.iso"
    args = _make_args(
        source=str(source), test_iso=str(iso), disc_size=str(_FIT_DISC),
    )
    rc = _run_dry_run(args, is_init=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERAGE" not in out
    assert "session_000" in out
    assert "session_001" not in out
    assert "Volume plan:" in out


def test_file_larger_than_disc_splits_without_split_large(
    tmp_path, capsys, monkeypatch,
):
    """
    Input: one file larger than one disc, split_large off
    Output: split chunks across volumes
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    source = tmp_path / "src"
    source.mkdir()
    (source / "huge.bin").write_bytes(b"H" * _HUGE_BYTES)
    iso = tmp_path / "test.iso"
    args = _make_args(
        source=str(source), test_iso=str(iso), disc_size=str(_TINY_DISC),
    )
    rc = _run_dry_run(args, is_init=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERAGE" not in out
    assert "huge.bin split" in out
    assert "chunks" in out


def test_dry_run_writes_no_session_and_no_staging(tmp_path, capsys, monkeypatch):
    """
    Input: init dry run
    Output: no ISO session and no staging children
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    source = tmp_path / "src"
    _write_pair(source, size=1000)
    iso = tmp_path / "test.iso"
    args = _make_args(
        source=str(source), test_iso=str(iso), disc_size=str(_FIT_DISC),
    )
    rc = _run_dry_run(args, is_init=True)
    assert rc == 0
    capsys.readouterr()
    assert not iso.exists()
    staging = runtime / "oddarchiver"
    if staging.exists():
        assert list(staging.iterdir()) == []
    cache_home = os.environ.get("XDG_CACHE_HOME")
    logger.info("dry-run plan left cache home %s", cache_home)
