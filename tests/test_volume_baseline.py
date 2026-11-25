"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for cumulative volume baselines, encryption, and disc-local
    blobs. The tests also cover sync on the latest disc.
"""
# Imports
import argparse
import hashlib
import logging
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.cli import _read_disc_manifests, dispatch
from oddarchiver.crypto import KeyfileCrypto, NullCrypto, generate_keyfile
from oddarchiver.disc import DiscInfo, ISOBackend
from oddarchiver.manifest import (
    ManifestEntry,
    build_disc_origin,
    build_disc_state,
    validate_disc_read_path,
)
from oddarchiver.session import build_staging
from oddarchiver.volume import (
    BaselineReadError, PathVersion, VolumeBaseline, VolumeMarker, VolumeRecord,
    build_volume_baseline, read_volume_baseline, read_volume_marker,
    write_volume_baseline,
)
from tests.helpers import _make_entry, _make_manifest, fill

# Globals
SMALL_DISC = 10 * 2**20
ROOMY_DISC = 50 * 2**20
_FILE_BYTES = 80_000
_DISC_THAT_SPLITS = 100_000
_TIMESTAMP = "2026-04-19T12:00:00Z"
_REPEAT = b"The quick brown fox jumps over the lazy dog.\n"
logger = logging.getLogger(__name__)
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
        "yubikey": False,
        "yubikey_slot": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    """Point cache and ledger at tmp_path so tests do not share home state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
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
    (source / "a.bin").write_bytes(a_bytes if a_bytes is not None else fill(b"A", _FILE_BYTES))
    (source / "b.bin").write_bytes(b_bytes if b_bytes is not None else fill(b"B", _FILE_BYTES))
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


def test_validate_disc_read_path_allows_volume_baselines():
    """Input: None. Output: None. Details: Both baseline formats are disc paths."""
    for name in ("volume_baseline.json", "volume_baseline.enc"):
        assert validate_disc_read_path(name) == name


