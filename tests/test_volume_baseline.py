"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for the cross-volume baseline sidecar. Covers write/read of
    volume_baseline.json, seed replay in build_disc_state/origin, and
    CLI-dispatch sync/ls/browse/history/status against only the latest
    volume of an encrypted multi-volume archive.
"""
# Imports
import argparse
import hashlib
import logging
import re
from pathlib import Path

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.cli import _read_disc_manifests, dispatch
from oddarchiver.crypto import KeyfileCrypto, generate_keyfile
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import (
    Manifest,
    ManifestEntry,
    build_disc_origin,
    build_disc_state,
    validate_disc_read_path,
)
from oddarchiver.volume import (
    read_volume_baseline,
    write_volume_baseline,
)

# Globals
SMALL_DISC = 10 * 2**20
ROOMY_DISC = 50 * 2**20
_FILE_BYTES = 80_000
_DISC_THAT_SPLITS = 100_000
_TIMESTAMP = "2026-04-19T12:00:00Z"
_REPEAT = b"The quick brown fox jumps over the lazy dog.\n"
_SUMMARY_RE = re.compile(
    r"(\d+) new, (\d+) changed, (\d+) deleted, (\d+) unchanged"
)


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides for the Namespace
    Output: argparse.Namespace with sensible defaults for test commands
    """
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "VBASE",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "50mb",
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
        "mirror": None,
        "recover_scan": False,
        "paths": None,
        "show_deleted": False,
        "path": None,
        "rescue_key": None,
        "skip_volumes": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    """Point cache and ledger at tmp_path so tests do not share home state."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        "oddarchiver.cli.CacheManager",
        lambda *a, **k: CacheManager(cache_dir),
    )
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    monkeypatch.setattr("oddarchiver.ledger.ledger_dir", lambda: ledger)
    return cache_dir


def _make_staging(tmp_path: Path) -> Path:
    """Build a minimal staging directory with one session blob."""
    staging = tmp_path / "staging"
    blob_dir = staging / "session_000" / "full"
    blob_dir.mkdir(parents=True)
    blob_name = hashlib.sha256(b"0:hello.txt").hexdigest()
    (blob_dir / blob_name).write_bytes(b"hello world")
    return staging


def _make_manifest(session: int = 0, entries=None, deleted=None) -> Manifest:
    """Build a minimal valid Manifest for seed-replay tests."""
    return Manifest(
        version=1,
        session=session,
        timestamp=_TIMESTAMP,
        source="/home/user/docs",
        label="TEST",
        based_on_session=None if session == 0 else session - 1,
        encryption={"mode": "none"},
        entries=entries or [],
        deleted=deleted or [],
        manifest_checksum="",
    )


def _make_entry(path: str, checksum: str = "abc123") -> ManifestEntry:
    """Build a minimal full ManifestEntry."""
    return ManifestEntry(
        path=path,
        type="full",
        result_checksum=checksum,
        full_size_bytes=100,
    )


def _summary_counts(out: str) -> tuple[int, int, int, int]:
    """Parse the sync new/changed/deleted/unchanged summary line."""
    match = _SUMMARY_RE.search(out)
    assert match is not None, out
    return tuple(int(g) for g in match.groups())


def _hide_volume0(tmp_path: Path, iso: Path) -> None:
    """Rename volume 0's ISO and sessions dir so later commands cannot open it."""
    iso.rename(tmp_path / f"hidden-{iso.name}")
    sessions = iso.with_suffix(".d")
    if sessions.exists():
        sessions.rename(tmp_path / f"hidden-{sessions.name}")
    meta = iso.with_name(iso.name + ".meta.json")
    if meta.exists():
        meta.rename(tmp_path / f"hidden-{meta.name}")


