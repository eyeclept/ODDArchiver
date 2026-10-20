"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    End-to-end tests for CLI dispatch: init, sync, history, status commands
    exercised against ISOBackend (no physical disc required).
"""
# Imports
import argparse
import hashlib
import json
from pathlib import Path

import pytest

from oddarchiver.cli import dispatch
from oddarchiver.crypto import generate_keyfile
from oddarchiver.disc import ISOBackend, parse_disc_size
from oddarchiver.manifest import read_manifest

# Globals
SMALL_DISC = 50 * 2**20  # 50 MiB. Room for two sessions.


# Functions


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides for the Namespace
    Output: argparse.Namespace with sensible defaults for test commands
    Details:
        Provides defaults so callers only specify what differs.
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
        "slow": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# Tests


def test_init_exits_0_and_produces_session0_manifest(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        init against a fresh ISOBackend must exit 0 and write session_000/manifest.json.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")

    iso = tmp_path / "test.iso"
    args = _make_args(command="init", source=str(source), test_iso=str(iso))

    rc = dispatch(args)

    assert rc == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    manifest_bytes = backend.read_path("session_000/manifest.json")
    manifest_data = json.loads(manifest_bytes)
    assert manifest_data["session"] == 0
    assert manifest_data["label"] == "TEST"
    assert len(manifest_data["entries"]) == 1


def test_sync_no_changes_exits_0_no_second_burn(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        sync with identical source after init must exit 0 and leave session count at 1.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"stable")

    iso = tmp_path / "test.iso"
    init_args = _make_args(command="init", source=str(source), test_iso=str(iso))
    assert dispatch(init_args) == 0

    sync_args = _make_args(command="sync", source=str(source), test_iso=str(iso))
    rc = dispatch(sync_args)

    assert rc == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 1


def test_sync_changed_file_exits_0_and_produces_session1_manifest(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        sync after modifying a file must exit 0 and produce session_001/manifest.json.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"version 1")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "a.txt").write_bytes(b"version 2 - different content")

    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))

    assert rc == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 2
    manifest_bytes = backend.read_path("session_001/manifest.json")
    manifest_data = json.loads(manifest_bytes)
    assert manifest_data["session"] == 1


def test_history_prints_one_row_per_session(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        After init plus sync, history must print two rows that include 000 and 001.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"v1")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    (source / "b.txt").write_bytes(b"new file")
    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0

    capsys.readouterr()  # clear init/sync output
    rc = dispatch(_make_args(command="history", test_iso=str(iso)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "000" in out
    assert "001" in out


def test_sync_deletion_only_writes_session_and_restore_has_no_ghost(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        B4 regression: init with file1.txt + file2.txt, delete file1.txt,
        sync must detect the deletion and write session 1.
        Subsequent restore must not contain file1.txt.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "file1.txt").write_bytes(b"will be deleted")
    (source / "file2.txt").write_bytes(b"stays")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    # Delete file1.txt. The only change is a deletion.
    (source / "file1.txt").unlink()
    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))

    assert rc == 0
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 2, "deletion-only sync must write a new session"

    # Confirm the session 1 manifest records the deletion.
    manifest_bytes = backend.read_path("session_001/manifest.json")
    manifest_data = json.loads(manifest_bytes)
    assert "file1.txt" in manifest_data["deleted"]

    # Restore. file1.txt must not appear.
    dest = tmp_path / "dest"
    from oddarchiver.crypto import NullCrypto
    from oddarchiver.restore import restore
    restore(dest, backend, NullCrypto())
    assert not (dest / "file1.txt").exists(), "ghost file: deleted file must not appear in restore"
    assert (dest / "file2.txt").exists()


def test_status_shows_suspect_on_bad_checksum(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        After init, tamper with the session 0 manifest. The status command must print SUSPECT.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")

    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    manifest_path = backend._sessions_root / "session_000" / "manifest.json"
    text = manifest_path.read_text()
    manifest_path.write_text(text.replace("TEST", "TAMPERED"))

    capsys.readouterr()
    rc = dispatch(_make_args(command="status", test_iso=str(iso)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "SUSPECT" in out


def test_unsafe_keyfile_refused_with_clean_error(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        A keyfile archive opened with a group- or world-readable keyfile
        exits 1. stderr names the chmod fix and holds no traceback.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    key = tmp_path / "archive.key"
    generate_keyfile(str(key))
    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        encrypt="keyfile", key=str(key),
    )) == 0
    key.chmod(0o644)
    capsys.readouterr()

    rc = dispatch(_make_args(command="status", test_iso=str(iso), key=str(key)))

    err = capsys.readouterr().err
    assert rc == 1
    assert f"chmod 600 {key}" in err
    assert "Traceback" not in err


def test_status_counts_archive_sessions_not_mmc_sessions(tmp_path, capsys, monkeypatch):
    """
    Input:  tmp_path, capsys, monkeypatch
    Output: None
    Details:
        BD-R SRM+POW reports one MMC session after any number of appends.
        A two-session archive whose mediainfo says 1 must still show
        "Sessions:  2" in status, the same count that history lists.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(command="init", source=str(source), test_iso=str(iso))) == 0
    (source / "b.txt").write_bytes(b"second session")
    assert dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso))) == 0

    real_mediainfo = ISOBackend.mediainfo

    def pow_mediainfo(self):
        info = real_mediainfo(self)
        info.session_count = 1
        return info

    monkeypatch.setattr(ISOBackend, "mediainfo", pow_mediainfo)
    capsys.readouterr()
    assert dispatch(_make_args(command="status", test_iso=str(iso))) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "Sessions:  2" in lines


