"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Stress and scenario tests: binary mutation, delta explosion, repeated restore,
    cache bypass, staging recovery, passphrase errors, history/status consistency,
    empty directory sync, large file, and gold-standard round-trip.
"""
# Imports
import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.cli import dispatch, _run_history, _run_status
from oddarchiver.crypto import NullCrypto, PassphraseCrypto
from oddarchiver.delta import compute_delta, delta_or_full
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import read_manifest
from oddarchiver.restore import restore
from oddarchiver.session import build_staging
from oddarchiver.verify import verify

# Globals
SMALL_DISC = 200 * 2**20   # 200 MiB — enough for large-file and delta-explosion tests


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs — field overrides
    Output: argparse.Namespace with sensible defaults
    """
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "STRESS",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "200mb",
        "prefill": None,
        "session": None,
        "force": True,
        "level": "fast",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Tests


def test_3_1_binary_mutation_uses_delta(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Create 5 MB random binary, init, mutate 1 KB, verify delta_or_full
        picks delta (delta size < full size).
    """
    source = tmp_path / "src"
    source.mkdir()
    original = os.urandom(5 * 2**20)
    f = source / "big.bin"
    f.write_bytes(original)

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    mutated = bytearray(original)
    mutated[2 * 2**20 : 2 * 2**20 + 1024] = os.urandom(1024)
    f.write_bytes(bytes(mutated))

    new_path = source / "big.bin"
    kind, blob = delta_or_full(original, new_path)
    delta_blob = compute_delta(original, new_path)

    assert kind == "delta", "only 1 KB changed; delta should be smaller than full"
    assert len(delta_blob) < len(original), "delta must be smaller than full content"


def test_3_2_delta_explosion_restore_matches_source(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Append one line 10 times (10 sequential sessions). Restore and assert
        the file matches the source after all syncs.
    """
    source = tmp_path / "src"
    source.mkdir()
    f = source / "log.txt"
    f.write_bytes(b"initial line\n")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    for i in range(10):
        with open(f, "ab") as fh:
            fh.write(f"line {i}\n".encode())
        rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))
        assert rc == 0, f"sync {i} failed"

    dest = tmp_path / "dest"
    rc = dispatch(_make_args(command="restore", dest=str(dest), test_iso=str(iso), force=True))
    assert rc == 0
    assert (dest / "log.txt").read_bytes() == f.read_bytes()


def test_4_1_repeated_restore_stability(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Init + sync once. Restore twice to different temp dirs. Assert
        diff -r dir1 dir2 is empty (deterministic restore).
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello restore")
    (source / "b.bin").write_bytes(os.urandom(512))

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "a.txt").write_bytes(b"hello restore updated")
    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0

    dest1 = tmp_path / "dest1"
    dest2 = tmp_path / "dest2"
    rc1 = dispatch(_make_args(command="restore", dest=str(dest1), test_iso=str(iso), force=True))
    rc2 = dispatch(_make_args(command="restore", dest=str(dest2), test_iso=str(iso), force=True))
    assert rc1 == 0
    assert rc2 == 0

    result = subprocess.run(["diff", "-r", str(dest1), str(dest2)], capture_output=True)
    assert result.returncode == 0, f"restore not deterministic:\n{result.stdout.decode()}"


