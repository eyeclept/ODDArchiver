"""Test encrypted manifest trust, mode memory, and metadata parse bounds."""
# Imports
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from oddarchiver import cli
from oddarchiver.catalog import remember_encryption_mode, remembered_encryption_mode
from oddarchiver.crypto import KeyfileCrypto, generate_keyfile
from oddarchiver.manifest import Manifest, load_session_manifests, read_manifest, write_manifest
from oddarchiver.volume import BaselineReadError, parse_volume_marker_bytes, read_volume_baseline

# Globals
logger = logging.getLogger(__name__)
_log = logger

# Functions


class Backend:
    """Serve a fixed set of disc control files."""

    def __init__(self, files):
        self.files = files
        self.calls = []

    def read_paths(self, paths):
        """Return requested control files and record the request."""
        self.calls.append(paths)
        return {name: self.files[name] for name in paths if name in self.files}

    def list_session_numbers(self):
        """Return the session slots present in the test disc."""
        return [0]

    def mediainfo(self):
        """Return the disc label used by the confirmation prompt."""
        return SimpleNamespace(label="TEST")


def _marker(index=0, first=0, archive_id="archive-trust"):
    """Return volume.json bytes for one disc."""
    return json.dumps({
        "archive_id": archive_id, "volume_index": index,
        "first_session": first, "created_at": "today",
        "previous_volume_label": "", "format": 1,
    }).encode()


def _manifest(tmp_path, *, session=0, crypto=None):
    """Write a checksummed manifest and return its bytes."""
    root = tmp_path / "session_000"
    root.mkdir(exist_ok=True)
    write_manifest(root, Manifest(
        version=1, session=session, timestamp="today", source="source",
        label="TEST", based_on_session=None, encryption={"mode": "keyfile"},
        entries=[], deleted=[], manifest_checksum="",
    ), crypto)
    return (root / ("manifest.enc" if crypto else "manifest.json")).read_bytes()


def _key(tmp_path):
    """Create a real keyfile backend."""
    path = tmp_path / "key"
    generate_keyfile(str(path))
    return KeyfileCrypto(str(path)), str(path)


def test_plaintext_manifest_with_key_names_file(tmp_path):
    """Reject a plaintext manifest even without a format or mode marker."""
    crypto, _ = _key(tmp_path)
    data = _manifest(tmp_path)
    path = tmp_path / "session_000" / "manifest.json"
    with pytest.raises(ValueError, match=r"manifest\.json"):
        read_manifest(path, crypto)
    backend = Backend({"session_000/manifest.json": data})
    with pytest.raises(ValueError, match=r"session_000/manifest\.json"):
        load_session_manifests(backend, crypto)


def test_manifest_session_must_match_slot(tmp_path):
    """Reject a valid checksum whose session is not its directory slot."""
    data = _manifest(tmp_path, session=4)
    with pytest.raises(ValueError, match=r"session_000/manifest\.json"):
        load_session_manifests(Backend({"session_000/manifest.json": data}))


def test_failed_session_listing_uses_probe(tmp_path):
    """Read the known manifest when the disc cannot list directories."""
    data = _manifest(tmp_path)
    backend = Backend({"session_000/manifest.json": data})
    def fail_listing():
        """Simulate a drive that cannot list its session directories."""
        raise RuntimeError("session listing failed")
    backend.list_session_numbers = fail_listing
    manifests = load_session_manifests(backend, raw=backend.files)
    assert [m.session for m in manifests] == [0]


def test_control_batch_contains_baselines_and_refuses_encrypted_baseline(monkeypatch):
    """Refuse a disc whose only encrypted metadata is its baseline."""
    monkeypatch.delenv("ODDARCHIVER_PASSPHRASE", raising=False)
    backend = Backend({
        "volume.json": _marker(),
        "session_000/enc_mode.json": b'{"mode":"none"}',
        "volume_baseline.enc": b"encrypted baseline",
    })
    with pytest.raises(ValueError, match="encrypted metadata"):
        cli._crypto_and_manifests(backend)
    assert len(backend.calls) == 1
    assert "volume_baseline.enc" in backend.calls[0]
    assert "volume_baseline.json" in backend.calls[0]


class TtyInput(io.StringIO):
    """Provide an interactive answer to one question."""

    def isatty(self):
        """Report an interactive input stream."""
        return True


