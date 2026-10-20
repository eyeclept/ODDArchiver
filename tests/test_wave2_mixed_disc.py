"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Test format 1 encrypted discs with new encrypted sessions and format 2
    downgrade checks.
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
from oddarchiver.catalog import build_archive_catalog
from oddarchiver.cli import dispatch
from oddarchiver.crypto import KeyfileCrypto, generate_keyfile
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, load_session_manifests, write_manifest
from oddarchiver.volume import (
    BaselineReadError, read_volume_baseline, read_volume_marker,
)

# Globals
logger = logging.getLogger(__name__)
_log = logger
_DISC = 3 * 2**20
_TIMESTAMP = "2026-10-15T12:00:00Z"
_ARCHIVE_ID = "mixed-disc-format-test"

# Functions


@pytest.fixture(autouse=True)
def _isolate_cli(tmp_path: Path, monkeypatch) -> None:
    """
    Input:  tmp_path and monkeypatch
    Output: None
    Details: Keep the CLI cache and local disc records inside this test.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cache = tmp_path / "cache" / "oddarchiver"
    cache.mkdir(parents=True)
    monkeypatch.setattr("oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)


def _args(**overrides) -> argparse.Namespace:
    """
    Input:  overrides for the CLI argument namespace
    Output: Complete argument namespace for the test commands
    """
    values = dict(
        command="ls", source=None, dest=None, device="/dev/sr0",
        test_iso=None, label="MIXED", encrypt="none", key=None,
        dry_run=False, no_cache=False, disc_size="3mb", prefill=None,
        session=None, force=False, level="fast", mirror=None,
        recover_scan=False, paths=None, show_deleted=False, path=None,
        rescue_key=None, skip_volumes=None, unskip_volumes=None,
        all_disks=False, embed_software=None, skip_large=False,
        slow=False, read_mode="sequential", all_files=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _manifest(n: int, version: int, entries: list[ManifestEntry] | None = None) -> Manifest:
    """
    Input:  session number, format version, and optional file entries
    Output: Manifest with a valid checksum after write_manifest
    """
    return Manifest(
        version=version, session=n, timestamp=_TIMESTAMP, source="/src",
        label="MIXED", based_on_session=n - 1 if n else None,
        encryption={"mode": "keyfile"}, entries=entries or [], deleted=[],
        manifest_checksum="",
    )


def _write_old_entries(
    staging: Path, n: int, crypto: KeyfileCrypto, current: dict[str, bytes],
) -> list[ManifestEntry]:
    """
    Input:  staging directory, session number, key, and current files
    Output: Encrypted file entries for the old disc manifest
    """
    entries = []
    for path, content in current.items():
        blob = f"session_{n:03d}/full/{hashlib.sha256(path.encode()).hexdigest()}"
        target = staging / blob
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(crypto.encrypt(content))
        entries.append(ManifestEntry(
            path=path, type="full", result_checksum=hashlib.sha256(content).hexdigest(),
            full_size_bytes=len(content), file=blob,
        ))
    return entries


def _old_disc(
    tmp_path: Path, n: int, crypto: KeyfileCrypto,
    prior: dict[str, bytes], current: dict[str, bytes], *, marker_format: int | None = None,
    first_version: int = 1, first_encrypted: bool = True,
) -> Path:
    """
    Input:  volume number, key, prior files, current files, and format options
    Output: ISO with a directly written format 1 marker and baseline
    Details: Writes a 02d325d disc. The manifest is manifest.enc, the
        baseline is plaintext with only state and origin, and the disc has
        no enc_mode.json.
    """
    staging = tmp_path / f"staging-{n}"
    session_dir = staging / f"session_{n:03d}"
    session_dir.mkdir(parents=True)
    entries = _write_old_entries(staging, n, crypto, current)
    write_manifest(
        session_dir, _manifest(n, first_version, entries),
        crypto if first_encrypted else None,
    )
    if first_encrypted:
        assert (session_dir / "manifest.enc").is_file()
    marker = dict(
        archive_id=_ARCHIVE_ID, volume_index=n, first_session=n,
        created_at=_TIMESTAMP, previous_volume_label=f"VOL{n-1}" if n else "",
    )
    if marker_format is not None:
        marker["format"] = marker_format
    (staging / "volume.json").write_text(json.dumps(marker), encoding="utf-8")
    if n:
        (staging / "volume_baseline.json").write_text(json.dumps({
            "state": {path: hashlib.sha256(data).hexdigest() for path, data in prior.items()},
            "origin": {path: i for i, path in enumerate(prior)},
        }), encoding="utf-8")
    iso = tmp_path / ("arc.iso" if n == 0 else f"arc.vol{n}.iso")
    ISOBackend(iso, disc_size=_DISC).init(staging, label="MIXED")
    return iso


def _archive(tmp_path: Path):
    """
    Input:  isolated test directory
    Output: source, three old discs, keyfile, key backend, and original files
    """
    keyfile = tmp_path / "k.key"
    generate_keyfile(str(keyfile))
    crypto = KeyfileCrypto(str(keyfile))
    source = tmp_path / "src"
    source.mkdir()
    files = {
        "first.txt": b"first disc bytes",
        "middle.txt": b"middle disc bytes",
        "last.txt": b"last disc bytes",
    }
    for path, content in files.items():
        (source / path).write_bytes(content)
    discs = [
        _old_disc(tmp_path, 0, crypto, {}, {"first.txt": files["first.txt"]}),
        _old_disc(tmp_path, 1, crypto, {"first.txt": files["first.txt"]},
                  {"middle.txt": files["middle.txt"]}),
        _old_disc(tmp_path, 2, crypto,
                  {name: files[name] for name in ("first.txt", "middle.txt")},
                  {"last.txt": files["last.txt"]}),
    ]
    return source, discs, keyfile, crypto, files


def _append_current(source: Path, iso: Path, keyfile: Path) -> bytes:
    """
    Input:  source, old later disc, and keyfile
    Output: bytes written by the new session
    """
    content = b"new session bytes"
    (source / "new.txt").write_bytes(content)
    assert dispatch(_args(
        command="sync", source=str(source), test_iso=str(iso), key=str(keyfile),
    )) == 0
    assert (iso.with_suffix(".d") / "session_003" / "manifest.enc").is_file()
    return content


def _assert_mixed_cli_commands(
    iso: Path, keyfile: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Input:  mixed disc, keyfile, and CLI output capture
    Output: Each listing command succeeds with expected diagnostics
    """
    for command, all_disks in (
        ("ls", False), ("ls", True), ("status", False),
        ("history", False), ("verify", False),
    ):
        assert dispatch(_args(
            command=command, test_iso=str(iso), key=str(keyfile),
            all_disks=all_disks,
        )) == 0
        captured = capsys.readouterr()
        assert "manifest.json" not in captured.err
        if all_disks:
            assert "plaintext metadata: volume_baseline.json" in captured.err


