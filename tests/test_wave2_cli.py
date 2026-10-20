"""Regression tests for CLI read modes and one-batch control reads."""

# Imports
import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from oddarchiver import cli
from oddarchiver.disc import DiscInfo, ISOBackend
from oddarchiver.restore import RestoreResult

# Globals
logger = logging.getLogger(__name__)
_log = logger

# Functions


@pytest.mark.parametrize("command", ("ls", "status", "verify", "restore"))
def test_read_commands_fetch_volume_marker_in_control_batch(command, monkeypatch, tmp_path):
    """
    Input: command, monkeypatch, tmp_path
    Output: None
    Details: Read the marker once in the same batch as session control files.
    """
    backend = MagicMock()
    backend.read_paths.return_value = {}
    backend.list_session_numbers.return_value = [0]
    backend.mediainfo.return_value = DiscInfo(
        session_count=0, remaining_bytes=1024, used_bytes=0, label="W2",
    )
    monkeypatch.setattr(cli, "_make_backend", lambda _args: backend)
    monkeypatch.setattr(cli, "_check_loaded_manifests", lambda *a, **k: None)
    monkeypatch.setattr("oddarchiver.verify.verify", lambda *a, **k: None)
    monkeypatch.setattr(
        "oddarchiver.restore.restore",
        lambda **kwargs: RestoreResult(restored=0, failures=0),
    )
    args = SimpleNamespace(
        command=command, key=None, level="fast", dest=str(tmp_path),
        test_iso=None, all_disks=False, force=False, read_mode=None,
    )
    run = {
        "ls": cli._run_ls,
        "status": cli._run_status,
        "verify": cli._run_verify,
        "restore": cli._run_restore,
    }[command]

    assert run(args) == 0
    batches = [call.args[0] for call in backend.read_paths.call_args_list]
    assert len(batches) == 1
    assert sum(batch.count("volume.json") for batch in batches) == 1
    assert "session_000/manifest.json" in batches[0]

@pytest.mark.parametrize("all_disks,expected_mode", ((False, "sequential"), (True, None)))
def test_restore_read_mode_default_tracks_scope(
    all_disks, expected_mode, monkeypatch, tmp_path,
):
    """
    Input: all_disks, expected_mode, monkeypatch, tmp_path
    Output: None
    Details: Use sequential for one disc and automatic selection for all discs.
    """
    marker = SimpleNamespace(archive_id="test-archive", first_session=0)
    backend = MagicMock()
    backend.read_paths.return_value = {}
    monkeypatch.setattr("oddarchiver.volume.read_volume_marker", lambda _backend: marker)
    backend.mediainfo.return_value = DiscInfo(
        session_count=0, remaining_bytes=1024, used_bytes=0, label="W2",
    )
    monkeypatch.setattr(cli, "_make_backend", lambda _args: backend)
    monkeypatch.setattr(
        cli, "_crypto_and_manifests",
        lambda *a, **k: (MagicMock(), [], SimpleNamespace(marker=marker)),
    )
    monkeypatch.setattr(cli, "_check_loaded_manifests", lambda *a, **k: None)
    monkeypatch.setattr(cli, "build_archive_catalog", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cli, "highest_seen_volume", lambda *a: None)
    monkeypatch.setattr(cli, "remember_discs", lambda *a: None)
    monkeypatch.setattr(cli, "_print_catalog_notices", lambda *a: None)
    observed = []

    def restore_result(**kwargs):
        observed.append(kwargs["read_mode"])
        return RestoreResult(restored=0, failures=0)

    monkeypatch.setattr("oddarchiver.restore.restore", restore_result)
    monkeypatch.setattr("oddarchiver.restore_plan.restore_archive", restore_result)
    args = SimpleNamespace(
        command="restore", dest=str(tmp_path), test_iso=None, key=None,
        rescue_key=None, read_mode=None, all_disks=all_disks,
    )

    assert cli._run_restore(args) == 0
    assert observed == [expected_mode]