def _two_volume_archive(
    tmp_path: Path,
    monkeypatch,
    *,
    encrypt: bool = True,
    a_bytes: bytes | None = None,
    b_bytes: bytes | None = None,
    label: str = "VBASE2",
) -> tuple[Path, Path, Path, Path | None]:
    """
    Input:  tmp_path, monkeypatch, optional file bytes and encrypt flag
    Output: (source, vol0_iso, vol1_iso, keyfile_or_None)
    Details:
        Two 80 KB files on a 100000-byte ISO force a 2-volume split.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(a_bytes if a_bytes is not None else b"A" * _FILE_BYTES)
    (source / "b.bin").write_bytes(b_bytes if b_bytes is not None else b"B" * _FILE_BYTES)
    keyfile = None
    kwargs = {
        "command": "init",
        "source": str(source),
        "test_iso": str(tmp_path / "archive.iso"),
        "disc_size": str(_DISC_THAT_SPLITS),
        "label": label,
    }
    if encrypt:
        keyfile = tmp_path / "test.key"
        generate_keyfile(str(keyfile))
        kwargs["encrypt"] = "keyfile"
        kwargs["key"] = str(keyfile)
    rc = dispatch(_make_args(**kwargs))
    assert rc == 0
    vol0 = tmp_path / "archive.iso"
    vol1 = tmp_path / "archive.vol1.iso"
    assert vol0.is_file()
    assert vol1.is_file()
    return source, vol0, vol1, keyfile


def _crypto_for_key(keyfile: Path | None) -> KeyfileCrypto | None:
    """Return KeyfileCrypto when a keyfile was used, else None."""
    if keyfile is None:
        return None
    return KeyfileCrypto(str(keyfile))


# Tests


def test_validate_disc_read_path_allows_volume_baseline_json():
    """
    Input:  None
    Output: None
    Details:
        Root-level volume_baseline.json must be a legal disc-read path so
        ISOBackend.read_paths() will accept it.
    """
    assert validate_disc_read_path("volume_baseline.json") == "volume_baseline.json"


def test_write_then_read_baseline_round_trips_via_iso_backend(tmp_path):
    """
    Input:  tmp_path - pytest temp directory
    Output: None
    Details:
        write_volume_baseline() at staging root, burn via ISOBackend.init(),
        then read_volume_baseline() must return equal state and origin dicts.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path)
    state = {"docs/a.txt": "abc123"}
    origin = {"docs/a.txt": 0}
    write_volume_baseline(staging, state, origin)
    backend.init(staging, label="TEST")
    loaded = read_volume_baseline(backend)
    assert loaded is not None
    assert loaded[0] == state
    assert loaded[1] == origin


def test_read_baseline_returns_none_when_absent(tmp_path):
    """
    Input:  tmp_path - pytest temp directory
    Output: None
    Details:
        An ISO that never received volume_baseline.json must read back as
        None, not raise and not invent empty dicts.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path)
    backend.init(staging, label="TEST")
    assert read_volume_baseline(backend) is None


def test_read_baseline_returns_none_when_malformed(tmp_path, caplog):
    """
    Input:  tmp_path - pytest temp directory
            caplog   - pytest log capture
    Output: None
    Details:
        A present but undecodable volume_baseline.json is treated as absent
        and logs a warning.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path)
    (staging / "volume_baseline.json").write_bytes(b"not-json{")
    backend.init(staging, label="TEST")
    with caplog.at_level(logging.WARNING):
        assert read_volume_baseline(backend) is None
    assert any("unreadable volume_baseline.json" in r.message for r in caplog.records)


def test_read_baseline_reraises_backend_failure(caplog):
    """
    Input:  caplog - pytest log capture
    Output: None
    Details:
        A genuine backend/transport failure is logged and re-raised.
        None is reserved for "no baseline on this disc."
    """

    class _FailingBackend:
        def read_paths(self, paths):
            raise RuntimeError("drive failed")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="drive failed"):
            read_volume_baseline(_FailingBackend())
    assert any(
        "Failed to read volume_baseline.json" in r.message for r in caplog.records
    )


def test_build_disc_state_seed_keeps_untouched_paths():
    """
    Input:  None
    Output: None
    Details:
        A seed path never mentioned in later manifests must survive replay.
        A path the later manifest writes must appear with the new checksum.
    """
    seed = {"old.txt": "aaa"}
    m0 = _make_manifest(1, entries=[_make_entry("new.txt", "bbb")])
    state = build_disc_state([m0], seed=seed)
    assert state["old.txt"] == "aaa"
    assert state["new.txt"] == "bbb"


def test_build_disc_state_seed_overwritten_by_later_manifest():
    """
    Input:  None
    Output: None
    Details:
        A later manifest entry for a seeded path must overwrite the seed
        checksum. The caller's seed dict must not be mutated.
    """
    seed = {"a.txt": "old"}
    m0 = _make_manifest(1, entries=[_make_entry("a.txt", "new")])
    state = build_disc_state([m0], seed=seed)
    assert state["a.txt"] == "new"
    assert seed == {"a.txt": "old"}