def _assert_restored_files(dest: Path, files: dict[str, bytes]) -> None:
    """
    Input:  restored destination and expected file bytes
    Output: Every restored file matches its source
    """
    for path, content in files.items():
        assert (dest / path).read_bytes() == content


def test_mixed_disc_cli_commands_and_restores(tmp_path, capsys):
    """
    Input:  format 1 three-disc archive with a current-code sync
    Output: Commands succeed and restored files have their source bytes
    """
    source, discs, keyfile, _crypto, files = _archive(tmp_path)
    files["new.txt"] = _append_current(source, discs[2], keyfile)
    capsys.readouterr()
    _assert_mixed_cli_commands(discs[2], keyfile, capsys)
    single = tmp_path / "single"
    assert dispatch(_args(
        command="restore", test_iso=str(discs[2]), key=str(keyfile),
        dest=str(single), force=True,
    )) == 0
    assert (single / "last.txt").read_bytes() == files["last.txt"]
    assert (single / "new.txt").read_bytes() == files["new.txt"]
    all_dest = tmp_path / "all"
    assert dispatch(_args(
        command="restore", test_iso=str(discs[2]), key=str(keyfile),
        dest=str(all_dest), all_disks=True, all_files=True, force=True,
    )) == 0
    _assert_restored_files(all_dest, files)


def test_first_encrypted_manifest_refuses_plaintext_baseline(tmp_path, capsys):
    """
    Input:  marker without format and a valid first-session v2 manifest.enc
    Output: Plaintext baseline is refused, and the CLI reports no traceback
    """
    _source, _discs, keyfile, crypto, files = _archive(tmp_path)
    iso = _old_disc(tmp_path, 3, crypto, files, {}, first_version=2,
                    first_encrypted=True)
    backend = ISOBackend(iso, disc_size=_DISC)
    with pytest.raises(BaselineReadError, match="volume_baseline.json"):
        read_volume_baseline(backend, crypto)
    assert dispatch(_args(
        command="ls", test_iso=str(iso), key=str(keyfile), all_disks=True,
    )) == 1
    error = capsys.readouterr().err
    assert "error: This encrypted format 2 disc" in error
    assert "Traceback" not in error
    for command in ("restore", "browse"):
        assert dispatch(_args(
            command=command, test_iso=str(iso), key=str(keyfile),
            dest=str(tmp_path / f"restore-{command}"), all_disks=True,
        )) == 1
        assert "Traceback" not in capsys.readouterr().err