@pytest.mark.parametrize("mode,expected", ((None, "sequential"), ("full-copy", "full-copy")))
def test_verify_uses_sequential_mode_by_default(mode, expected, monkeypatch):
    """
    Input: mode, expected, monkeypatch
    Output: None
    Details: Verify uses the selected mode or the one-disc default.
    """
    monkeypatch.setattr(cli, "_make_backend", lambda _args: MagicMock())
    monkeypatch.setattr(
        cli, "_crypto_and_manifests",
        lambda *a, **k: (MagicMock(), [], {}),
    )
    seen = []
    monkeypatch.setattr(
        "oddarchiver.verify.verify",
        lambda *a, **k: seen.append(k["read_mode"]),
    )

    assert cli._run_verify(SimpleNamespace(level="fast", key=None, read_mode=mode)) == 0
    assert seen == [expected]


def _build_full_copy_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> tuple[dict[str, bytes], Path, list[str], argparse.ArgumentParser]:
    """Input: isolated test paths. Output: a two-disc archive with two sessions on disc 1."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    source = tmp_path / "src"
    source.mkdir()
    files = {
        "keep.txt": b"keep-bytes-v1",
        "big.bin": b"B" * 2_000_000,
        "big2.bin": b"C" * 1_000_000,
    }
    for name, data in files.items():
        (source / name).write_bytes(data)
    iso = tmp_path / "fc.iso"
    parser = cli.build_parser()
    assert cli.dispatch(parser.parse_args([
        "init", str(source), "--test-iso", str(iso), "--disc-size", "4mb",
        "--label", "FC",
    ])) == 0
    vol1 = tmp_path / "fc.vol1.iso"
    assert vol1.is_file()
    files["keep.txt"] = b"keep-bytes-v2"
    (source / "keep.txt").write_bytes(files["keep.txt"])
    assert cli.dispatch(parser.parse_args([
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", "4mb",
    ])) == 0
    assert not (tmp_path / "fc.vol2.iso").exists()
    sessions = sorted(p.name for p in vol1.with_suffix(".d").glob("session_*"))
    assert len(sessions) == 2
    return files, vol1, sessions, parser


def _build_extract_tracker(
    original: Callable[..., int], batches: list[tuple[str, list[str], int]],
) -> Callable[..., int]:
    """Input: original extractor and batches. Output: extractor recording blob reads."""
    def track(self, paths, dest_root, max_bytes=None):
        """Input: blob paths. Output: bytes written. Details: Record each blob batch."""
        written = original(self, paths, dest_root, max_bytes=max_bytes)
        blobs = [path for path in paths if "/full/" in path or "/deltas/" in path]
        if blobs:
            root = Path(self.iso_path).with_suffix(".d")
            assert written == sum((root / path).stat().st_size for path in blobs)
            batches.append((Path(self.iso_path).name, blobs, written))
        return written

    return track


def _assert_full_copy_restore(
    batches: list[tuple[str, list[str], int]], sessions: list[str],
    dest: Path, files: dict[str, bytes], capsys: pytest.CaptureFixture[str],
) -> None:
    """Input: read batches, expected files, output capture. Output: restored bytes and batch invariants."""
    disc1_sessions = {blob.split("/", 1)[0] for blob in batches[0][1]}
    assert disc1_sessions == set(sessions)
    assert len(batches[1][1]) == 1
    assert all(written > 0 for _name, _blobs, written in batches)
    assert {p.name: p.read_bytes() for p in dest.iterdir()} == files
    assert "  missing:" not in capsys.readouterr().out


def test_all_disks_full_copy_reads_one_batch_per_disc(monkeypatch, tmp_path, capsys):
    """
    Input: monkeypatch, tmp_path, capsys
    Output: None
    Details: Build a real two-disc archive with two sessions on disc 1.
        restore --all-disks --read-mode full-copy extracts one blob batch
        per disc. The batches hold the blobs of both disc 1 sessions and
        write exactly the on-disc blob bytes.
    """
    files, vol1, sessions, parser = _build_full_copy_archive(monkeypatch, tmp_path)
    batches = []
    original = ISOBackend.extract_paths
    monkeypatch.setattr(ISOBackend, "extract_paths", _build_extract_tracker(original, batches))
    dest = tmp_path / "out"
    capsys.readouterr()
    assert cli.dispatch(parser.parse_args([
        "restore", str(dest), "--test-iso", str(vol1), "--all-disks",
        "--all-files", "--read-mode", "full-copy",
    ])) == 0
    assert [name for name, _blobs, _written in batches] == ["fc.vol1.iso", "fc.iso"]
    _assert_full_copy_restore(batches, sessions, dest, files, capsys)
