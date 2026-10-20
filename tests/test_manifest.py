"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for manifest.py. Write/read round-trip, checksum validation,
    SUSPECT detection, disc-state replay, and atomic write.
"""
# Imports
from collections.abc import Callable
import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.manifest import (
    Manifest,
    ManifestEntry,
    build_disc_origin,
    build_disc_state,
    read_manifest,
    write_manifest,
)
from tests.helpers import _make_entry, _make_manifest

# Globals
logger = logging.getLogger(__name__)
_TIMESTAMP = "2026-04-19T12:00:00Z"

# Functions


def _assert_round_trip_fields(loaded: Manifest, original: Manifest) -> None:
    """
    Input:  loaded and original manifests
    Output: None
    """
    assert loaded.version == original.version
    assert loaded.session == original.session
    assert loaded.timestamp == original.timestamp
    assert loaded.source == original.source
    assert loaded.label == original.label
    assert loaded.encryption == original.encryption
    assert len(loaded.entries) == 1
    assert loaded.entries[0].path == "a/b.txt"
    assert loaded.entries[0].result_checksum == "dead"


def test_round_trip_preserves_all_fields(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        write_manifest then read_manifest must return an equivalent Manifest
        with all fields intact and suspect=False.
    """
    m = _make_manifest(entries=[_make_entry("a/b.txt", "dead")])
    write_manifest(tmp_path, m)
    loaded = read_manifest(tmp_path / "manifest.json")

    _assert_round_trip_fields(loaded, m)
    assert not loaded.suspect


