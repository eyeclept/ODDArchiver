"""Tests for tray control at disc-swap prompts."""
import logging
import subprocess

import pytest

from oddarchiver import drive as drive_mod


def test_open_tray_skips_iso_and_missing_device():
    """An ISO has no tray, so the call is a no-op that reports success."""
    assert drive_mod.open_tray(None) is True


def test_open_tray_runs_eject_and_reports_failure(monkeypatch):
    """A failing eject is logged and returns False, never raises."""
    seen = []

    class _Result:
        returncode = 1
        stderr = "no tray"

    def _run(argv, **kwargs):
        seen.append(argv)
        return _Result()

    monkeypatch.setattr(drive_mod.shutil, "which", lambda name: "/usr/bin/eject")
    monkeypatch.setattr(drive_mod.subprocess, "run", _run)
    assert drive_mod.open_tray("/dev/sr0") is False
    assert seen == [["/usr/bin/eject", "/dev/sr0"]]


def test_open_tray_success(monkeypatch):
    """A zero exit reports success."""
    class _Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(drive_mod.shutil, "which", lambda name: "/usr/bin/eject")
    monkeypatch.setattr(drive_mod.subprocess, "run", lambda *a, **k: _Result())
    assert drive_mod.open_tray("/dev/sr0") is True