def test_4_2_cache_bypass_produces_identical_restore(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Init + sync with cache enabled vs no_cache=True. Restore from both
        ISOs; assert file contents are identical.
    """
    src_a = tmp_path / "src_a"
    src_a.mkdir()
    content = b"same content in both paths"
    (src_a / "file.txt").write_bytes(content)

    src_b = tmp_path / "src_b"
    src_b.mkdir()
    (src_b / "file.txt").write_bytes(content)

    iso_a = tmp_path / "with_cache.iso"
    iso_b = tmp_path / "no_cache.iso"

    assert dispatch(_make_args(command="init", source=str(src_a), test_iso=str(iso_a))) == 0
    assert dispatch(_make_args(command="sync", source=str(src_a), test_iso=str(iso_a))) == 0

    assert dispatch(_make_args(command="init", source=str(src_b), test_iso=str(iso_b))) == 0
    assert dispatch(_make_args(command="sync", source=str(src_b), test_iso=str(iso_b), no_cache=True)) == 0

    dest_a = tmp_path / "dest_a"
    dest_b = tmp_path / "dest_b"
    rc_a = dispatch(_make_args(command="restore", dest=str(dest_a), test_iso=str(iso_a), force=True))
    rc_b = dispatch(_make_args(command="restore", dest=str(dest_b), test_iso=str(iso_b), force=True))
    assert rc_a == 0
    assert rc_b == 0

    assert (dest_a / "file.txt").read_bytes() == (dest_b / "file.txt").read_bytes()


def test_5_1_interrupted_sync_recovery(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Manually create a stale oddarchiver_staging_001 dir then run
        build_staging for session 1. Assert the stale dir is gone and
        session completes successfully.
    """
    from oddarchiver.disc import DiscInfo
    from unittest.mock import MagicMock

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello world")

    staging_root = tmp_path / "staging_root"
    staging_root.mkdir()

    stale_dir = staging_root / "oddarchiver_staging_001"
    stale_dir.mkdir()
    sentinel = stale_dir / "stale_sentinel.txt"
    sentinel.write_text("leftover from prior crash")

    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=1,
        remaining_bytes=25 * 2**30,
        used_bytes=0,
        label="STRESS",
    )
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""

    staging = build_staging(
        session_n=1,
        source=source,
        disc_state={},
        backend=backend,
        cache=cache,
        crypto=NullCrypto(),
        _staging_root=staging_root,
    )
    try:
        assert not sentinel.exists(), "stale sentinel must be removed by build_staging"
        assert (staging / "session_001" / "manifest.json").exists()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def test_5_2_staging_dir_cleanup_on_sigint(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Simulate SIGINT by setting _sigint_received before the scan finishes.
        Assert no oddarchiver_staging_* dirs remain in the staging root.
    """
    import oddarchiver.session as session_mod
    from oddarchiver.disc import DiscInfo
    from unittest.mock import MagicMock

    source = tmp_path / "src"
    source.mkdir()
    (source / "file.txt").write_bytes(b"content")

    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=0,
        remaining_bytes=25 * 2**30,
        used_bytes=0,
        label="STRESS",
    )
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""

    staging_root = tmp_path / "staging"
    staging_root.mkdir()

    real_sha256 = session_mod._sha256_file

    def sha256_then_sigint(path: Path) -> str:
        session_mod._sigint_received = True
        return real_sha256(path)

    original_sha = session_mod._sha256_file
    session_mod._sha256_file = sha256_then_sigint
    try:
        with pytest.raises(KeyboardInterrupt):
            build_staging(
                session_n=0,
                source=source,
                disc_state={},
                backend=backend,
                cache=cache,
                crypto=NullCrypto(),
                _staging_root=staging_root,
            )
    finally:
        session_mod._sha256_file = original_sha

    leftover = list(staging_root.glob("oddarchiver_staging_*"))
    assert leftover == [], f"staging dirs not cleaned up: {leftover}"


def test_6_1_wrong_passphrase_restore_raises(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Init with PassphraseCrypto("correct"), sync, then restore with
        PassphraseCrypto("wrong"). Assert it raises an exception (no silent
        corrupt output).
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "secret.txt").write_bytes(b"confidential data")

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)

    correct_crypto = PassphraseCrypto("correct")
    wrong_crypto = PassphraseCrypto("wrong")

    cache = CacheManager(tmp_path / "cache")
    staging = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        backend=backend,
        cache=cache,
        crypto=correct_crypto,
        _staging_root=tmp_path / "staging",
    )
    try:
        from oddarchiver.cli import _patch_manifest, _encryption_block
        _patch_manifest(staging, 0, "STRESS", _encryption_block(correct_crypto))
        backend.init(staging, "STRESS", expected_session_count=0)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    dest = tmp_path / "dest"
    restored_count, failure_count = restore(dest, backend, wrong_crypto)
    assert failure_count > 0, "wrong passphrase must produce failures, not silent success"
    assert restored_count == 0, "no file should be restored with wrong passphrase"
    assert not (dest / "secret.txt").exists(), "no file must be written with wrong passphrase"


def test_6_2_encrypted_storage_differs_from_plaintext(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Init with PassphraseCrypto, sync a file. Read the raw blob from the
        ISO staging dir. Assert the blob bytes differ from the plaintext source.
    """
    source = tmp_path / "src"
    source.mkdir()
    plaintext = b"this is the plaintext content that must not appear on disc"
    (source / "data.txt").write_bytes(plaintext)

    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    crypto = PassphraseCrypto("mypassphrase")

    cache = CacheManager(tmp_path / "cache")
    staging = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        backend=backend,
        cache=cache,
        crypto=crypto,
        _staging_root=tmp_path / "staging",
    )
    try:
        from oddarchiver.cli import _patch_manifest, _encryption_block
        _patch_manifest(staging, 0, "STRESS", _encryption_block(crypto))
        backend.init(staging, "STRESS", expected_session_count=0)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    blob = backend.read_path("session_000/full/data.txt")
    assert blob != plaintext, "encrypted blob must not equal plaintext"
    assert plaintext not in blob, "plaintext must not appear verbatim in ciphertext"