def test_build_disc_origin_seed_keeps_untouched_paths():
    """
    Input:  None
    Output: None
    Details:
        A seed origin for a path nobody rewrote must survive. A path the
        later manifest writes records that manifest's session number.
    """
    seed = {"old.txt": 0}
    m0 = _make_manifest(2, entries=[_make_entry("new.txt", "bbb")])
    origin = build_disc_origin([m0], seed=seed)
    assert origin["old.txt"] == 0
    assert origin["new.txt"] == 2


def test_build_disc_origin_seed_deleted_path_dropped():
    """
    Input:  None
    Output: None
    Details:
        A path listed in a later manifest's deleted list must be absent
        from the origin map even when it arrived via seed.
    """
    seed = {"gone.txt": 0, "keep.txt": 0}
    m0 = _make_manifest(1, deleted=["gone.txt"])
    origin = build_disc_origin([m0], seed=seed)
    assert "gone.txt" not in origin
    assert origin["keep.txt"] == 0


def test_encrypted_volume2_only_sync_counts_and_exits_0(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        An encrypted 2-volume archive, CLI-dispatch sync pointed at only
        the second volume (volume 0's ISO is renamed away) reports
        0 new / 0 changed / 2 unchanged and exits 0.
    """
    source, vol0, vol1, keyfile = _two_volume_archive(tmp_path, monkeypatch)
    _hide_volume0(tmp_path, vol0)
    capsys.readouterr()
    rc = dispatch(_make_args(
        command="sync",
        source=str(source),
        test_iso=str(vol1),
        disc_size=str(_DISC_THAT_SPLITS),
        key=str(keyfile),
        label="VBASE2",
    ))
    out = capsys.readouterr().out
    assert rc == 0, out
    new, changed, deleted, unchanged = _summary_counts(out)
    assert (new, changed, deleted, unchanged) == (0, 0, 0, 2)


def test_encrypted_volume2_only_ls_browse_history_status(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        ls, browse, history, and status pointed at only the second volume
        of an encrypted 2-volume archive each produce correct, non-empty
        output. restore of the file that lives on volume 2 succeeds.
    """
    source, vol0, vol1, keyfile = _two_volume_archive(tmp_path, monkeypatch)
    _hide_volume0(tmp_path, vol0)
    key = str(keyfile)
    iso = str(vol1)

    capsys.readouterr()
    rc = dispatch(_make_args(command="ls", test_iso=iso, key=key, label="VBASE2"))
    ls_out = capsys.readouterr().out
    assert rc == 0, ls_out
    assert "b.bin" in ls_out
    assert "No sessions found." not in ls_out

    rc = dispatch(_make_args(
        command="history", test_iso=iso, key=key, label="VBASE2",
    ))
    hist_out = capsys.readouterr().out
    assert rc == 0, hist_out
    assert "001" in hist_out
    assert "No sessions found." not in hist_out

    rc = dispatch(_make_args(
        command="status", test_iso=iso, key=key, label="VBASE2",
    ))
    status_out = capsys.readouterr().out
    assert rc == 0, status_out
    assert "Sessions:" in status_out

    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    rc = dispatch(_make_args(
        command="browse", test_iso=iso, key=key, label="VBASE2",
    ))
    browse_out = capsys.readouterr().out
    assert rc == 0, browse_out
    assert "b.bin" in browse_out

    dest = tmp_path / "restored"
    rc = dispatch(_make_args(
        command="restore",
        dest=str(dest),
        test_iso=iso,
        key=key,
        paths=["b.bin"],
        force=True,
        label="VBASE2",
    ))
    assert rc == 0
    assert (dest / "b.bin").read_bytes() == b"B" * _FILE_BYTES


def test_volume1_file_reported_unchanged_on_volume2_sync(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        A file archived only on volume 1 is reported unchanged, not
        re-archived, when volume 2 is synced with an unrelated new file,
        using only volume 2's backend.
    """
    source, vol0, vol1, keyfile = _two_volume_archive(tmp_path, monkeypatch)
    (source / "c.txt").write_bytes(b"brand new")
    _hide_volume0(tmp_path, vol0)
    capsys.readouterr()
    rc = dispatch(_make_args(
        command="sync",
        source=str(source),
        test_iso=str(vol1),
        disc_size="50mb",
        key=str(keyfile),
        label="VBASE2",
    ))
    out = capsys.readouterr().out
    assert rc == 0, out
    new, changed, deleted, unchanged = _summary_counts(out)
    assert (new, changed, deleted, unchanged) == (1, 0, 0, 2)

    crypto = _crypto_for_key(keyfile)
    backend = ISOBackend(vol1, disc_size=ROOMY_DISC)
    manifests = _read_disc_manifests(backend, crypto=crypto)
    assert manifests
    last_paths = {e.path for e in manifests[-1].entries}
    assert "c.txt" in last_paths
    assert "a.bin" not in last_paths


def test_volume1_origin_delta_encoded_on_volume2_sync(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        A file last written on volume 1 (origin session before volume 2's
        first_session) is delta-encoded against its old bytes when changed
        again during a volume-2-only sync. Proves disc_origin seed threads
        through to cache.get_with_fallback.
    """
    a_bytes = (_REPEAT * (_FILE_BYTES // len(_REPEAT) + 1))[:_FILE_BYTES]
    source, vol0, vol1, keyfile = _two_volume_archive(
        tmp_path, monkeypatch, a_bytes=a_bytes, label="VBASED",
    )
    changed = a_bytes.replace(b"lazy", b"LAZY", 1)
    (source / "a.bin").write_bytes(changed)
    _hide_volume0(tmp_path, vol0)
    capsys.readouterr()
    rc = dispatch(_make_args(
        command="sync",
        source=str(source),
        test_iso=str(vol1),
        disc_size="50mb",
        key=str(keyfile),
        label="VBASED",
    ))
    out = capsys.readouterr().out
    assert rc == 0, out
    new, n_changed, deleted, unchanged = _summary_counts(out)
    assert (new, n_changed, deleted, unchanged) == (0, 1, 0, 1)

    crypto = _crypto_for_key(keyfile)
    backend = ISOBackend(vol1, disc_size=ROOMY_DISC)
    manifests = _read_disc_manifests(backend, crypto=crypto)
    assert len(manifests) >= 2
    a_entries = [e for e in manifests[-1].entries if e.path == "a.bin"]
    assert len(a_entries) == 1
    assert a_entries[0].type == "delta"


def test_three_volume_baseline_chains_through_intermediate(
    tmp_path, monkeypatch,
):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A 3-volume init writes volume 3's baseline as the cumulative state
        of volumes 1 and 2 together, not just volume 2's own sessions.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    a = b"A" * _FILE_BYTES
    b = b"B" * _FILE_BYTES
    c = b"C" * _FILE_BYTES
    (source / "a.bin").write_bytes(a)
    (source / "b.bin").write_bytes(b)
    (source / "c.bin").write_bytes(c)
    iso = tmp_path / "archive.iso"
    rc = dispatch(_make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        disc_size=str(_DISC_THAT_SPLITS),
        label="VBASE3",
    ))
    assert rc == 0
    vol2 = tmp_path / "archive.vol2.iso"
    assert vol2.is_file()
    backend = ISOBackend(vol2, disc_size=_DISC_THAT_SPLITS)
    loaded = read_volume_baseline(backend)
    assert loaded is not None
    state, origin = loaded
    assert state["a.bin"] == hashlib.sha256(a).hexdigest()
    assert state["b.bin"] == hashlib.sha256(b).hexdigest()
    assert "c.bin" not in state
    assert origin["a.bin"] == 0
    assert origin["b.bin"] == 1


def test_legacy_single_volume_without_marker_syncs_as_before(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        A single-volume archive whose volume.json is removed (the
        no-marker/no-baseline path) still syncs a new file as 1 new /
        0 changed / 1 unchanged, matching pre-28c behavior.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello legacy")
    iso = tmp_path / "legacy.iso"
    rc = dispatch(_make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        label="VBASEL",
    ))
    assert rc == 0
    sessions = iso.with_suffix(".d")
    (sessions / "volume.json").unlink()
    assert not (sessions / "volume_baseline.json").exists()
    (source / "b.txt").write_bytes(b"new after legacy")
    capsys.readouterr()
    rc = dispatch(_make_args(
        command="sync",
        source=str(source),
        test_iso=str(iso),
        label="VBASEL",
    ))
    out = capsys.readouterr().out
    assert rc == 0, out
    new, changed, deleted, unchanged = _summary_counts(out)
    assert (new, changed, deleted, unchanged) == (1, 0, 0, 1)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
