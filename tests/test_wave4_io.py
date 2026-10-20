"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Check bounded disc reads, private logs, and session listing fallback.
"""
# Imports
import argparse
import logging
import os
import subprocess
from pathlib import Path

import pytest

from oddarchiver.cli import _run_ls
from oddarchiver.disc import DiscBackend, DiscInfo, ISOBackend
from oddarchiver.log import setup_logging
from oddarchiver.manifest import Manifest, ManifestEntry, session_control_paths, write_manifest
from oddarchiver.session import _default_staging_root
from oddarchiver.verify import _probe_trailing_slot

# Globals
logger = logging.getLogger(__name__)

# Tests


def test_drive_rejects_large_blob_before_extraction(tmp_path, monkeypatch):
    """Input: oversized disc blob. Output: no extraction or scratch payload."""
    blob = "session_000/full/" + "a" * 64
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        if "-find" in cmd:
            report = f"File data lba: 0 , 100 , 50 , 102400 , '/{blob}'\n"
            return subprocess.CompletedProcess(cmd, 0, report.encode(), b"")
        raise AssertionError(f"Unexpected extraction: {cmd}")

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    dest = tmp_path / "scratch"
    with pytest.raises(RuntimeError, match="Not enough local space"):
        DiscBackend("/dev/sr9").extract_paths([blob], dest, max_bytes=100)
    assert len(commands) == 1
    assert not list(dest.rglob("*"))


def _write_fake_extracted_blobs(cmd) -> None:
    """
    Input: extraction command
    Output: None
    Details: Emulate writing each requested blob.
    """
    for n, item in enumerate(cmd):
        if item == "-extract":
            dest = Path(cmd[n + 2])
            dest.write_bytes(b"abc")


def _assert_extracted_batch(commands, dest, paths) -> None:
    """
    Input: recorded commands, scratch directory, and requested paths
    Output: None
    Details: Check extraction count and resulting bytes.
    """
    assert len(commands) == 2
    extracts = [item for item in commands[1] if item == "-extract"]
    assert extracts == ["-extract"] * 2
    contents = [(dest / path).read_bytes() for path in paths]
    assert contents == [b"abc", b"abc"]


def test_drive_extracts_one_batch_after_listing(tmp_path, monkeypatch):
    """Input: two small disc blobs. Output: one listing and one extraction."""
    paths = ["session_000/full/" + c * 64 for c in "ab"]
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        if "-find" in cmd:
            lines = [f"File data lba: 0 , {n}, 1 , 3 , '/{path}'" for n, path in enumerate(paths)]
            return subprocess.CompletedProcess(cmd, 0, ("\n".join(lines) + "\n").encode(), b"")
        _write_fake_extracted_blobs(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    dest = tmp_path / "scratch"
    assert DiscBackend("/dev/sr9").extract_paths(paths, dest, max_bytes=6) == 6
    _assert_extracted_batch(commands, dest, paths)


def test_iso_refuses_large_manifest_by_name(tmp_path):
    """Input: oversized ISO control file. Output: named refusal before read."""
    backend = ISOBackend(tmp_path / "archive.iso")
    name = "session_000/manifest.enc"
    target = backend._sessions_root / name
    target.parent.mkdir(parents=True)
    with target.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(RuntimeError, match=name):
        backend.read_paths([name])


def test_log_is_private_and_existing_user_dir_unchanged(tmp_path):
    """Input: fresh log path and preexisting directory. Output: private permissions."""
    parent = tmp_path / "new_logs"
    log = parent / "oddarchiver.log"
    setup_logging(log)
    root = logging.getLogger()
    try:
        assert parent.stat().st_mode & 0o777 == 0o700
        assert log.stat().st_mode & 0o777 == 0o600
        logging.getLogger("test.io").warning("private-name.txt")
        assert "private-name.txt" in log.read_text()
        os.chmod(log, 0o644)
        setup_logging(log)
        assert log.stat().st_mode & 0o777 == 0o600
        existing = tmp_path / "existing"
        existing.mkdir(mode=0o755)
        setup_logging(existing / "other.log")
        assert existing.stat().st_mode & 0o777 == 0o755
    finally:
        for handler in list(root.handlers):  # scbc ignore[unnecessary-list-call] removing handlers mutates the iterated container
            if isinstance(handler, logging.FileHandler) and handler.baseFilename in (str(log), str(tmp_path / "existing" / "other.log")):
                root.removeHandler(handler)
                handler.close()


def test_listing_failure_falls_back_for_reads_but_not_burn(monkeypatch, caplog):
    """Input: failed drive listing. Output: complete probe paths and hard burn refusal."""
    backend = DiscBackend("/dev/sr9")

    def fail(_cmd, **_kwargs):
        return subprocess.CompletedProcess([], 1, b"", b"no disc")

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fail)
    with caplog.at_level(logging.WARNING):
        paths = session_control_paths(backend)
    assert len(paths) == 3000
    assert "session_999/manifest.enc" in paths
    assert "probe" in caplog.text.lower()
    with pytest.raises(RuntimeError, match="Cannot list sessions"):
        backend.list_session_numbers()


def test_ls_works_after_drive_listing_failure(tmp_path, monkeypatch, capsys):
    """Input: failed listing and readable control file. Output: listed path."""
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest = Manifest(
        version=2, session=0, timestamp="2026-01-01T00:00:00Z",
        source="/source", label="TEST", based_on_session=None,
        encryption={"mode": "none"},
        entries=[ManifestEntry(
            path="listed.txt", type="full", result_checksum="a" * 64,
            full_size_bytes=3, file="session_000/full/" + "a" * 64,
        )],
        deleted=[], manifest_checksum="",
    )
    write_manifest(staging, manifest)
    backend = DiscBackend("/dev/sr9")
    control = {"session_000/manifest.json": (staging / "manifest.json").read_bytes()}
    def failed_listing():
        raise RuntimeError("drive listing failed")
    monkeypatch.setattr(backend, "list_session_numbers", failed_listing)
    monkeypatch.setattr(backend, "read_paths", lambda paths: {
        path: control[path] for path in paths if path in control
    })
    monkeypatch.setattr(backend, "mediainfo", lambda: DiscInfo(1, 10, 1, "TEST"))
    monkeypatch.setattr("oddarchiver.cli._make_backend", lambda _args: backend)
    assert _run_ls(argparse.Namespace(key=None, session=None)) == 0
    assert "listed.txt" in capsys.readouterr().out


def test_trailing_probe_falls_back_to_control_bytes(monkeypatch, caplog):
    """Input: failed listing and a trailing manifest. Output: trailing slot."""
    backend = DiscBackend("/dev/sr9")
    def _raise_listing_failed():
        raise RuntimeError("listing failed")

    monkeypatch.setattr(backend, "list_session_numbers", _raise_listing_failed)
    with caplog.at_level(logging.WARNING):
        result = _probe_trailing_slot(backend, 0, {"session_001/manifest.enc": b"x"})
    assert result == 1
    assert "listing failed" in caplog.text


def test_staging_stays_under_per_test_runtime(tmp_path):
    """Input: an autouse HOME and XDG environment. Output: isolated staging."""
    root = _default_staging_root()
    assert root.is_relative_to(tmp_path)
    assert root == tmp_path / "xdg_runtime" / "oddarchiver"
    for key in ("HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert Path(os.environ[key]).is_relative_to(tmp_path)
