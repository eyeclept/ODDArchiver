"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Cache migration, permissions, and bounded update regression tests.
"""
# Imports
import json
import logging
import os
import stat
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

import oddarchiver.cache as cache_module
from oddarchiver.cache import CacheManager, _blob_path
from oddarchiver.cli import _update_cache
from oddarchiver.crypto import NullCrypto
from oddarchiver.session import _print_bar, _sidecar_dir

# Globals
logger = logging.getLogger(__name__)

# Functions


def _assert_private_migrated_tree(root: Path) -> None:
    """
    Input: migrated cache root
    Output: None
    Details: Check opaque names and private permissions on every item.
    """
    for item in root.rglob("*"):
        assert "secret" not in item.name and "nested.txt" not in item.name
        mode = stat.S_IMODE(item.stat().st_mode)
        assert mode == (0o700 if item.is_dir() else 0o600)


def test_old_cache_migrates_without_plaintext_paths(tmp_path, caplog):
    """
    Input: an old-layout cache with two nested file paths
    Output: readable encrypted bytes and private opaque filenames
    Details: A bad old entry is dropped without stopping migration.
    """
    root = tmp_path / "cache"
    old = root / "blobs" / "2" / "secret" / "nested.txt.blob"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"ciphertext")
    bad = root / "blobs" / "2" / "secret" / "bad.txt.blob"
    bad.write_bytes(b"bad")
    (root / "cache_manifest.json").write_text(json.dumps({
        "2:secret/nested.txt": {"size": 10},
        "2:secret/bad.txt": {"size": 12},
    }))
    with caplog.at_level(logging.WARNING):
        cache = CacheManager(root)
        assert cache.get("secret/nested.txt", 2) == b"ciphertext"
        assert cache.get("secret/bad.txt", 2) is None
    assert any("dropping corrupt old cache entry" in r.message for r in caplog.records)
    assert not old.exists() and not bad.exists()
    assert _blob_path(root, "secret/nested.txt", 2).read_bytes() == b"ciphertext"
    assert json.loads((root / "cache_manifest.json").read_text()).keys() == {"2:secret/nested.txt"}
    _assert_private_migrated_tree(root)


def test_interrupted_migration_resumes(tmp_path, monkeypatch):
    """Input: a move that succeeded before index save failed. Output: next use reads the blob."""
    root = tmp_path / "cache"
    old = root / "blobs" / "1" / "private.txt.blob"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"encrypted")
    (root / "cache_manifest.json").write_text(json.dumps({"1:private.txt": {"size": 9}}))
    original = cache_module._save_manifest

    def interrupted(*args):
        raise OSError("simulated interruption")

    monkeypatch.setattr(cache_module, "_save_manifest", interrupted)
    with pytest.raises(OSError, match="simulated interruption"):
        CacheManager(root).get("private.txt", 1)
    monkeypatch.setattr(cache_module, "_save_manifest", original)
    assert CacheManager(root).get("private.txt", 1) == b"encrypted"
    assert not old.exists()
    index = json.loads((root / "cache_manifest.json").read_text())
    assert index["1:private.txt"]["blob"] == _blob_path(root, "private.txt", 1).name


def test_cache_update_streams_one_payload_and_commits_once(tmp_path, monkeypatch):
    """
    Input: several full entries in one staged session
    Output: one index save, with no list of all live payloads
    Details: Track each payload until the cache consumes it.
    """
    staging = tmp_path / "staging"
    for i in range(8):
        target = staging / "session_001" / "full" / str(i)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes([i]) * 256)
    entries = [SimpleNamespace(type="full", path=f"secret/{i}", file=f"session_001/full/{i}") for i in range(8)]
    cache = CacheManager(tmp_path / "cache")
    save = cache_module._save_manifest
    saves = []

    def count_save(*args):
        saves.append(1)
        return save(*args)

    class Payload(bytearray):
        pass

    original = Path.read_bytes
    live = []
    peak = 0

    def tracked_read(self):
        nonlocal peak
        data = original(self)
        if self.parent == staging / "session_001" / "full":
            result = Payload(data)
            live.append(weakref.ref(result))
            peak = max(peak, sum(ref() is not None for ref in live))
            return result
        return data

    monkeypatch.setattr(cache_module, "_save_manifest", count_save)
    monkeypatch.setattr(Path, "read_bytes", tracked_read)
    _update_cache(cache, staging, 1, entries, NullCrypto())
    assert peak <= 2
    assert saves == [1]
    for i in range(8):
        assert cache.get(f"secret/{i}", 1) == bytes([i]) * 256


def test_sidecar_refuses_symlink_and_bar_escapes_controls(tmp_path, capsys):
    """Input: a linked sidecar and an untrusted label. Output: refusal and escaped text."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "sidecar"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="Unsafe delta cache sidecar directory"):
        _sidecar_dir(link)
    _print_bar(1, 1, suffix="name\x1b[31m\nsecret")
    shown = capsys.readouterr().out
    assert "\x1b" not in shown and "\n" not in shown
    assert "\\u001b" in shown and "\\u000a" in shown