def test_7_1_history_status_session_count_consistent(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        Init + 2 syncs. _run_history and _run_status must report session count
        matching the number of manifest files on disc.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"version 1")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "a.txt").write_bytes(b"version 2")
    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0

    (source / "b.txt").write_bytes(b"new file")
    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    manifest_count = len(list(backend._sessions_root.glob("session_*/manifest.json")))

    capsys.readouterr()
    _run_history(_make_args(command="history", test_iso=str(iso)))
    history_out = capsys.readouterr().out

    _run_status(_make_args(command="status", test_iso=str(iso)))
    status_out = capsys.readouterr().out

    rows = [
        line for line in history_out.splitlines()
        if line and not line.startswith("Session") and not line.startswith("-")
        and line.strip()
    ]
    assert len(rows) == manifest_count, (
        f"history rows ({len(rows)}) must equal manifest count ({manifest_count})"
    )
    assert str(manifest_count) in status_out, "status must report correct session count"


def test_8_1_empty_directory_sync_exits_0(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Init with one file. Delete all files. Sync. Assert exits 0.
        Either a deletion-only session is written or sync is a no-op.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "will_be_deleted.txt").write_bytes(b"temporary")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "will_be_deleted.txt").unlink()

    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))
    assert rc == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    session_count = backend.mediainfo().session_count
    if session_count == 2:
        manifest_bytes = backend.read_path("session_001/manifest.json")
        import json
        manifest_data = json.loads(manifest_bytes)
        assert manifest_data["deleted"], "deletion-only session must record deleted paths"


def test_8_2_large_file_manifest_and_verify(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Sync a 20 MB random binary. Assert manifest records the file and
        verify --level checksum exits 0.
    """
    source = tmp_path / "src"
    source.mkdir()
    large_content = os.urandom(20 * 2**20)
    (source / "large.bin").write_bytes(large_content)

    iso = tmp_path / "test.iso"
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))
    assert rc == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    import json
    manifest_bytes = backend.read_path("session_000/manifest.json")
    manifest_data = json.loads(manifest_bytes)
    assert any(e["path"] == "large.bin" for e in manifest_data["entries"])

    rc = dispatch(_make_args(command="verify", level="checksum", test_iso=str(iso)))
    assert rc == 0


def test_9_gold_standard_full_verify_and_restore(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Init + 2 syncs with file changes. Run verify --level full.
        Restore to a temp dir. diff -r source restore_dir must be empty.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "alpha.txt").write_bytes(b"The quick brown fox.\n" * 200)
    (source / "beta.bin").write_bytes(bytes(range(256)) * 32)

    iso = tmp_path / "archive.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "alpha.txt").write_bytes(b"The quick brown fox UPDATED.\n" * 200)
    (source / "gamma.txt").write_bytes(b"new file for session 1\n")
    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0

    (source / "gamma.txt").write_bytes(b"gamma updated in session 2\n")
    (source / "delta.bin").write_bytes(os.urandom(1024))
    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0

    rc = dispatch(_make_args(command="verify", level="full", test_iso=str(iso)))
    assert rc == 0, "verify --level full must exit 0 on clean archive"

    dest = tmp_path / "restored"
    rc = dispatch(_make_args(command="restore", dest=str(dest), test_iso=str(iso), force=True))
    assert rc == 0

    result = subprocess.run(
        ["diff", "-r", str(source), str(dest)],
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"restored tree differs from source:\n{result.stdout.decode()}"
    )