class _CorruptAfterBurnISO(ISOBackend):
    """ISOBackend that overwrites stored full blobs after each burn."""

    def init(self, staging, label, expected_session_count=0):
        super().init(staging, label, expected_session_count)
        self._corrupt_full_blobs()

    def append(self, staging, label, expected_session_count):
        super().append(staging, label, expected_session_count)
        self._corrupt_full_blobs()

    def _corrupt_full_blobs(self) -> None:
        """
        Input:  None
        Output: None
        Details:
            Overwrites every session_*/full/* blob with garbage so a later
            checksum verify can see media-level corruption.
        """
        for blob in self._sessions_root.glob("session_*/full/*"):
            if blob.is_file():
                blob.write_bytes(b"CORRUPTED GARBAGE")


def _spy_verify_levels(monkeypatch) -> list[str]:
    """Record verify(level=) values without skipping the real call."""
    import oddarchiver.verify as verify_mod

    levels: list[str] = []
    real = verify_mod.verify

    def spy(*args, **kwargs):
        levels.append(kwargs["level"])
        return real(*args, **kwargs)

    monkeypatch.setattr(verify_mod, "verify", spy)
    return levels


def test_init_post_burn_verify_defaults_to_fast(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        init without --slow must call verify at fast level. Verify must
        run; it cannot be skipped.
    """
    levels = _spy_verify_levels(monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "test.iso"
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))
    assert rc == 0
    assert levels == ["fast"]


def test_init_slow_post_burn_verify_uses_checksum(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        init --slow must call verify at checksum level.
    """
    levels = _spy_verify_levels(monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "test.iso"
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso), slow=True,
    ))
    assert rc == 0
    assert levels == ["checksum"]


def test_sync_slow_post_burn_verify_uses_checksum(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        sync --slow must call verify at checksum level after the append.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"version 1")
    iso = tmp_path / "test.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
    )) == 0
    (source / "a.txt").write_bytes(b"version 2 - different content")
    levels = _spy_verify_levels(monkeypatch)
    rc = dispatch(_make_args(
        command="sync", source=str(source), test_iso=str(iso), slow=True,
    ))
    assert rc == 0
    assert levels == ["checksum"]


def test_post_burn_verify_cannot_be_skipped_via_config(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        config.toml post_burn_verify = false must not skip post-burn
        verify. The burn path always calls verify at fast unless --slow.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text("post_burn_verify = false\n", encoding="utf-8")
    monkeypatch.setattr("oddarchiver.config.default_config_path", lambda: cfg)
    levels = _spy_verify_levels(monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "test.iso"
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))
    assert rc == 0
    assert levels == ["fast"]


def test_slow_catches_corrupted_blob_default_does_not(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        After the burn, stored blob bytes are overwritten with garbage.
        Default post-burn verify still exits 0. --slow exits nonzero.
    """
    def fake_make(args):
        return _CorruptAfterBurnISO(
            Path(args.test_iso),
            disc_size=parse_disc_size(getattr(args, "disc_size", "50mb")),
        )

    monkeypatch.setattr("oddarchiver.cli._make_backend", fake_make)

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")

    fast_iso = tmp_path / "fast.iso"
    rc_fast = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(fast_iso),
    ))
    assert rc_fast == 0
    fast_err = capsys.readouterr().err
    assert "post-burn verify failed" not in fast_err

    slow_iso = tmp_path / "slow.iso"
    rc_slow = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(slow_iso), slow=True,
    ))
    captured = capsys.readouterr()
    assert rc_slow == 1
    assert "post-burn verify failed" in captured.err