def test_manifest_transition_and_marker_downgrade(tmp_path):
    """
    Input:  02d325d disc, then plaintext and mismatched manifests
    Output: Every plaintext manifest under a key is refused by name. A
        manifest whose session differs from its slot is refused. A
        plaintext baseline on a format 2 marker is refused.
    """
    _source, discs, _keyfile, crypto, _files = _archive(tmp_path)
    iso = discs[2]
    root = iso.with_suffix(".d")
    old_session = root / "session_002"
    write_manifest(old_session, _manifest(2, 1), None)
    backend = ISOBackend(iso, disc_size=_DISC)
    marker = read_volume_marker(backend)
    with pytest.raises(ValueError, match="session_002/manifest.json"):
        load_session_manifests(backend, crypto, start_session=2, marker=marker)
    plain = load_session_manifests(backend, None, start_session=2, marker=marker)
    assert read_volume_baseline(backend, None, marker=marker, manifests=plain).version == 1
    write_manifest(old_session, _manifest(2, 1), crypto)
    next_session = root / "session_003"
    next_session.mkdir()
    write_manifest(next_session, _manifest(3, 2), crypto)
    backend = ISOBackend(iso, disc_size=_DISC)
    manifests = load_session_manifests(backend, crypto, start_session=2, marker=marker)
    assert [m.session for m in manifests] == [2, 3]
    forged = root / "session_004"
    forged.mkdir()
    write_manifest(forged, _manifest(4, 1), None)
    backend = ISOBackend(iso, disc_size=_DISC)
    with pytest.raises(ValueError, match="session_004/manifest.json"):
        load_session_manifests(backend, crypto, start_session=2, marker=marker)
    (forged / "manifest.json").unlink()
    forged.rmdir()
    write_manifest(next_session, _manifest(3, 1), None)
    backend = ISOBackend(iso, disc_size=_DISC)
    with pytest.raises(ValueError, match="session_003/manifest.json"):
        load_session_manifests(backend, crypto, start_session=2, marker=marker)
    write_manifest(next_session, _manifest(5, 2), crypto)
    backend = ISOBackend(iso, disc_size=_DISC)
    with pytest.raises(ValueError, match="session_003/manifest.enc"):
        load_session_manifests(backend, crypto, start_session=2, marker=marker)
    write_manifest(next_session, _manifest(3, 2), crypto)
    marker_file = root / "volume.json"
    raw = json.loads(marker_file.read_text(encoding="utf-8"))
    raw["format"] = 2
    marker_file.write_text(json.dumps(raw), encoding="utf-8")
    backend = ISOBackend(iso, disc_size=_DISC)
    marker = read_volume_marker(backend)
    with pytest.raises(BaselineReadError, match="volume_baseline.json"):
        read_volume_baseline(backend, crypto, marker=marker, manifests=manifests)


def test_unknown_size_row_keeps_columns(tmp_path, capsys):
    """
    Input:  format 1 baseline with an old state-only path
    Output: Exact ls row has unknown size and an empty timestamp
    """
    _source, discs, keyfile, _crypto, _files = _archive(tmp_path)
    assert dispatch(_args(
        command="ls", test_iso=str(discs[2]), key=str(keyfile), all_disks=True,
    )) == 0
    output = capsys.readouterr().out
    expected = (
        f"{'first.txt':<40} {'?':>10} 000           {'':<22} "
        "the disc that holds session 0"
    )
    assert expected in output.splitlines()


def _assert_spill_catalog(
    new_disc: Path, crypto: KeyfileCrypto, files: dict[str, bytes],
) -> None:
    """
    Input:  spilled disc, key, and all source files
    Output: Format 2 marker, encrypted baseline, and complete catalog
    """
    assert new_disc.is_file()
    backend = ISOBackend(new_disc, disc_size=_DISC)
    marker = read_volume_marker(backend)
    assert marker is not None and marker.format == 2 and marker.volume_index == 3
    assert (new_disc.with_suffix(".d") / "volume_baseline.enc").is_file()
    assert not (new_disc.with_suffix(".d") / "volume_baseline.json").exists()
    manifests = load_session_manifests(
        backend, crypto, start_session=marker.first_session, marker=marker,
    )
    baseline = read_volume_baseline(backend, crypto, marker=marker, manifests=manifests)
    assert baseline is not None and baseline.version == 2
    catalog = build_archive_catalog(backend, crypto, manifests, marker, label="MIXED")
    assert set(catalog.history) == set(files)


def test_second_sync_and_spill_keep_old_catalog(tmp_path, capsys):
    """
    Input:  old disc after one new session and enough data to fill the disc
    Output: Next sync succeeds, creates format 2, and keeps all file paths
    """
    source, discs, keyfile, crypto, files = _archive(tmp_path)
    files["new.txt"] = _append_current(source, discs[2], keyfile)
    files["large-a.bin"] = bytes(range(256)) * 6200
    files["large-b.bin"] = bytes(range(255, -1, -1)) * 6200
    for path in ("large-a.bin", "large-b.bin"):
        (source / path).write_bytes(files[path])
    assert dispatch(_args(
        command="sync", source=str(source), test_iso=str(discs[2]),
        key=str(keyfile),
    )) == 0
    new_disc = tmp_path / "arc.vol3.iso"
    _assert_spill_catalog(new_disc, crypto, files)
    assert "old encrypted disc" in capsys.readouterr().err