def test_write_then_read_baseline_round_trips_via_iso_backend(tmp_path):
    """
    Input:  tmp_path - pytest temp directory
    Output: None
    Details:
        Write a version 2 baseline to staging and burn an ISO. Reading
        the disc returns the same baseline.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path)
    baseline = VolumeBaseline(
        2, [VolumeRecord(0, "TEST", 0, 0)],
        {"docs/a.txt": [PathVersion(0, 0, "full", "abc123", 11, _TIMESTAMP, 0)]},
        {"docs/a.txt": "abc123"}, {"docs/a.txt": 0},
    )
    write_volume_baseline(staging, baseline, None)
    backend.init(staging, label="TEST")
    assert read_volume_baseline(backend, None) == baseline


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
    assert read_volume_baseline(backend, None) is None


def test_read_baseline_raises_when_malformed(tmp_path, caplog):
    """
    Input:  tmp_path - pytest temp directory
            caplog   - pytest log capture
    Output: None
    Details:
        A present but undecodable volume_baseline.json raises
        BaselineReadError. None is reserved for a missing baseline.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path)
    (staging / "volume_baseline.json").write_bytes(b"not-json{")
    backend.init(staging, label="TEST")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BaselineReadError, match="volume_baseline.json"):
            read_volume_baseline(backend, None)
    assert any("volume_baseline.json" in r.message for r in caplog.records)


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
            read_volume_baseline(_FailingBackend(), None)
    assert any(
        "Failed to read volume_baseline" in r.message for r in caplog.records
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


def _assert_volume2_listing_history_status(iso: str, key: str, capsys) -> None:
    """Input: ISO, key, capture. Output: None. Check three read commands."""
    rc = dispatch(_make_args(command="ls", test_iso=iso, key=key, label="VBASE2"))
    listing = capsys.readouterr()
    ls_out = listing.out
    assert rc == 0, ls_out
    assert "b.bin" in ls_out
    assert "No sessions found." not in ls_out
    assert "Note: This list shows this disc only." in listing.err

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


def _assert_volume2_browse_restore(
    tmp_path: Path, monkeypatch, capsys, iso: str, key: str,
) -> None:
    """Input: temp root, patcher, capture, ISO, key. Output: None."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    rc = dispatch(_make_args(
        command="browse", test_iso=iso, key=key, label="VBASE2",
    ))
    listing = capsys.readouterr()
    browse_out = listing.out
    assert rc == 0, browse_out
    assert "b.bin" in browse_out
    assert "Note: This list shows this disc only." in listing.err

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
    assert (dest / "b.bin").read_bytes() == fill(b"B", _FILE_BYTES)


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
    _assert_volume2_listing_history_status(iso, key, capsys)
    _assert_volume2_browse_restore(tmp_path, monkeypatch, capsys, iso, key)


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


def test_volume1_origin_writes_full_on_volume2_sync(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        A change on a later disc writes a full blob when the prior version
        is on an earlier disc.
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
    assert a_entries[0].type == "full"
    assert a_entries[0].file.startswith("session_")


def _assert_chain_state(loaded: VolumeBaseline, a: bytes, b: bytes) -> None:
    """Input: baseline and source bytes. Output: None. Check stored state."""
    assert loaded.state["a.bin"] == hashlib.sha256(a).hexdigest()
    assert loaded.state["b.bin"] == hashlib.sha256(b).hexdigest()
    assert "c.bin" not in loaded.state
    assert loaded.origin["a.bin"] == 0
    assert loaded.origin["b.bin"] == 1


def _assert_chain_baseline(loaded: VolumeBaseline, a: bytes, b: bytes) -> None:
    """Input: cumulative baseline and file bytes. Output: None."""
    assert loaded.version == 2
    assert [v.volume_index for v in loaded.volumes] == [0, 1]
    _assert_chain_state(loaded, a, b)
    assert loaded.history["a.bin"][0].blob_session == 0
    assert loaded.history["b.bin"][0].blob_session == 1


def _assert_disc_local_blobs(manifests: list, minimum_session: int) -> None:
    """Input: manifests, first allowed blob session. Output: None."""
    for manifest in manifests:
        for entry in manifest.entries:
            blob_path = entry.delta_file if entry.type == "delta" else entry.file
            assert int(blob_path.split("/", 1)[0][8:]) >= minimum_session


def _assert_chain_history(
    index: int, disc_backend: ISOBackend, baseline: VolumeBaseline,
) -> None:
    """Input: disc index, backend, baseline. Output: None."""
    manifests = _read_disc_manifests(disc_backend, crypto=None)
    expected = [
        (manifest.session, entry.path, entry.result_checksum)
        for manifest in manifests for entry in manifest.entries
    ]
    actual = [
        (version.session, path, version.result_checksum)
        for path, versions in baseline.history.items()
        for version in versions
        if version.volume_index == index and version.kind != "deleted"
    ]
    assert sorted(actual) == sorted(expected)
    _assert_disc_local_blobs(manifests, index)


def test_three_volume_baseline_chains_through_intermediate(
    tmp_path, monkeypatch,
):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A three-volume init writes the state and history of both earlier
        discs into volume 2's baseline.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    a = fill(b"A", _FILE_BYTES)
    b = fill(b"B", _FILE_BYTES)
    c = fill(b"C", _FILE_BYTES)
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
    loaded = read_volume_baseline(backend, None)
    assert loaded is not None
    _assert_chain_baseline(loaded, a, b)
    vol0_backend = ISOBackend(iso, disc_size=_DISC_THAT_SPLITS)
    vol1_backend = ISOBackend(
        tmp_path / "archive.vol1.iso", disc_size=_DISC_THAT_SPLITS,
    )
    volume1_baseline = read_volume_baseline(vol1_backend, None)
    assert volume1_baseline is not None
    assert [record.volume_index for record in volume1_baseline.volumes] == [0]
    for index, disc_backend in enumerate((vol0_backend, vol1_backend)):
        baseline = volume1_baseline if index == 0 else loaded
        _assert_chain_history(index, disc_backend, baseline)
    _assert_disc_local_blobs(_read_disc_manifests(backend, crypto=None), 2)


def test_encrypted_archive_stores_only_encrypted_baseline(tmp_path, monkeypatch):
    """
    Input:  tmp_path and monkeypatch
    Output: None
    Details:
        A split keyfile archive writes no readable baseline path names.
    """
    _source, _vol0, vol1, keyfile = _two_volume_archive(
        tmp_path, monkeypatch, label="VBCRYPT",
    )
    backend = ISOBackend(vol1, disc_size=_DISC_THAT_SPLITS)
    found = backend.read_paths(["volume_baseline.enc", "volume_baseline.json"])
    assert "volume_baseline.enc" in found
    assert "volume_baseline.json" not in found
    assert b"a.bin" not in vol1.read_bytes()
    assert read_volume_baseline(backend, _crypto_for_key(keyfile)) is not None


def test_legacy_single_volume_without_marker_syncs_as_before(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        A single-volume archive whose volume.json is removed (the
        no-marker/no-baseline path) still syncs a new file as 1 new /
        0 changed / 1 unchanged, matching pre-28c behavior. Restore
        writes the exact bytes of both files.
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
    dest = tmp_path / "legacy_dest"
    rc = dispatch(_make_args(
        command="restore",
        dest=str(dest),
        test_iso=str(iso),
        label="VBASEL",
    ))
    assert rc == 0
    restored = {
        str(path.relative_to(dest)): path.read_bytes()
        for path in dest.rglob("*") if path.is_file()
    }
    assert restored == {
        "a.txt": b"hello legacy",
        "b.txt": b"new after legacy",
    }


def _assert_v1_seed_history(next_baseline: VolumeBaseline) -> None:
    """Input: migrated baseline. Output: None. Check seeded legacy history."""
    assert next_baseline.volumes == [
        VolumeRecord(1, "NEW", 1, 1),
    ]
    assert [v.kind for v in next_baseline.history["old.txt"]] == [
        "full", "deleted",
    ]
    assert next_baseline.history["old.txt"][0].volume_index is None
    assert next_baseline.history["old.txt"][0].full_size_bytes == 0
    assert next_baseline.history["old.txt"][0].blob_session == 0
    assert next_baseline.state == {}
    assert next_baseline.origin == {}


def test_v1_baseline_reads_and_seeds_v2_history(tmp_path):
    """
    Input:  tmp_path - isolated disc image
    Output: None
    Details:
        An old plaintext baseline keeps its maps. The next baseline marks
        unknown sizes and unknown volume locations.
    """
    backend = ISOBackend(tmp_path / "old.iso", disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path)
    (staging / "volume_baseline.json").write_text(json.dumps({
        "state": {"old.txt": "old-hash"},
        "origin": {"old.txt": 0},
    }))
    backend.init(staging, label="OLD")
    prior = read_volume_baseline(backend, None)
    assert prior is not None
    assert prior.version == 1
    assert prior.state == {"old.txt": "old-hash"}
    marker = VolumeMarker("archive", 1, 1, _TIMESTAMP, "OLD")
    next_baseline = build_volume_baseline(
        prior, [_make_manifest(1, deleted=["old.txt"])], marker, "NEW",
    )
    _assert_v1_seed_history(next_baseline)


def test_build_baseline_records_dedup_and_tombstones():
    """
    Input:  None
    Output: None
    Details:
        Record each write and deletion. Keep a dedup reference's physical
        blob session distinct from the write session.
    """
    old = _make_manifest(0, entries=[
        ManifestEntry(
            path="one.txt", type="full", result_checksum="one",
            full_size_bytes=10, file="session_000/full/" + "a" * 64,
        ),
    ])
    first = build_volume_baseline(
        None, [old], VolumeMarker("archive", 0, 0, _TIMESTAMP, ""), "VOL0",
    )
    copied = _make_manifest(1, entries=[
        ManifestEntry(
            path="copy.txt", type="full", result_checksum="one",
            full_size_bytes=10, file="session_000/full/" + "a" * 64,
        ),
    ], deleted=["one.txt"])
    suspect = _make_manifest(2, entries=[_make_entry("bad.txt")])
    suspect.suspect = True
    second = build_volume_baseline(
        first, [copied, suspect],
        VolumeMarker("archive", 1, 1, _TIMESTAMP, "VOL0"), "VOL1",
    )
    assert [v.volume_index for v in second.volumes] == [0, 1]
    assert second.history["copy.txt"] == [
        PathVersion(1, 1, "full", "one", 10, _TIMESTAMP, 0),
    ]
    assert [v.kind for v in second.history["one.txt"]] == [
        "full", "deleted",
    ]
    assert second.history["one.txt"][-1].blob_session is None
    assert "bad.txt" not in second.history
    assert second.state == {"copy.txt": "one"}


def test_encrypted_baseline_never_writes_plaintext(tmp_path, caplog):
    """
    Input:  tmp_path and caplog
    Output: None
    Details:
        An encrypted baseline hides path names and rejects missing or wrong
        keys while preserving the data for the correct key.
    """
    staging = _make_staging(tmp_path)
    key = tmp_path / "right.key"
    wrong = tmp_path / "wrong.key"
    generate_keyfile(str(key))
    generate_keyfile(str(wrong))
    crypto = KeyfileCrypto(str(key))
    baseline = VolumeBaseline(
        2, [VolumeRecord(0, "VOL0", 0, 0)], {},
        {"secret-known-source-path.txt": "abc"}, {"secret-known-source-path.txt": 0},
    )
    write_volume_baseline(staging, baseline, crypto)
    assert (staging / "volume_baseline.enc").exists()
    assert not (staging / "volume_baseline.json").exists()
    backend = ISOBackend(tmp_path / "encrypted.iso", disc_size=SMALL_DISC)
    backend.init(staging, label="ENCRYPTED")
    assert b"secret-known-source-path.txt" not in backend.iso_path.read_bytes()
    assert read_volume_baseline(backend, crypto) == baseline
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BaselineReadError, match="volume_baseline.enc"):
            read_volume_baseline(backend, None)
        with pytest.raises(BaselineReadError, match="volume_baseline.enc"):
            read_volume_baseline(backend, KeyfileCrypto(str(wrong)))
    assert any("volume_baseline.enc" in r.message for r in caplog.records)


def test_new_chunks_materialize_delta_and_dedup_refs(tmp_path):
    """
    Input:  tmp_path - source and staging roots
    Output: None
    Details:
        Packing a delta and a dedup reference into new volumes writes full
        blobs locally after a second packing pass. Nothing fits the loaded
        medium, so its empty session is skipped and each remaining bin is
        a new volume: two bins, not an empty bin plus two.
    """
    old = (_REPEAT * 2000)[:_FILE_BYTES]
    changed = old.replace(b"lazy", b"LAZY", 1)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(changed)
    (source / "b.bin").write_bytes(old)
    backend = MagicMock()
    backend.disc_size = _DISC_THAT_SPLITS
    backend.mediainfo.return_value = DiscInfo(
        session_count=1, remaining_bytes=0, used_bytes=0, label="VOL0",
    )
    cache = MagicMock()
    cache.get_with_fallback.return_value = old
    checksum = hashlib.sha256(old).hexdigest()
    original = _make_manifest(0, entries=[
        ManifestEntry(
            path="a.bin", type="full", result_checksum=checksum,
            full_size_bytes=len(old),
            file="session_000/full/" + "a" * 64,
        ),
    ])
    chunks = build_staging(
        session_n=1, source=source,
        disc_state={"a.bin": checksum}, disc_origin={"a.bin": 0},
        backend=backend, cache=cache, crypto=NullCrypto(),
        manifests=[original], _staging_root=tmp_path / "staging-root",
    )
    assert len(chunks) == 2
    from oddarchiver.manifest import read_manifest
    for staging, session, new_disc in chunks:
        assert new_disc
        manifest = read_manifest(staging / f"session_{session:03d}" / "manifest.json")
        for entry in manifest.entries:
            assert entry.type == "full"
            assert entry.file.startswith(f"session_{session:03d}/")
            assert (staging / entry.file).exists()


def test_prior_disc_dedup_is_not_reused(tmp_path):
    """
    Input:  tmp_path - source and staging roots
    Output: None
    Details:
        A duplicate from an earlier disc gets its own full blob.
    """
    old = b"prior-volume-bytes" * 200
    source = tmp_path / "source"
    source.mkdir()
    (source / "old.bin").write_bytes(old)
    (source / "copy.bin").write_bytes(old)
    backend = MagicMock()
    backend.disc_size = _DISC_THAT_SPLITS
    backend.mediainfo.return_value = DiscInfo(
        session_count=1, remaining_bytes=_DISC_THAT_SPLITS,
        used_bytes=0, label="VOL1",
    )
    chunks = build_staging(
        session_n=2, source=source,
        disc_state={"old.bin": hashlib.sha256(old).hexdigest()},
        disc_origin={"old.bin": 0}, volume_first_session=1,
        backend=backend, cache=MagicMock(), crypto=NullCrypto(),
        _staging_root=tmp_path / "staging-root",
    )
    from oddarchiver.manifest import read_manifest
    staging, session, _is_new = chunks[0]
    manifest = read_manifest(staging / f"session_{session:03d}" / "manifest.json")
    entry = next(e for e in manifest.entries if e.path == "copy.bin")
    assert entry.file.startswith("session_002/")
    assert (staging / entry.file).exists()


def test_sync_refuses_legacy_baseline_on_new_volume(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        A new-format session records the baseline checksum. Sync must
        refuse a substituted legacy baseline before it writes a session.
    """
    source, vol0, vol1, _keyfile = _two_volume_archive(
        tmp_path, monkeypatch, encrypt=False,
    )
    old_baseline = vol1.with_suffix(".d") / "volume_baseline.json"
    old_baseline.write_text(json.dumps({
        "state": {"a.bin": hashlib.sha256((source / "a.bin").read_bytes()).hexdigest()},
        "origin": {"a.bin": 0},
    }))
    _hide_volume0(tmp_path, vol0)
    capsys.readouterr()
    assert dispatch(_make_args(
        command="sync", source=str(source), test_iso=str(vol1),
        disc_size="50mb", label="VBASE2",
    )) != 0
    assert "volume_baseline" in capsys.readouterr().err


def _assert_prior_disc_events(
    disc_index: int, baseline: VolumeBaseline, prior_manifests: list,
) -> None:
    """Input: disc index, baseline, earlier manifests. Output: None."""
    assert baseline.version == 2
    assert [v.volume_index for v in baseline.volumes] == list(range(disc_index))
    expected_writes = [
        (entry.path, manifest.session, manifest.timestamp, entry.type,
         entry.result_checksum, entry.full_size_bytes,
         int((entry.delta_file or entry.file).split("/", 1)[0][8:]))
        for previous in prior_manifests
        for manifest in previous
        for entry in manifest.entries
    ]
    expected_deletions = [
        (path, manifest.session, manifest.timestamp, "deleted", "", 0, None)
        for previous in prior_manifests
        for manifest in previous
        for path in manifest.deleted
    ]
    actual_events = [
        (path, event.session, event.timestamp, event.kind,
         event.result_checksum, event.full_size_bytes, event.blob_session)
        for path, events in baseline.history.items()
        for event in events
    ]
    assert sorted(actual_events) == sorted(expected_writes + expected_deletions)


def _assert_prior_blob_reference(entry, manifest, manifests: list, first_session: int) -> None:
    """Input: entry, manifest, disc manifests, first session. Output: None."""
    assert entry.type == "full" or (
        any(
            previous.session >= first_session
            and any(old.path == entry.path for old in previous.entries)
            for previous in manifests if previous.session < manifest.session
        )
    )


def _assert_disc_blob_references(manifests: list, first_session: int) -> None:
    """Input: disc manifests, first session. Output: None."""
    for manifest in manifests:
        for entry in manifest.entries:
            blob_session = int(
                (entry.delta_file or entry.file).split("/", 1)[0][8:]
            )
            assert blob_session >= first_session
            _assert_prior_blob_reference(entry, manifest, manifests, first_session)


def _assert_newest_disc_entries(manifests: list) -> None:
    """Input: latest disc manifests. Output: None. Check full entries."""
    newest = [e for m in manifests for e in m.entries]
    assert {e.path for e in newest} == {"a.bin", "copy.bin"}
    assert all(e.type == "full" for e in newest)


def _assert_three_disc_history(iso: Path, vol1: Path, vol2: Path) -> None:
    """Input: three ISO paths. Output: None. Check history and disc blobs."""
    prior_manifests = []
    for disc_index, image in enumerate((iso, vol1, vol2)):
        backend = ISOBackend(image, disc_size=_DISC_THAT_SPLITS)
        marker = read_volume_marker(backend)
        assert marker is not None
        assert marker.volume_index == disc_index
        baseline = read_volume_baseline(backend, None)
        if disc_index:
            assert baseline is not None
            _assert_prior_disc_events(disc_index, baseline, prior_manifests)
        manifests = _read_disc_manifests(backend, crypto=None)
        prior_manifests.append(manifests)
        _assert_disc_blob_references(manifests, marker.first_session)
        if disc_index == 2:
            _assert_newest_disc_entries(manifests)


def test_three_disc_writes_history_and_disc_local_blobs(tmp_path, monkeypatch):
    """
    Input:  tmp_path and monkeypatch
    Output: None
    Details:
        A file changes on three discs. Volume 2 also gets a duplicate of
        the first version. Baselines keep all earlier writes and deletions.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    first = fill(b"A", 40_000)
    (source / "a.bin").write_bytes(first)
    (source / "removed.txt").write_bytes(b"delete me")
    iso = tmp_path / "archive.iso"
    args = _make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size=str(_DISC_THAT_SPLITS), label="THREEDISC",
    )
    assert dispatch(args) == 0
    (source / "a.bin").write_bytes(fill(b"B", len(first)))
    (source / "removed.txt").unlink()
    args.command = "sync"
    assert dispatch(args) == 0
    vol1 = tmp_path / "archive.vol1.iso"
    assert vol1.exists()
    (source / "a.bin").write_bytes(fill(b"C", len(first)))
    (source / "copy.bin").write_bytes(first)
    args.test_iso = str(vol1)
    assert dispatch(args) == 0
    vol2 = tmp_path / "archive.vol2.iso"
    assert vol2.exists()

    _assert_three_disc_history(iso, vol1, vol2)


def _assert_catalog_rows(rows: dict[str, str]) -> None:
    """Input: listed file rows. Output: None. Check volume locations."""
    assert "a.bin" in rows and "disc 2" in rows["a.bin"]
    assert "old.bin" in rows and "disc 0" in rows["old.bin"]
    assert "copy.bin" in rows and "disc 2" in rows["copy.bin"]
    assert "removed.txt" not in rows


def _assert_catalog_all_discs(args: argparse.Namespace, capsys, vol2: Path) -> None:
    """Input: CLI arguments, capture, final disc. Output: None."""
    args.command = "ls"
    args.test_iso = str(vol2)
    args.all_disks = True
    assert dispatch(args) == 0
    out = capsys.readouterr()
    assert "Disc" in out.out
    rows = {line.split()[0]: line for line in out.out.splitlines()[2:]}
    _assert_catalog_rows(rows)
    assert "Catalog from " in out.err


def _assert_catalog_session_zero(args: argparse.Namespace, capsys) -> None:
    """Input: CLI arguments and capture. Output: None. Check past session."""
    args.session = 0
    assert dispatch(args) == 0
    out = capsys.readouterr().out
    assert "removed.txt" in out
    assert "old.bin" in out
    assert "copy.bin" not in out
    assert "a.bin" in out and "disc 0" in next(
        line for line in out.splitlines() if "a.bin" in line
    )


def _assert_catalog_deleted_and_past(args: argparse.Namespace, capsys) -> None:
    """Input: CLI arguments and capture. Output: None."""
    args.show_deleted = True
    assert dispatch(args) == 0
    out = capsys.readouterr().out
    removed = next(line for line in out.splitlines() if "removed.txt" in line)
    assert "DELETED at session" in removed
    # Sync skips the full current medium, so its first burned chunk is
    # session 001 on disc 1 and the tombstone lives there.
    assert "disc 1" in removed

    _assert_catalog_session_zero(args, capsys)


def _assert_catalog_disc_only(args: argparse.Namespace, capsys) -> None:
    """Input: CLI arguments and capture. Output: None."""
    args.all_disks = False
    args.session = None
    assert dispatch(args) == 0
    out = capsys.readouterr()
    assert "a.bin" in out.out
    assert "old.bin" not in out.out
    assert "Note: This list shows this disc only." in out.err


def test_three_disc_catalog_lists_history_and_tombstones(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        Read the third ISO after syncs write three discs.
        Check older paths, newer writes, deleted paths, and past sessions.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(fill(b"A", 40_000))
    (source / "removed.txt").write_bytes(b"remove")
    (source / "old.bin").write_bytes(b"old")
    iso = tmp_path / "archive.iso"
    args = _make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size=str(_DISC_THAT_SPLITS), label="THREEDISC",
    )
    assert dispatch(args) == 0
    (source / "a.bin").write_bytes(fill(b"B", 40_000))
    (source / "removed.txt").unlink()
    args.command = "sync"
    assert dispatch(args) == 0
    vol1 = tmp_path / "archive.vol1.iso"
    assert vol1.exists()
    (source / "a.bin").write_bytes(fill(b"C", 40_000))
    (source / "copy.bin").write_bytes(fill(b"A", 40_000))
    args.test_iso = str(vol1)
    assert dispatch(args) == 0
    vol2 = tmp_path / "archive.vol2.iso"
    assert vol2.exists()
    capsys.readouterr()

    _assert_catalog_all_discs(args, capsys, vol2)
    _assert_catalog_deleted_and_past(args, capsys)
    _assert_catalog_disc_only(args, capsys)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
