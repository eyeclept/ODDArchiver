"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for Task 14: Idempotency — init on existing disc, sync no-change silence,
    interrupted burn recovery, stale staging dir cleanup.
"""
# Imports
import argparse
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oddarchiver.cli import dispatch
from oddarchiver.disc import ISOBackend, DiscInfo
from oddarchiver.session import build_staging
from oddarchiver.crypto import NullCrypto

# Globals
SMALL_DISC = 50 * 2**20  # 50 MiB


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs — field overrides for the Namespace
    Output: argparse.Namespace with sensible defaults
    """
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
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# Tests


def test_init_on_initialized_iso_exits_0_with_warning(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        Running init twice on the same ISO must exit 0 with a warning on the
        second call and must not write a second session.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    capsys.readouterr()
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))

    assert rc == 0
    captured = capsys.readouterr()
    assert "already initialized" in captured.err
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 1


def test_sync_twice_identical_source_produces_one_burn(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        sync with no source changes must silently exit 0 and produce no second burn.
        Running sync twice with the same source must leave session count at 1.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"stable content")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0
    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 1


def test_burn_failure_leaves_cache_unchanged(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        If backend.append() raises an exception (simulated burn failure), the
        cache must not be updated.  Cache integrity is verified by asserting that
        cache.put() is never called when the burn fails.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"initial content")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "b.txt").write_bytes(b"new file for second session")

    put_calls: list = []

    original_append = ISOBackend.append

    def failing_append(self, staging, label, expected_session_count=None):
        raise RuntimeError("Simulated burn failure")

    from oddarchiver.cache import CacheManager
    original_put = CacheManager.put

    def tracking_put(self, *args, **kwargs):
        put_calls.append(args)
        return original_put(self, *args, **kwargs)

    with patch.object(ISOBackend, "append", failing_append), \
         patch.object(CacheManager, "put", tracking_put):
        rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))

    assert rc == 1
    assert not put_calls, "cache.put must not be called after a burn failure"


def test_stale_staging_dir_removed_and_rebuilt(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        If a staging directory from a prior crashed run already exists when
        build_staging is called, it must be removed and a fresh one built.
        Verified by placing a sentinel file in the stale dir and checking it
        is gone after build_staging returns.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello world")

    staging_root = tmp_path / "staging_root"
    staging_root.mkdir()

    stale_dir = staging_root / "oddarchiver_staging_000"
    stale_dir.mkdir()
    sentinel = stale_dir / "stale_sentinel.txt"
    sentinel.write_text("this is stale")

    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=0,
        remaining_bytes=25 * 2 ** 30,
        used_bytes=0,
        label="TEST",
    )
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""

    staging = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        backend=backend,
        cache=cache,
        crypto=NullCrypto(),
        _staging_root=staging_root,
    )
    try:
        assert not sentinel.exists(), "stale sentinel must be removed"
        assert (staging / "session_000" / "manifest.json").exists()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    pass