def test_tampered_manifest_is_suspect(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Editing any field in manifest.json after write must cause
        read_manifest to return suspect=True.
    """
    write_manifest(tmp_path, _make_manifest())
    manifest_path = tmp_path / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["label"] = "TAMPERED"
    manifest_path.write_text(json.dumps(raw))

    loaded = read_manifest(manifest_path)
    assert loaded.suspect


def test_tampered_manifest_logs_warning(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        read_manifest must log a WARNING (not raise) on checksum mismatch.
    """
    write_manifest(tmp_path, _make_manifest())
    manifest_path = tmp_path / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["session"] = 99
    manifest_path.write_text(json.dumps(raw))

    with caplog.at_level(logging.WARNING):
        loaded = read_manifest(manifest_path)
    assert loaded.suspect
    assert any("SUSPECT" in r.message for r in caplog.records)


def test_build_disc_state_replays_sessions(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        build_disc_state with three sessions must return the last known
        checksum for each path, with later sessions overwriting earlier ones.
    """
    m0 = _make_manifest(0, entries=[
        _make_entry("a.txt", "v1"),
        _make_entry("b.txt", "bv1"),
    ])
    m1 = _make_manifest(1, entries=[_make_entry("a.txt", "v2")])
    m2 = _make_manifest(2, entries=[_make_entry("c.txt", "cv1")])

    state = build_disc_state([m0, m1, m2])
    assert state["a.txt"] == "v2"
    assert state["b.txt"] == "bv1"
    assert state["c.txt"] == "cv1"


def _assert_deleted_path(
    build: Callable[[list[Manifest]], dict[str, str] | dict[str, int]],
) -> None:
    """
    Input:  build - disc state or origin builder
    Output: None
    """
    m0 = _make_manifest(0, entries=[_make_entry("remove_me.txt", "x")])
    m1 = _make_manifest(1, deleted=["remove_me.txt"])
    result = build([m0, m1])
    assert "remove_me.txt" not in result


def test_build_disc_state_applies_deleted(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Paths listed in a manifest's deleted list must be absent from the
        resulting disc state.
    """
    _assert_deleted_path(build_disc_state)


def test_build_disc_state_skips_suspect(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        A manifest with suspect=True must be skipped. No exception is raised.
        A WARNING must be logged.
    """
    m0 = _make_manifest(0, entries=[_make_entry("a.txt", "good")])
    m1 = _make_manifest(1, entries=[_make_entry("a.txt", "bad")])
    m1.suspect = True

    with caplog.at_level(logging.WARNING):
        state = build_disc_state([m0, m1])

    assert state["a.txt"] == "good"
    assert any("SUSPECT" in r.message for r in caplog.records)


def test_build_disc_origin_replays_sessions(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        build_disc_origin with three sessions must return the session that
        last wrote each path. a.txt is rewritten in session 1, so its
        recorded origin is 1, not 0; b.txt is untouched after session 0 and
        keeps origin 0; c.txt is first written in session 2.
    """
    m0 = _make_manifest(0, entries=[
        _make_entry("a.txt", "v1"),
        _make_entry("b.txt", "bv1"),
    ])
    m1 = _make_manifest(1, entries=[_make_entry("a.txt", "v2")])
    m2 = _make_manifest(2, entries=[_make_entry("c.txt", "cv1")])

    origin = build_disc_origin([m0, m1, m2])
    assert origin["a.txt"] == 1
    assert origin["b.txt"] == 0
    assert origin["c.txt"] == 2


def test_build_disc_origin_applies_deleted(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Paths listed in a manifest's deleted list must be absent from the
        resulting origin map, same as build_disc_state().
    """
    _assert_deleted_path(build_disc_origin)


def test_build_disc_origin_skips_suspect(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        A manifest with suspect=True must be skipped, so its entries do not
        overwrite an earlier path's recorded origin. A WARNING is logged.
    """
    m0 = _make_manifest(0, entries=[_make_entry("a.txt", "good")])
    m1 = _make_manifest(1, entries=[_make_entry("a.txt", "bad")])
    m1.suspect = True

    with caplog.at_level(logging.WARNING):
        origin = build_disc_origin([m0, m1])

    assert origin["a.txt"] == 0
    assert any("SUSPECT" in r.message for r in caplog.records)


def test_write_manifest_is_atomic(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        If os.replace raises mid-write, the destination manifest.json must
        not exist (no partial file left behind).
    """
    import os
    import oddarchiver.manifest as manifest_mod

    def failing_replace(src, dst):
        Path(src).unlink(missing_ok=True)
        raise OSError("simulated failure")

    monkeypatch.setattr(manifest_mod.os, "replace", failing_replace)

    with pytest.raises(OSError):
        write_manifest(tmp_path, _make_manifest())

    assert not (tmp_path / "manifest.json").exists()


def test_read_manifest_invalid_json_returns_suspect(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        B5 regression: read_manifest on extra bytes after a JSON object must
        return a Manifest with suspect=True and log WARNING. Do not raise JSONDecodeError.
    """
    bad = tmp_path / "manifest.json"
    bad.write_text('{"valid": "json"}extra junk that breaks parse', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        m = read_manifest(bad)

    assert m.suspect
    assert any("cannot parse JSON" in r.message for r in caplog.records)


def test_read_manifest_appended_garbage_returns_suspect(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        B5 regression: extra bytes after valid JSON (as in bugs.txt: echo junk >>)
        must return suspect=True. Do not raise.
    """
    write_manifest(tmp_path, _make_manifest())
    manifest_path = tmp_path / "manifest.json"
    with open(manifest_path, "a", encoding="utf-8") as fh:
        fh.write("\njunk appended by accident")

    with caplog.at_level(logging.WARNING):
        m = read_manifest(manifest_path)

    assert m.suspect


def test_read_manifest_missing_streamed_defaults_false(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A hand-written manifest.json without the streamed key (simulating an
        already-archived disc) parses with streamed=False on every entry.
    """
    from oddarchiver.manifest import _compute_checksum, manifest_checksum_ok

    payload = {
        "version": 1,
        "session": 0,
        "timestamp": _TIMESTAMP,
        "source": "/src",
        "label": "TEST",
        "based_on_session": None,
        "encryption": {"mode": "none"},
        "entries": [
            {
                "path": "a.txt",
                "type": "full",
                "result_checksum": "abc123",
                "full_size_bytes": 4,
                "source_checksum": "",
                "delta_file": "",
                "delta_size_bytes": 0,
                "file": "session_000/full/" + ("ab" * 32),
                "encrypted_dek": "",
            }
        ],
        "deleted": [],
        "drives": [],
        "manifest_checksum": "",
    }
    payload["manifest_checksum"] = _compute_checksum(
        {**payload, "manifest_checksum": ""}
    )
    dest = tmp_path / "manifest.json"
    dest.write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_manifest(dest)
    assert not loaded.suspect
    assert len(loaded.entries) == 1
    assert not loaded.entries[0].streamed
    assert manifest_checksum_ok(loaded)



def test_read_manifest_missing_volume_chunk_fields_defaults_none(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A hand-written manifest.json without volume_chunk/volume_chunk_count
        keys (simulating a disc archived before section 30) parses with both
        fields None on every entry, and manifest_checksum_ok still reports
        clean against that legacy payload.
    """
    from oddarchiver.manifest import _compute_checksum, manifest_checksum_ok

    payload = {
        "version": 1,
        "session": 0,
        "timestamp": _TIMESTAMP,
        "source": "/src",
        "label": "TEST",
        "based_on_session": None,
        "encryption": {"mode": "none"},
        "entries": [
            {
                "path": "a.txt",
                "type": "full",
                "result_checksum": "abc123",
                "full_size_bytes": 4,
                "source_checksum": "",
                "delta_file": "",
                "delta_size_bytes": 0,
                "file": "session_000/full/" + ("ab" * 32),
                "encrypted_dek": "",
            }
        ],
        "deleted": [],
        "drives": [],
        "manifest_checksum": "",
    }
    payload["manifest_checksum"] = _compute_checksum(
        {**payload, "manifest_checksum": ""}
    )
    dest = tmp_path / "manifest.json"
    dest.write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_manifest(dest)
    assert not loaded.suspect
    assert len(loaded.entries) == 1
    assert loaded.entries[0].volume_chunk is None
    assert loaded.entries[0].volume_chunk_count is None
    assert manifest_checksum_ok(loaded)


def test_volume_chunk_fields_round_trip(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A ManifestEntry constructed with volume_chunk/volume_chunk_count set
        must survive write_manifest/read_manifest unchanged.
    """
    entry = ManifestEntry(
        path="big.iso",
        type="full",
        result_checksum="dead",
        full_size_bytes=1234,
        volume_chunk=2,
        volume_chunk_count=5,
    )
    m = _make_manifest(entries=[entry])
    write_manifest(tmp_path, m)
    loaded = read_manifest(tmp_path / "manifest.json")

    assert not loaded.suspect
    assert len(loaded.entries) == 1
    assert loaded.entries[0].volume_chunk == 2
    assert loaded.entries[0].volume_chunk_count == 5


@pytest.mark.parametrize("mode", ["none", "keyfile", "passphrase"])
def test_load_session_manifests_does_not_create_temp_directory(
    tmp_path, monkeypatch, mode,
):
    """
    Input: tmp_path, monkeypatch, encryption mode
    Output: None
    Details: Loading a manifest parses disc bytes without a temporary directory.
    """
    from oddarchiver.crypto import KeyfileCrypto, PassphraseCrypto, generate_keyfile
    from oddarchiver.manifest import load_session_manifests

    if mode == "keyfile":
        key = tmp_path / "key.bin"
        generate_keyfile(str(key))
        crypto = KeyfileCrypto(str(key))
    elif mode == "passphrase":
        crypto = PassphraseCrypto(passphrase="test passphrase")
    else:
        crypto = None
    write_manifest(tmp_path, _make_manifest(), crypto=crypto)
    suffix = "enc" if crypto else "json"
    raw = {f"session_000/manifest.{suffix}": (
        tmp_path / f"manifest.{suffix}"
    ).read_bytes()}
    if crypto:
        raw["session_000/enc_mode.json"] = (
            tmp_path / "enc_mode.json"
        ).read_bytes()
    backend = MagicMock()
    backend.list_session_numbers.return_value = [0]

    def forbid_temp_directory(*args, **kwargs):
        raise AssertionError("Manifest load created a temporary directory")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", forbid_temp_directory)
    loaded = load_session_manifests(backend, crypto=crypto, raw=raw)
    assert len(loaded) == 1
    assert loaded[0].session == 0
    assert not loaded[0].suspect
    assert not list(tmp_path.glob("oddarchiver_manifests_*"))


def test_read_manifest_refuses_plaintext_on_encrypted_format2(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        With a real key, refuse every plaintext manifest.json.
        Without a real key, allow a plaintext archive manifest.
    """
    from oddarchiver.crypto import KeyfileCrypto, generate_keyfile
    key = tmp_path / "k.key"
    generate_keyfile(str(key))
    crypto = KeyfileCrypto(str(key))
    write_manifest(tmp_path, _make_manifest())
    path = tmp_path / "manifest.json"
    with pytest.raises(ValueError, match="manifest.json"):
        read_manifest(path, crypto, format_2=True)
    with pytest.raises(ValueError, match="manifest.json"):
        read_manifest(path, crypto, encrypted_mode=True)
    with pytest.raises(ValueError, match="manifest.json"):
        read_manifest(path, crypto)
    loaded = read_manifest(path)
    assert not loaded.suspect
    assert loaded.session == 0


def test_read_manifest_wrong_key_stays_suspect(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A manifest.enc that does not decrypt stays a SUSPECT manifest.
        The format 2 change does not alter this path.
    """
    from oddarchiver.crypto import KeyfileCrypto, generate_keyfile
    right, wrong = tmp_path / "r.key", tmp_path / "w.key"
    generate_keyfile(str(right))
    generate_keyfile(str(wrong))
    write_manifest(tmp_path, _make_manifest(), KeyfileCrypto(str(right)))
    loaded = read_manifest(tmp_path / "manifest.enc", KeyfileCrypto(str(wrong)))
    assert loaded.suspect
    assert loaded.session == -1
