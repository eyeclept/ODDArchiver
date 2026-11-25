"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Deeper tests for audit section 41 items T1, T2, T3, T5, and T7.
    T4 and T8 stay in the files that already hold those tests.
"""
# Imports
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.catalog import build_archive_catalog, remember_encryption_mode
from oddarchiver.cli import _run_restore, dispatch
from tests.helpers import fill
from oddarchiver.crypto import KeyfileCrypto, NullCrypto, generate_keyfile
from oddarchiver.delta import compute_delta
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, load_session_manifests, write_manifest
from oddarchiver.restore_plan import DiscAcquisition, restore_archive
from oddarchiver.restore_state import load_restore_state, state_path
from oddarchiver.volume import read_volume_marker
from tests.test_restore_plan import _ID as _PLAN_ID
from tests.test_restore_plan import _three_discs

# Globals
logger = logging.getLogger(__name__)
_DISC = 20 * 2**20
_SPLIT_DISC = 100_000
_FILE_BYTES = 80_000
_ID = "depth-section41"
_CRYPTO = NullCrypto()
_CREATED = "2026-10-15T00:00:00Z"


# Functions


@pytest.fixture(autouse=True)
def _isolated_cli_state(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Keep cache, state, and HOME files inside the test directory.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cache = tmp_path / "cache" / "oddarchiver"
    cache.mkdir(parents=True)
    monkeypatch.setattr(
        "oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache),
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides
    Output: Namespace for init, sync, restore, and browse
    """
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "DEPTH",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": str(_SPLIT_DISC),
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
        "unskip_volumes": None,
        "all_disks": False,
        "embed_software": None,
        "skip_large": False,
        "slow": False,
        "read_mode": "sequential",
        "all_files": False,
        "yubikey": False,
        "yubikey_slot": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _payload(marker: bytes, fill: bytes, size: int = _FILE_BYTES) -> bytes:
    """
    Input:  marker, fill, size
    Output: size bytes that start with marker
    """
    body = marker + fill * (size // len(fill) + 1)
    return body[:size]


def _sha256(data: bytes) -> str:
    """Input: data. Output: hex digest."""
    return hashlib.sha256(data).hexdigest()


def _blob_id(session_n: int, rel_path: str) -> str:
    """Input: session and path. Output: blob file name used on disc."""
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _relative_child_path(path: str, prefix: str) -> str:
    """Input: catalog path and browse prefix. Output: path relative to prefix."""
    if prefix:
        if path == prefix or not path.startswith(prefix + "/"):
            return ""
        return path[len(prefix) + 1:]
    return path


def _child_names(paths: list[str], current: str = "") -> list[str]:
    """
    Input:  catalog paths and the current browse directory
    Output: sorted direct child names
    """
    names: set[str] = set()
    prefix = current.rstrip("/")
    for path in paths:
        rest = _relative_child_path(path, prefix)
        if not rest:
            continue
        names.add(rest.split("/", 1)[0])
    return sorted(names)


def _browse_keys(target: str, paths: list[str]) -> list[str]:
    """
    Input:  target path and live catalog paths
    Output: numbered browse keys that select target
    """
    commands: list[str] = []
    current = ""
    parts = target.split("/")
    for i, part in enumerate(parts):
        children = _child_names(paths, current)
        commands.append(str(children.index(part) + 1))
        if i < len(parts) - 1:
            current = f"{current}/{part}" if current else part
    return commands


def _entry_paths(iso: Path, crypto=None) -> set[str]:
    """
    Input:  ISO path and optional crypto
    Output: source paths stored on that disc
    """
    backend = ISOBackend(iso, disc_size=_SPLIT_DISC)
    marker = read_volume_marker(backend)
    start = marker.first_session if marker else 0
    manifests = load_session_manifests(backend, crypto or _CRYPTO, start_session=start)
    return {entry.path for manifest in manifests for entry in manifest.entries}


def _three_volume_cli_archive(tmp_path: Path, monkeypatch, *, encrypt: bool = False):
    """
    Input:  tmp_path, monkeypatch, encrypt flag
    Output: (source, isos, keyfile, files)
    Details:
        Real init on a 100000-byte ISO packs three 80 KB files onto
        archive.iso, archive.vol1.iso, and archive.vol2.iso.
    """
    def _boom(prompt=""):
        raise AssertionError(f"ISOBackend must not call input(): {prompt!r}")

    monkeypatch.setattr("builtins.input", _boom)
    source = tmp_path / "src"
    source.mkdir()
    nested = source / "aaa_only"
    nested.mkdir()
    files = {
        "aaa_only/payload.bin": _payload(b"DISC0-ONLY-MARKER-", b"A"),
        "mid.bin": _payload(b"DISC1-MID-MARKER--", b"B"),
        "zzz.bin": _payload(b"DISC2-LATE-MARKER-", b"C"),
    }
    for rel, data in files.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    iso = tmp_path / "archive.iso"
    keyfile = None
    kwargs = {
        "command": "init",
        "source": str(source),
        "test_iso": str(iso),
        "disc_size": str(_SPLIT_DISC),
        "label": "DEPTH",
    }
    if encrypt:
        keyfile = tmp_path / "archive.key"
        generate_keyfile(str(keyfile))
        kwargs["encrypt"] = "keyfile"
        kwargs["key"] = str(keyfile)
    rc = dispatch(_make_args(**kwargs))
    assert rc == 0
    isos = [iso, tmp_path / "archive.vol1.iso", tmp_path / "archive.vol2.iso"]
    for path in isos:
        assert path.is_file(), path
    return source, isos, keyfile, files


def _stage_entries(
    staging: Path, tmp_path: Path, n: int, full: dict, delta: dict | None,
    crypto, state: dict, origin: dict,
) -> list[ManifestEntry]:
    """Input: staged files and prior maps. Output: format-1 manifest entries."""
    entries = []
    for path, content in full.items():
        rel = f"session_{n:03d}/full/{_blob_id(n, path)}"
        blob = staging / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(crypto.encrypt(content))
        entries.append(ManifestEntry(
            path=path, type="full", file=rel,
            result_checksum=_sha256(content),
            full_size_bytes=len(content),
        ))
        state[path] = _sha256(content)
        origin[path] = n
    for path, (base, content) in (delta or {}).items():
        new_file = tmp_path / f"delta-source-{n}"
        new_file.write_bytes(content)
        difference = compute_delta(base, new_file)
        rel = f"session_{n:03d}/deltas/{_blob_id(n, path)}"
        blob = staging / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(crypto.encrypt(difference))
        entries.append(ManifestEntry(
            path=path, type="delta", delta_file=rel,
            delta_size_bytes=len(difference),
            result_checksum=_sha256(content),
            full_size_bytes=len(content),
        ))
        state[path] = _sha256(content)
    return entries


def _stage(tmp_path: Path, n: int, full: dict, crypto, prior_state=None,
           prior_origin=None, delta=None, archive_id=_ID, iso_name=None):
    """
    Input:  session index, file maps, crypto, optional v1 prior maps
    Output: (iso, state, origin)
    Details:
        Burns one format-1 disc in the commit 02d325d layout. volume.json
        has no format field. The baseline JSON has no version field and
        stays plaintext. With a non-Null crypto, each session holds
        manifest.enc and enc_mode.json. With NullCrypto, it holds a
        plaintext manifest.json.
    """
    staging = tmp_path / f"stage-{archive_id}-{n}"
    session_dir = staging / f"session_{n:03d}"
    session_dir.mkdir(parents=True)
    state = dict(prior_state or {})
    origin = dict(prior_origin or {})
    entries = _stage_entries(staging, tmp_path, n, full, delta, crypto, state, origin)
    enc_block = {} if isinstance(crypto, NullCrypto) else {"mode": "keyfile"}
    write_manifest(session_dir, Manifest(
        version=1, session=n, timestamp=f"2026-10-15T00:00:{n:02d}Z",
        source="/src", label=f"VOL{n}", based_on_session=n - 1 if n else None,
        encryption=enc_block, entries=entries, deleted=[], manifest_checksum="",
    ), crypto)
    marker = {
        "archive_id": archive_id,
        "volume_index": n,
        "first_session": n,
        "created_at": _CREATED,
        "previous_volume_label": f"VOL{n - 1}" if n else "",
    }
    (staging / "volume.json").write_text(json.dumps(marker, indent=2) + "\n")
    if prior_state is not None:
        (staging / "volume_baseline.json").write_text(
            json.dumps({"state": prior_state, "origin": prior_origin}, indent=2) + "\n",
        )
    iso = tmp_path / (iso_name or f"vol{n}.iso")
    ISOBackend(iso, disc_size=_DISC).init(staging, label=f"VOL{n}", expected_session_count=0)
    return iso, state, origin


def _source(iso: Path, crypto=_CRYPTO):
    """
    Input:  ISO and crypto
    Output: (backend, catalog, manifests)
    """
    backend = ISOBackend(iso, disc_size=_DISC)
    marker = read_volume_marker(backend)
    manifests = load_session_manifests(backend, crypto, start_session=marker.first_session)
    return backend, build_archive_catalog(backend, crypto, manifests, marker), manifests


class SwappableDrive:
    """One drive object whose disc content changes on insert."""

    def __init__(self, discs: list[Path], disc_size: int = _DISC):
        """
        Input:  ISO paths in volume order and disc size
        Output: drive standing in for a physical unit
        """
        self.discs = list(discs)
        self.disc_size = disc_size
        self.blob_reads: list[tuple[Path, list[str]]] = []
        self._inner = ISOBackend(discs[-1], disc_size=disc_size)
        self.inserted = discs[-1]

    def insert(self, iso: Path) -> None:
        """Input: ISO path. Output: None. Details: Mount this ISO as the disc."""
        self.inserted = iso
        self._inner = ISOBackend(iso, disc_size=self.disc_size)

    def read_paths(self, paths):
        """Input: disc paths. Output: found control bytes."""
        return self._inner.read_paths(paths)

    def extract_paths(self, paths, dest_root, max_bytes=None):
        """Input: disc paths and target. Output: bytes written. Record blob reads."""
        blobs = [path for path in paths if "/full/" in path or "/deltas/" in path]
        if blobs:
            self.blob_reads.append((self.inserted, list(blobs)))
        return self._inner.extract_paths(paths, dest_root, max_bytes=max_bytes)

    def read_path(self, path):
        """Input: one path. Output: bytes."""
        return self._inner.read_path(path)

    def mediainfo(self):
        """Input: None. Output: DiscInfo of the inserted ISO."""
        return self._inner.mediainfo()

    def list_session_numbers(self):
        """Input: None. Output: session numbers on the inserted ISO."""
        return self._inner.list_session_numbers()

    def extract_path_to_file(self, path, dest):
        """Input: disc path and dest. Output: None."""
        return self._inner.extract_path_to_file(path, dest)

    def init(self, staging, label, expected_session_count=0, **kwargs):
        """Input: staging write. Output: None. Details: Tests do not burn here."""
        raise AssertionError("SwappableDrive.init is not used")

    def append(self, staging, label, expected_session_count, **kwargs):
        """Input: staging write. Output: None. Details: Tests do not burn here."""
        raise AssertionError("SwappableDrive.append is not used")


class RecordingAcquisition(DiscAcquisition):
    """Real DiscAcquisition that records each needed-disc map."""

    def __init__(self, drive: SwappableDrive, ask):
        """
        Input:  drive and prompt function
        Output: acquisition that uses the drive prompt path
        """
        super().__init__(drive, image=None, ask=ask)
        self.needed_log: list[dict[int, int]] = []

    def acquire(self, needed, catalog, archive_id):
        """Input: needed map. Output: backend, missing index, or quit."""
        self.needed_log.append(dict(needed))
        return super().acquire(needed, catalog, archive_id)


class _ReverseAcquisition:
    """Hand discs to restore_archive from highest index to lowest."""

    image = None

    def __init__(self, discs, pause_first=False):
        """
        Input:  ISO list and whether the first acquire should quit
        Output: acquisition object
        """
        self.discs = discs
        self.pause_first = pause_first
        self.paused = False
        self.calls = []

    def acquire(self, needed, catalog, archive_id):
        """Input: needed map. Output: next disc in reverse index order, or quit."""
        self.calls.append(dict(needed))
        if self.pause_first and not self.paused:
            self.paused = True
            return None, None, True
        for index in sorted(needed, reverse=True):
            return ISOBackend(self.discs[index], disc_size=_DISC), None, False
        return None, None, True


def _restore_cli_args(dest: Path, *, key: str | None = None) -> argparse.Namespace:
    """
    Input:  dest and optional keyfile
    Output: Namespace for _run_restore on a fake drive
    """
    return argparse.Namespace(
        dest=str(dest),
        test_iso=None,
        device="/dev/sr0",
        disc_size="20mb",
        session=None,
        force=False,
        paths=None,
        key=key,
        rescue_key=None,
        skip_volumes=None,
        unskip_volumes=None,
        all_disks=True,
        read_mode="sequential",
        recover_scan=False,
        label="",
    )


def _build_browse_selection(isos: list[Path]) -> str:
    """Input: archive discs. Output: the first path found only on disc zero."""
    crypto = None
    disc0_only = _entry_paths(isos[0], crypto) - _entry_paths(isos[1], crypto) - _entry_paths(isos[2], crypto)
    assert disc0_only, "init did not isolate a disc-0 path"
    return sorted(disc0_only)[0]


def _track_browse_io(monkeypatch) -> tuple[list[str], list[str]]:
    """Input: monkeypatch. Output: recorded blob extraction and loaded ISO names."""
    blob_names: list[str] = []
    loaded: list[str] = []
    original_extract = ISOBackend.extract_paths
    original_init = ISOBackend.__init__

    def track_extract(self, paths, dest_root, max_bytes=None):
        blobs = [path for path in paths if "/full/" in path or "/deltas/" in path]
        if blobs:
            blob_names.append(Path(self.iso_path).name)
        return original_extract(self, paths, dest_root, max_bytes=max_bytes)

    def track_init(self, iso_path, disc_size=None):
        loaded.append(Path(iso_path).name)
        original_init(self, iso_path, disc_size)

    monkeypatch.setattr(ISOBackend, "extract_paths", track_extract)
    monkeypatch.setattr(ISOBackend, "__init__", track_init)
    return blob_names, loaded


def test_browse_all_disks_restores_disc0_only_path(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        browse --all-disks on the latest disc of a real 3-disc init
        archive. Select the path that lives only on disc 0. Restored
        bytes match the source. Blob reads hit disc 0 only. Manifest
        checks still open the other known volumes.
    """
    _source_dir, isos, _key, files = _three_volume_cli_archive(tmp_path, monkeypatch)
    crypto = None
    target = _build_browse_selection(isos)
    catalog_paths = sorted(
        _entry_paths(isos[0], crypto) | _entry_paths(isos[1], crypto) | _entry_paths(isos[2], crypto),
    )
    dest = tmp_path / "browse_dest"
    keys = _browse_keys(target, catalog_paths) + ["r", str(dest)]
    answers = iter(keys)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    blob_names, loaded = _track_browse_io(monkeypatch)
    rc = dispatch(_make_args(
        command="browse",
        test_iso=str(isos[2]),
        all_disks=True,
        disc_size=str(_SPLIT_DISC),
        label="DEPTH",
    ))
    assert rc == 0
    restored = dest / target
    assert restored.read_bytes() == files[target]
    assert blob_names == ["archive.iso"]
    assert "archive.iso" in loaded
    assert "archive.vol2.iso" in loaded
    assert "archive.vol1.iso" in loaded


def _build_scripted_prompt(drive: SwappableDrive, script: list[tuple[str, Path | None]]):
    """Input: swappable drive and scripted replies. Output: acquisition prompt."""
    steps = iter(script)

    def ask(prompt):
        answer, iso = next(steps)
        if iso is not None:
            drive.insert(iso)
        return answer

    return ask


def _assert_acquisition_state(dest: Path) -> None:
    """Input: restore path. Output: asserts saved missing and read discs."""
    state = load_restore_state(dest, _PLAN_ID)
    assert state.missing_discs == [1]
    assert set(state.read_discs) == {0, 2}


def test_disc_acquisition_prompt_rejects_fallback_and_restores(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        Real DiscAcquisition prompts on a swappable drive. Bad input,
        a foreign disc, and an unneeded disc are refused. m 1 falls
        back. Enter with disc 0 restores the fallback bytes.
    """
    discs = _three_discs(tmp_path)
    other, _state, _origin = _stage(
        tmp_path, 0, {"foreign.txt": b"foreign"}, _CRYPTO,
        archive_id="other-archive", iso_name="foreign.iso",
    )
    drive = SwappableDrive(discs)
    drive.insert(discs[2])
    script = [
        ("m \u00b2", None),
        ("", other),
        ("", discs[2]),
        ("m 1", None),
        ("", discs[0]),
    ]
    ask = _build_scripted_prompt(drive, script)

    _backend, catalog, manifests = _source(discs[2])
    acquisition = RecordingAcquisition(drive, ask)
    dest = tmp_path / "prompt"
    result = restore_archive(
        dest, drive, _CRYPTO, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    err = capsys.readouterr().err
    assert result.failures == 0
    assert (dest / "latest.txt").read_bytes() == b"latest"
    assert (dest / "old-only.txt").read_bytes() == b"old-only"
    assert (dest / "a.txt").read_bytes() == b"A-old"
    assert result.stale == [("a.txt", 0)]
    assert acquisition.needed_log == [
        {0: 1, 1: 1},
        {0: 1, 1: 1},
        {0: 1, 1: 1},
        {0: 1, 1: 1},
        {0: 2},
    ]
    _assert_acquisition_state(dest)
    assert "at most six digits" in err
    assert "another archive" in err
    assert "Traceback" not in err


def test_disc_acquisition_quit_cli_exit_3(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        q at the real acquire prompt through _run_restore exits 3 and
        writes restore-state with the inserted disc already read.
    """
    discs = _three_discs(tmp_path)
    remember_encryption_mode(_PLAN_ID, "none")
    drive = SwappableDrive(discs)
    drive.insert(discs[2])
    monkeypatch.setattr("oddarchiver.cli._make_backend", lambda args: drive)
    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    dest = tmp_path / "paused"
    rc = _run_restore(_restore_cli_args(dest))
    assert rc == 3
    state = load_restore_state(dest, _PLAN_ID)
    assert state is not None
    assert state.read_discs == [2]
    assert state.missing_discs == []
    assert (dest / "latest.txt").read_bytes() == b"latest"
    assert not (dest / "a.txt").exists()
    assert not (dest / "old-only.txt").exists()


def test_fallback_rereads_already_read_disc_once(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Read disc 0 for old-only.txt. Mark disc 1 missing. Disc 0 is
        requested once more for the a.txt fallback. Bytes are A-old.
    """
    discs = _three_discs(tmp_path)
    drive = SwappableDrive(discs)
    drive.insert(discs[2])
    script = [
        ("", discs[0]),
        ("m 1", None),
        ("", discs[0]),
    ]
    ask = _build_scripted_prompt(drive, script)

    _backend, catalog, manifests = _source(discs[2])
    acquisition = RecordingAcquisition(drive, ask)
    dest = tmp_path / "reread"
    result = restore_archive(
        dest, drive, _CRYPTO, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    assert result.failures == 0
    assert (dest / "old-only.txt").read_bytes() == b"old-only"
    assert (dest / "a.txt").read_bytes() == b"A-old"
    assert (dest / "latest.txt").read_bytes() == b"latest"
    assert result.stale == [("a.txt", 0)]
    assert acquisition.needed_log == [
        {0: 1, 1: 1},
        {1: 1},
        {0: 1},
    ]
    disc0_blob_reads = [
        blobs for iso, blobs in drive.blob_reads if iso.resolve() == discs[0].resolve()
    ]
    assert len(disc0_blob_reads) == 2
    _assert_acquisition_state(dest)


def test_encrypted_three_iso_hides_paths_and_markers(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A keyfile init that splits across three ISOs must not store any
        source path name or content marker in the raw ISO bytes.
    """
    _source_dir, isos, keyfile, files = _three_volume_cli_archive(
        tmp_path, monkeypatch, encrypt=True,
    )
    assert keyfile is not None
    needles = []
    for rel, data in files.items():
        needles.append(rel.encode())
        needles.append(Path(rel).name.encode())
        needles.append(data[:18])
    for iso in isos:
        raw = iso.read_bytes()
        for needle in needles:
            assert needle not in raw, (iso.name, needle)


def _assert_old_format_baseline(backend: ISOBackend) -> None:
    """Input: legacy backend. Output: asserts the unversioned baseline."""
    found = backend.read_paths(["volume_baseline.json", "volume_baseline.enc"])
    if "volume_baseline.json" in found:
        baseline_raw = json.loads(found["volume_baseline.json"])
        assert "version" not in baseline_raw
        assert "volume_baseline.enc" not in found


def _assert_old_format_manifest(backend: ISOBackend, crypto, marker) -> None:
    """Input: backend, crypto, and marker. Output: asserts format-1 manifest."""
    manifests = load_session_manifests(
        backend, crypto, start_session=marker.first_session, marker=marker,
    )
    assert manifests
    assert all(item.version == 1 for item in manifests)
    session_dir = backend._sessions_root / f"session_{marker.first_session:03d}"
    assert (session_dir / "manifest.enc").is_file()
    assert (session_dir / "enc_mode.json").is_file()
    assert json.loads((session_dir / "enc_mode.json").read_text()) == {"mode": "keyfile"}
    assert not (session_dir / "manifest.json").exists()


def _assert_old_format_discs(discs: list[Path], crypto) -> None:
    """Input: legacy discs and crypto. Output: asserts format-1 control files."""
    for iso in discs:
        backend = ISOBackend(iso, disc_size=_DISC)
        marker_raw = json.loads(backend.read_paths(["volume.json"])["volume.json"])
        assert "format" not in marker_raw
        marker = read_volume_marker(backend)
        assert marker is not None
        assert marker.format == 1
        _assert_old_format_baseline(backend)
        _assert_old_format_manifest(backend, crypto, marker)


def _assert_encrypted_spool(spool: Path, crypto, forbidden: tuple[bytes, ...]) -> None:
    """Input: spool, crypto, and plaintexts. Output: asserts encrypted blobs."""
    blobs = [path for path in spool.rglob("*") if path.is_file()]
    assert blobs
    for blob in blobs:
        data = blob.read_bytes()
        for text in forbidden:
            assert text not in data
        crypto.decrypt(data)


def test_old_format_delta_chain_reverse_restore(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Format-1 volume.json, unversioned plaintext v1 baselines, and
        manifest version 1. Encrypted blobs form a delta chain across
        discs 0, 1, and 2. Restore from disc 2, pause after that disc,
        then acquire 1 then 0. Final bytes match v2. The spool holds
        ciphertext and is removed at the end.
    """
    crypto = KeyfileCrypto.from_kek(fill(b"K", 32))
    original = b"V0-PLAINTEXT-MARKER-" + fill(b"A", 4000)
    updated = original[:-8] + b"V1-TAIL!"
    latest = updated[:-8] + b"V2-TAIL!"
    iso0, state0, origin0 = _stage(tmp_path, 0, {"changed.txt": original}, crypto)
    iso1, state1, origin1 = _stage(
        tmp_path, 1, {}, crypto, prior_state=state0, prior_origin=origin0,
        delta={"changed.txt": (original, updated)},
    )
    iso2, _state2, _origin2 = _stage(
        tmp_path, 2, {}, crypto, prior_state=state1, prior_origin=origin1,
        delta={"changed.txt": (updated, latest)},
    )
    discs = [iso0, iso1, iso2]
    _assert_old_format_discs(discs, crypto)
    backend, catalog, manifests = _source(iso2, crypto)
    dest = tmp_path / "legacy"
    paused = restore_archive(
        dest, backend, crypto, catalog, manifests=manifests,
        acquisition=_ReverseAcquisition(discs, pause_first=True),
    )
    assert paused.paused
    assert not (dest / "changed.txt").exists()
    spool = state_path(dest, _ID).with_suffix(".spool")
    _assert_encrypted_spool(spool, crypto, (original, updated, latest))
    backend, catalog, manifests = _source(iso2, crypto)
    result = restore_archive(
        dest, backend, crypto, catalog, manifests=manifests,
        acquisition=_ReverseAcquisition(discs, pause_first=False),
    )
    assert result.failures == 0
    assert (dest / "changed.txt").read_bytes() == latest
    assert not spool.exists()


def test_format1_learn_replan_through_swappable_drive(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A format 1 archive uses a v1 plaintext baseline and manifest
        version 1. The drive serves disc 1 then disc 0. The plan learns
        the full base on disc 0 and restores the newer delta on disc 1.
    """
    crypto = KeyfileCrypto.from_kek(fill(b"K", 32))
    original = b"format-one-base" + fill(b"A", 1000)
    latest = original[:-8] + b"V1-TAIL!"
    iso0, state, origin = _stage(tmp_path, 0, {"changed.txt": original}, crypto)
    iso1, _state, _origin = _stage(
        tmp_path, 1, {}, crypto, prior_state=state, prior_origin=origin,
        delta={"changed.txt": (original, latest)},
    )
    discs = [iso0, iso1]
    for iso in discs:
        backend = ISOBackend(iso, disc_size=_DISC)
        marker = read_volume_marker(backend)
        assert marker.format == 1
        assert all(item.version == 1 for item in load_session_manifests(
            backend, crypto, start_session=marker.first_session, marker=marker,
        ))
    backend, catalog, manifests = _source(iso1, crypto)
    drive = SwappableDrive(discs)
    drive.insert(iso1)
    dest = tmp_path / "format1-drive"
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        drive.insert(iso0)
        return ""

    result = restore_archive(
        dest, drive, crypto, catalog, manifests=manifests,
        acquisition=DiscAcquisition(drive, ask=ask),
    )
    assert result.failures == 0
    assert result.missing == []
    assert len(prompts) == 1
    assert (dest / "changed.txt").read_bytes() == latest
