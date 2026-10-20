"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Integration tests. Full pipeline against ISOBackend.
    Covers init plus sync plus verify --level full plus restore.
    Also covers dry-run ISO invariance, SIGINT staging cleanup, and burn cache isolation.
    No physical disc is required. All tests use ISOBackend.
"""
# Imports
import argparse
import hashlib
import logging
import json
import shutil
from pathlib import Path
from unittest.mock import patch


from oddarchiver.cli import dispatch
from oddarchiver.disc import ISOBackend
import oddarchiver.session as session_mod
from tests.helpers import _assert_staging_sigint

# Globals
SMALL_DISC = 100 * 2**20  # 100 MiB - room for several sessions
logger = logging.getLogger(__name__)


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides
    Output: argparse.Namespace with test-safe defaults
    """
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "INTTEST",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "100mb",
        "prefill": None,
        "session": None,
        "force": True,
        "level": "full",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Tests


def test_full_pipeline_init_sync_verify_restore(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Full end-to-end pipeline:
          1. init  - session 0, two files
          2. sync  - session 1, one file changed + one new file
          3. verify --level full - must exit 0
          4. restore - reconstructed tree matches session-1 source byte-for-byte
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "alpha.txt").write_bytes(b"The quick brown fox jumps over the lazy dog.\n" * 100)
    (source / "beta.bin").write_bytes(bytes(range(256)) * 16)

    iso = tmp_path / "archive.iso"

    # Step 1: init
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))
    assert rc == 0, "init must exit 0"

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 1

    # Step 2: sync - modify alpha, add gamma
    (source / "alpha.txt").write_bytes(b"The quick brown fox jumps over the LAZY dog.\n" * 100)
    (source / "gamma.txt").write_bytes(b"new file added in session 1")

    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))
    assert rc == 0, "sync must exit 0"

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 2

    # Step 3: verify --level full
    rc = dispatch(_make_args(command="verify", level="full", test_iso=str(iso)))
    assert rc == 0, "verify --level full must exit 0 on clean archive"

    # Step 4: restore and compare byte-for-byte
    dest = tmp_path / "restored"
    rc = dispatch(_make_args(command="restore", dest=str(dest), test_iso=str(iso), force=True))
    assert rc == 0, "restore must exit 0"

    expected = {
        "alpha.txt": (source / "alpha.txt").read_bytes(),
        "beta.bin":  (source / "beta.bin").read_bytes(),
        "gamma.txt": (source / "gamma.txt").read_bytes(),
    }
    for name, data in expected.items():
        restored = (dest / name).read_bytes()
        assert restored == data, f"{name}: restored bytes do not match source"


def test_dry_run_does_not_modify_iso(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        _run_dry_run must exit 0 and leave the ISO file unmodified (same mtime
        and size as before the call). The test calls _run_dry_run directly.
        It passes a test ISO path on the args namespace.
    """
    from oddarchiver.cli import _run_dry_run

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"initial")

    iso = tmp_path / "dry.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "a.txt").write_bytes(b"modified content for dry run")

    before_stat = iso.stat()
    args = _make_args(command="sync", source=str(source), test_iso=str(iso), dry_run=False)
    rc = _run_dry_run(args, is_init=False)
    after_stat = iso.stat()

    assert rc == 0
    assert after_stat.st_mtime == before_stat.st_mtime, "ISO mtime must not change on dry-run"
    assert after_stat.st_size == before_stat.st_size, "ISO size must not change on dry-run"


def test_sigint_during_staging_removes_staging_dir(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        If SIGINT arrives during the file scan inside build_staging, the
        staging directory must be cleaned up before KeyboardInterrupt propagates.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "file.txt").write_bytes(b"some content")

    from oddarchiver.disc import DiscInfo
    from unittest.mock import MagicMock

    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=1,
        remaining_bytes=25 * 2 ** 30,
        used_bytes=0,
        label="TEST",
    )
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    expected_staging = staging_root / "oddarchiver_staging_003"

    real_hash = session_mod.hash_file

    def hash_then_signal(path, chunk_size, digest=None):
        session_mod._sigint_received = True
        return real_hash(path, chunk_size, digest)

    _assert_staging_sigint(
        3, source, backend, cache, staging_root, hash_then_signal, real_hash,
    )

    assert not expected_staging.exists(), "staging dir must be removed after SIGINT"


def test_interrupted_burn_does_not_update_cache(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        If backend.append() raises (simulated burn failure), cache.put must
        never be called. Verified by patching CacheManager.put and asserting
        it is not invoked when the burn fails.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"version 1")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "b.txt").write_bytes(b"added in session 1")

    from oddarchiver.cache import CacheManager
    put_calls: list = []
    original_put = CacheManager.put

    def tracking_put(self, *args, **kwargs):
        put_calls.append(args)
        return original_put(self, *args, **kwargs)

    def failing_append(self, staging, label, expected_session_count=None):
        raise RuntimeError("Simulated growisofs failure")

    with patch.object(ISOBackend, "append", failing_append), \
         patch.object(CacheManager, "put", tracking_put):
        rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))

    assert rc == 1, "failed burn must exit 1"
    assert not put_calls, "cache.put must not be called when burn fails"


if __name__ == "__main__":
    pass