class TtyOutput(io.StringIO):
    """Capture an interactive confirmation question."""

    def isatty(self):
        """Report an interactive output stream."""
        return True


def test_stripped_disc_confirmation_records_none(tmp_path, monkeypatch, capsys):
    """Remember a tty yes and skip confirmation on the next read."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ODDARCHIVER_PASSPHRASE", raising=False)
    backend = Backend({"volume.json": _marker()})
    out = TtyOutput()
    monkeypatch.setattr("sys.stdin", TtyInput("y\n"))
    monkeypatch.setattr("sys.stdout", out)
    cli._crypto_and_manifests(backend)
    assert "Is archive TEST a plaintext archive? [y/N]" in out.getvalue()
    assert remembered_encryption_mode("archive-trust") == "none"
    monkeypatch.setattr("sys.stdin", TtyInput("n\n"))
    second = TtyOutput()
    monkeypatch.setattr("sys.stdout", second)
    cli._crypto_and_manifests(backend)
    assert "plaintext archive?" not in second.getvalue()


def test_stripped_disc_no_and_non_tty_both_exit(tmp_path, monkeypatch, capsys):
    """Reject a tty no and a nonterminal read without saving the mode."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ODDARCHIVER_PASSPHRASE", raising=False)
    backend = Backend({"volume.json": _marker(archive_id="archive-no")})
    monkeypatch.setattr("sys.stdin", TtyInput("n\n"))
    monkeypatch.setattr("sys.stdout", TtyOutput())
    with pytest.raises(SystemExit) as exc:
        cli._crypto_and_manifests(backend)
    assert exc.value.code == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    with pytest.raises(SystemExit) as exc:
        cli._crypto_and_manifests(backend)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Run the same command once from a terminal" in err
    assert remembered_encryption_mode("archive-no") is None


def test_mode_memory_conflict_is_not_overwritten(tmp_path, monkeypatch):
    """Reject a different authenticated mode without changing local memory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    remember_encryption_mode("archive-conflict", "none")
    crypto, key_path = _key(tmp_path)
    backend = Backend({
        "volume.json": _marker(archive_id="archive-conflict"),
        "session_000/enc_mode.json": b'{"mode":"keyfile"}',
        "session_000/manifest.enc": _manifest(tmp_path, crypto=crypto),
    })
    with pytest.raises(ValueError, match="Local disc record"):
        cli._crypto_and_manifests(backend, key_path=key_path)
    assert remembered_encryption_mode("archive-conflict") == "none"


def test_manifest_entries_cap_names_file(tmp_path, monkeypatch):
    """Reject a manifest with more than one million entries before conversion."""
    import oddarchiver.manifest as manifest_mod
    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    original = manifest_mod.json.loads
    class TooMany(list):
        """Report a too-large entry count without allocating entries."""
        def __len__(self):
            """Report the declared number of entries."""
            return 1_000_001
    def parse(data, *args, **kwargs):
        """Supply a large entry collection to the manifest parser."""
        if data == "{}":
            return {"entries": TooMany(), "manifest_checksum": ""}
        return original(data, *args, **kwargs)
    monkeypatch.setattr(manifest_mod.json, "loads", parse)
    with pytest.raises(ValueError, match="manifest.json"):
        read_manifest(path)


def test_baseline_history_cap_names_file(monkeypatch):
    """Reject a baseline with more than two million history paths."""
    import oddarchiver.volume as volume_mod
    class TooMany(dict):
        """Report a too-large path count without allocating paths."""
        def __len__(self):
            """Report the declared number of paths."""
            return 2_000_001
    original = volume_mod.json.loads
    def parse(data, *args, **kwargs):
        """Supply a large history map to the baseline parser."""
        if data == '{}':
            return {"version": 2, "state": {}, "origin": {}, "volumes": [], "history": TooMany()}
        return original(data, *args, **kwargs)
    monkeypatch.setattr(volume_mod.json, "loads", parse)
    with pytest.raises(BaselineReadError, match="volume_baseline.json"):
        read_volume_baseline(Backend({"volume_baseline.json": b"{}"}), None)


def test_marker_rejects_large_indices():
    """Name the out-of-range marker field before a planner sees it."""
    with pytest.raises(ValueError, match="volume.json.*volume_index"):
        parse_volume_marker_bytes(_marker(index=10_000))
    with pytest.raises(ValueError, match="volume.json.*first_session"):
        parse_volume_marker_bytes(_marker(first=1000))
