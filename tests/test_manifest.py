"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for Task 3: manifest.py — write/read round-trip, checksum validation,
    SUSPECT detection, disc-state replay, and atomic write.
"""
# Imports
import json
import logging
from pathlib import Path

import pytest

from oddarchiver.manifest import (
    Manifest,
    ManifestEntry,
    build_disc_state,
    read_manifest,
    write_manifest,
)

# Globals
_TIMESTAMP = "2026-04-19T12:00:00Z"

# Functions


def _make_manifest(session: int = 0, entries=None, deleted=None) -> Manifest:
    """Build a minimal valid Manifest for testing."""
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
    return ManifestEntry(
        path=path,
        type="full",
        result_checksum=checksum,
        full_size_bytes=100,
    )


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

    assert loaded.version == m.version
    assert loaded.session == m.session
    assert loaded.timestamp == m.timestamp
    assert loaded.source == m.source
    assert loaded.label == m.label
    assert loaded.encryption == m.encryption
    assert len(loaded.entries) == 1
    assert loaded.entries[0].path == "a/b.txt"
    assert loaded.entries[0].result_checksum == "dead"
    assert loaded.suspect is False


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
    assert loaded.suspect is True


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
    assert loaded.suspect is True
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


def test_build_disc_state_applies_deleted(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Paths listed in a manifest's deleted list must be absent from the
        resulting disc state.
    """
    m0 = _make_manifest(0, entries=[_make_entry("remove_me.txt", "x")])
    m1 = _make_manifest(1, deleted=["remove_me.txt"])
    state = build_disc_state([m0, m1])
    assert "remove_me.txt" not in state


def test_build_disc_state_skips_suspect(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        A manifest with suspect=True must be skipped; no exception raised;
        a WARNING must be logged.
    """
    m0 = _make_manifest(0, entries=[_make_entry("a.txt", "good")])
    m1 = _make_manifest(1, entries=[_make_entry("a.txt", "bad")])
    m1.suspect = True

    with caplog.at_level(logging.WARNING):
        state = build_disc_state([m0, m1])

    assert state["a.txt"] == "good"
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
