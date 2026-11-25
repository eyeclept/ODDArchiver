"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Regression tests for disc-aware restore safety: catalog mismatch,
    resume dest checks, pause exit code, spool modes, prompt parsing,
    space checks, and batched disc reads.
"""
# Imports
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.catalog import remember_encryption_mode
from oddarchiver.cli import _run_browse, _run_restore
from oddarchiver.crypto import KeyfileCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest
from oddarchiver.restore_plan import DiscAcquisition, restore_archive
from oddarchiver.restore_state import load_restore_state, state_path
from oddarchiver.volume import VolumeMarker, write_volume_marker
from tests.test_restore_plan import (
    Acquisition, _CRYPTO, _DISC, _ID, _source, _stage, _three_discs,
)

# Globals
logger = logging.getLogger(__name__)
_log = logger


# Functions


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Keep restore-state and CLI cache inside the test directory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache))


def _restore_args(dest: Path, iso: Path, **extra) -> argparse.Namespace:
    """Input: dest, ISO, optional fields. Output: namespace for _run_restore."""
    values = dict(
        dest=str(dest),
        test_iso=str(iso),
        device=None,
        disc_size="20mb",
        session=None,
        force=False,
        paths=None,
        key=None,
        rescue_key=None,
        recover_scan=False,
        label="",
        skip_volumes=None,
        unskip_volumes=None,
        all_disks=True,
        read_mode="sequential",
    )
    values.update(extra)
    return argparse.Namespace(**values)


def _one_disc_sessions(tmp_path, files):
    """Write one marked ISO with one full file per session. Return the ISO path."""
    iso = tmp_path / "onedisc.iso"
    backend = ISOBackend(iso, disc_size=_DISC)
    for n, (path, content) in enumerate(files):
        staging = tmp_path / f"one-stage-{n}"
        session = staging / f"session_{n:03d}"
        session.mkdir(parents=True)
        rel = f"session_{n:03d}/full/{hashlib.sha256(path.encode()).hexdigest()}"
        blob = staging / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(content)
        entry = ManifestEntry(
            path=path, type="full", file=rel,
            result_checksum=hashlib.sha256(content).hexdigest(),
            full_size_bytes=len(content),
        )
        write_manifest(session, Manifest(
            version=1, session=n, timestamp=f"2026-10-15T00:00:{n:02d}Z",
            source="/src", label="VOL0", based_on_session=n - 1 if n else None,
            encryption={}, entries=[entry], deleted=[], manifest_checksum="",
        ), None)
        write_volume_marker(staging, VolumeMarker(_ID, 0, 0, f"2026-10-15T00:00:{n:02d}Z", ""))
        if n == 0:
            backend.init(staging, label="VOL0", expected_session_count=0)
        else:
            backend.append(staging, label="VOL0", expected_session_count=n)
    return iso

def test_restore_archive_creates_private_destination_components(tmp_path):
    """Create a nested destination with mode 0700 and keep its parent mode."""
    iso = _one_disc_sessions(tmp_path, [("one/two/file.txt", b"archive data")])
    backend, catalog, manifests = _source(iso)
    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    dest = parent / "dest" / "nested"
    old_umask = os.umask(0o022)
    try:
        result = restore_archive(
            dest, backend, _CRYPTO, catalog, manifests=manifests,
        )
    finally:
        os.umask(old_umask)
    assert result.restored == 1
    assert result.failures == 0
    assert (dest / "one" / "two" / "file.txt").read_bytes() == b"archive data"
    assert parent.stat().st_mode & 0o777 == 0o755
    for directory in (
        parent / "dest", dest, dest / "one", dest / "one" / "two",
    ):
        assert directory.stat().st_mode & 0o777 == 0o700


def test_catalog_checksum_mismatch_leaves_dest_untouched(tmp_path, capsys):
    discs = _three_discs(tmp_path)
    backend, catalog, manifests = _source(discs[2])
    catalog.history["a.txt"][-1].result_checksum = "ab" * 32
    dest = tmp_path / "mismatch"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, paths=["a.txt"],
        manifests=manifests, acquisition=Acquisition(discs, [1, 0]),
    )
    err = capsys.readouterr().err
    assert result.failures >= 1
    assert not (dest / "a.txt").exists()
    assert "does not match the catalog" in err


def test_deleted_dest_is_restored_on_resume(tmp_path):
    discs = _three_discs(tmp_path)
    backend, catalog, manifests = _source(discs[2])
    dest = tmp_path / "resume-missing"
    first = restore_archive(
        dest, backend, _CRYPTO, catalog, paths=["a.txt"],
        manifests=manifests, acquisition=Acquisition(discs, [1, 0]),
    )
    assert first.failures == 0
    assert (dest / "a.txt").read_bytes() == b"A-new"
    (dest / "a.txt").unlink()
    backend, catalog, manifests = _source(discs[2])
    second = restore_archive(
        dest, backend, _CRYPTO, catalog, paths=["a.txt"],
        manifests=manifests, acquisition=Acquisition(discs, [1]),
    )
    assert second.failures == 0
    assert (dest / "a.txt").read_bytes() == b"A-new"


def test_changed_dest_bytes_are_restored_on_resume(tmp_path):
    """Overwrite dest bytes after a full restore. Resume must write the catalog copy."""
    discs = _three_discs(tmp_path)
    backend, catalog, manifests = _source(discs[2])
    dest = tmp_path / "resume-changed"
    first = restore_archive(
        dest, backend, _CRYPTO, catalog, paths=["a.txt"],
        manifests=manifests, acquisition=Acquisition(discs, [1, 0]),
    )
    assert first.failures == 0
    assert (dest / "a.txt").read_bytes() == b"A-new"
    (dest / "a.txt").write_bytes(b"tampered-locally")
    backend, catalog, manifests = _source(discs[2])
    second = restore_archive(
        dest, backend, _CRYPTO, catalog, paths=["a.txt"],
        manifests=manifests, acquisition=Acquisition(discs, [1]),
    )
    assert second.failures == 0
    assert (dest / "a.txt").read_bytes() == b"A-new"


def test_cli_quit_exits_3_and_saves_state(tmp_path, monkeypatch):
    discs = _three_discs(tmp_path)
    remember_encryption_mode(_ID, "none")
    dest = tmp_path / "cli-paused"
    monkeypatch.setattr(
        "oddarchiver.restore_plan.DiscAcquisition.acquire",
        lambda self, needed, catalog, archive_id: (None, None, True),
    )
    rc = _run_restore(_restore_args(dest, discs[2], paths=["a.txt"]))
    assert rc == 3
    state = load_restore_state(dest, _ID)
    assert state is not None
    assert not (dest / "a.txt").exists()


def test_browse_r_paused_exits_3(tmp_path, monkeypatch):
    """Drive browse r through restore_archive, then q. Exit 3. Dest bytes stay."""
    discs = _three_discs(tmp_path)
    remember_encryption_mode(_ID, "none")
    dest = tmp_path / "browse-paused"
    dest.mkdir()
    original = b"keep-local"
    (dest / "a.txt").write_bytes(original)

    class _NoImage(DiscAcquisition):
        def __init__(self, initial, image=None, ask=None):
            super().__init__(initial, image=None, ask=ask)

    answers = iter(["t 1", "r", str(dest), "q"])
    monkeypatch.setattr("oddarchiver.restore_plan.DiscAcquisition", _NoImage)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    args = _restore_args(dest, discs[2])
    args.command = "browse"
    args.show_deleted = False
    args.source = None
    rc = _run_browse(args)
    assert rc == 3
    assert (dest / "a.txt").read_bytes() == original


def test_spool_symlink_leaves_target_untouched(tmp_path):
    crypto = KeyfileCrypto.from_kek(b"K" * 32)
    original = b"old" * 3000
    updated = original[:-8] + b"new-tail"
    iso0, prior = _stage(tmp_path, 0, {"changed.txt": original}, crypto)
    iso1, _prior = _stage(
        tmp_path, 1, {}, crypto, prior,
        delta={"changed.txt": (original, updated)},
    )
    dest = tmp_path / "spool-link"
    dest.mkdir()
    spool = state_path(dest, _ID).with_suffix(".spool")
    victim = tmp_path / "victim-dir"
    victim.mkdir()
    secret = victim / "secret.txt"
    secret.write_bytes(b"keep")
    spool.symlink_to(victim)
    backend, catalog, manifests = _source(iso1, crypto)
    with pytest.raises(RuntimeError, match="Unsafe restore spool"):
        restore_archive(
            dest, backend, crypto, catalog, manifests=manifests,
            acquisition=Acquisition([iso0, iso1], ["q"]),
        )
    assert secret.read_bytes() == b"keep"
    assert spool.is_symlink()


def test_blob_symlink_leaves_target_untouched(tmp_path):
    crypto = KeyfileCrypto.from_kek(b"K" * 32)
    original = b"old" * 3000
    updated = original[:-8] + b"new-tail"
    iso0, prior = _stage(tmp_path, 0, {"changed.txt": original}, crypto)
    iso1, _prior = _stage(
        tmp_path, 1, {}, crypto, prior,
        delta={"changed.txt": (original, updated)},
    )
    discs = [iso0, iso1]
    backend, catalog, manifests = _source(iso1, crypto)
    dest = tmp_path / "blob-link"
    paused = restore_archive(
        dest, backend, crypto, catalog, manifests=manifests,
        acquisition=Acquisition(discs, ["q"]),
    )
    assert paused.paused
    spool = state_path(dest, _ID).with_suffix(".spool")
    rel = f"session_000/full/{hashlib.sha256(b'changed.txt').hexdigest()}"
    victim = tmp_path / "victim-blob"
    victim.write_bytes(b"keep-blob")
    target = spool / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(victim)
    backend, catalog, manifests = _source(iso1, crypto)
    with pytest.raises(RuntimeError, match="Unsafe restore"):
        restore_archive(
            dest, backend, crypto, catalog, manifests=manifests,
            acquisition=Acquisition(discs, [0]),
        )
    assert victim.read_bytes() == b"keep-blob"
    assert target.is_symlink()


def test_tampered_spool_blob_fails_resume(tmp_path):
    """Pause after a cross-disc spool, flip the spool blob, then resume."""
    crypto = KeyfileCrypto.from_kek(b"K" * 32)
    original = b"old" * 3000
    updated = original[:-8] + b"new-tail"
    iso0, prior = _stage(tmp_path, 0, {"changed.txt": original}, crypto)
    iso1, _prior = _stage(
        tmp_path, 1, {}, crypto, prior,
        delta={"changed.txt": (original, updated)},
    )
    discs = [iso0, iso1]
    backend, catalog, manifests = _source(iso1, crypto)
    dest = tmp_path / "spool-tamper"
    dest.mkdir()
    local = b"local-copy"
    (dest / "changed.txt").write_bytes(local)
    paused = restore_archive(
        dest, backend, crypto, catalog, manifests=manifests,
        acquisition=Acquisition(discs, ["q"]),
    )
    assert paused.paused
    assert (dest / "changed.txt").read_bytes() == local
    spool = state_path(dest, _ID).with_suffix(".spool")
    rel = f"session_001/deltas/{hashlib.sha256(b'changed.txt').hexdigest()}"
    blob = spool / rel
    assert blob.is_file()
    blob.write_bytes(bytes(byte ^ 0xFF for byte in blob.read_bytes()))
    backend, catalog, manifests = _source(iso1, crypto)
    second = restore_archive(
        dest, backend, crypto, catalog, manifests=manifests,
        acquisition=Acquisition(discs, [0]),
    )
    assert second.failures >= 1
    restored = dest / "changed.txt"
    assert (not restored.exists()) or restored.read_bytes() == local


def test_understated_manifest_size_does_not_bypass_space_check(tmp_path, monkeypatch):
    content = b"X" * 8000
    iso = _one_disc_sessions(tmp_path, [("big.txt", content)])
    backend, catalog, manifests = _source(iso)
    catalog.history["big.txt"][-1].full_size_bytes = 1
    for manifest in manifests:
        for entry in manifest.entries:
            entry.full_size_bytes = 1
    monkeypatch.setattr(
        "oddarchiver.restore_plan.shutil.disk_usage",
        lambda path: type("Usage", (), {"free": 80})(),
    )
    dest = tmp_path / "space"
    with pytest.raises(RuntimeError, match="Not enough local space"):
        restore_archive(
            dest, backend, _CRYPTO, catalog, paths=["big.txt"],
            manifests=manifests, acquisition=Acquisition([iso], []),
        )
    assert not (dest / "big.txt").exists()


def test_eof_at_first_prompt_pauses(tmp_path, monkeypatch):
    discs = _three_discs(tmp_path)
    backend, catalog, manifests = _source(discs[2])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    dest = tmp_path / "eof-first"

    def ask(prompt):
        raise EOFError

    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=DiscAcquisition(backend, ask=ask),
    )
    assert result.paused
    assert result.failures == 0
    assert not (dest / "latest.txt").exists()
    assert load_restore_state(dest, _ID) is not None


def test_enter_at_first_prompt_restores_all_present(tmp_path, monkeypatch):
    """Enter at the first disc prompt means all discs are present. Restore runs."""
    discs = _three_discs(tmp_path)
    backend, catalog, manifests = _source(discs[2])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    dest = tmp_path / "enter-all"
    prompts = []
    replies = iter(["", "m 0", "m 1"])

    def ask(prompt):
        prompts.append(prompt)
        return next(replies)

    result = restore_archive(
        dest, backend, _CRYPTO, catalog, paths=["latest.txt"],
        manifests=manifests, acquisition=DiscAcquisition(backend, ask=ask),
    )
    assert any("Which discs do you have" in prompt for prompt in prompts)
    assert not result.paused
    assert result.failures == 0
    assert (dest / "latest.txt").read_bytes() == b"latest"


def test_bad_prompt_input_reprompts(tmp_path, capsys, monkeypatch):
    discs = _three_discs(tmp_path)
    backend, catalog, manifests = _source(discs[2])
    answers = iter(["m ²", "m " + "1" * 5000, "q"])
    acq = DiscAcquisition(backend, ask=lambda prompt: next(answers))
    first = acq.acquire({1: 1}, catalog, _ID)
    second = acq.acquire({1: 1}, catalog, _ID)
    third = acq.acquire({1: 1}, catalog, _ID)
    err = capsys.readouterr().err
    assert first == (None, None, False)
    assert second == (None, None, False)
    assert third == (None, None, True)
    assert err.count("at most six digits") >= 2

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    feed = ["²", "1" * 5000]

    def ask(prompt):
        if feed:
            return feed.pop(0)
        raise EOFError

    dest = tmp_path / "bad-first"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=DiscAcquisition(backend, ask=ask),
    )
    assert result.paused
    assert "at most six digits" in capsys.readouterr().err


def _track_iso_blob_io(monkeypatch):
    """Record blob paths from extract_paths batches."""
    events: list[list[str]] = []
    original = ISOBackend.extract_paths

    def track(self, paths, dest_root, max_bytes=None):
        found = [path for path in paths if "/full/" in path or "/deltas/" in path]
        if found:
            events.append(found)
        return original(self, paths, dest_root, max_bytes=max_bytes)

    monkeypatch.setattr(ISOBackend, "extract_paths", track)
    return events


def test_three_session_disc_sequential_uses_one_blob_batch_per_session(tmp_path, monkeypatch):
    """Sequential restore reads each session blob once."""
    files = [("a.txt", b"A" * 100), ("b.txt", b"B" * 100), ("c.txt", b"C" * 100)]
    iso = _one_disc_sessions(tmp_path, files)
    backend, catalog, manifests = _source(iso)
    seen = _track_iso_blob_io(monkeypatch)
    dest = tmp_path / "batched"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=Acquisition([iso], []), read_mode="sequential",
    )
    assert result.failures == 0
    assert {name: (dest / name).read_bytes() for name, _content in files} == dict(files)
    flat = [path for batch in seen for path in batch]
    assert len(flat) == len(set(flat)) == 3
    assert len(seen) == 3
    assert all(len(batch) == 1 for batch in seen)


def test_three_session_disc_full_copy_uses_one_blob_batch(tmp_path, monkeypatch):
    """Full-copy restore extracts all session blobs in one batch."""
    files = [("a.txt", b"A" * 100), ("b.txt", b"B" * 100), ("c.txt", b"C" * 100)]
    iso = _one_disc_sessions(tmp_path, files)
    backend, catalog, manifests = _source(iso)
    seen = _track_iso_blob_io(monkeypatch)
    dest = tmp_path / "full-copy"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=Acquisition([iso], []), read_mode="full-copy",
    )
    assert result.failures == 0
    assert {name: (dest / name).read_bytes() for name, _content in files} == dict(files)
    assert len(seen) == 1
    assert len(seen[0]) == len(set(seen[0])) == 3



def test_suspect_manifest_counts_as_failure(tmp_path, capsys):
    """A tampered on-disc manifest is SUSPECT and counts as a restore failure."""
    discs = _three_discs(tmp_path)
    backend, catalog, _manifests = _source(discs[2])
    manifest_path = discs[1].with_suffix(".d") / "session_001" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["manifest_checksum"] = "00" * 32
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    dest = tmp_path / "suspect"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog,
        manifests=None, acquisition=Acquisition(discs, [1, 0]),
    )
    err = capsys.readouterr().err
    assert result.failures >= 1
    assert "SUSPECT" in err
    assert "session 1" in err
    assert "Disc 1" in err
    assert (dest / "latest.txt").read_bytes() == b"latest"
    assert (dest / "old-only.txt").read_bytes() == b"old-only"
    assert not (dest / "a.txt").exists()
