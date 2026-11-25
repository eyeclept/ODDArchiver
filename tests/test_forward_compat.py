"""
Tests that local-state loaders ignore unknown keys from a newer build.
"""
# Imports
import json
import logging
from pathlib import Path

from oddarchiver.catalog import _parse_memory_record
from oddarchiver.ledger import ledger_path, load_ledger
from oddarchiver.restore_state import _parse_state
from oddarchiver.volume import _record, parse_volume_marker_bytes

# Globals
logger = logging.getLogger(__name__)
_log = logger

# Keys no build defines. session_ranges was the first real case, but the
# fill build defines it, so it cannot stand in for a future key here.
_EXTRA = {"future_ranges": [[0, 1]], "future_flag": True}


# Functions


def _with_extra(record: dict) -> dict:
    """Input: a JSON object. Output: a copy with unknown keys."""
    merged = dict(record)
    merged.update(_EXTRA)
    return merged


def test_catalog_memory_ignores_unknown_keys(tmp_path):
    """
    Input:  A disc record with and without unknown keys
    Output: The same CatalogDisc objects
    """
    disc = {
        "volume_index": 0,
        "label": "DISC",
        "first_session": 0,
        "last_session": 1,
        "encrypted": False,
    }
    base = {
        "discs": {"0": disc},
        "highest_volume_index": 0,
        "encryption_mode": "none",
    }
    extra = {
        "discs": {"0": _with_extra(disc)},
        "highest_volume_index": 0,
        "encryption_mode": "none",
        **_EXTRA,
    }
    path = tmp_path / "archive-id.json"
    expected = _parse_memory_record(base, path)
    assert _parse_memory_record(extra, path) == expected


def test_restore_state_ignores_unknown_keys():
    """
    Input:  Restore-state JSON with and without unknown keys
    Output: The same RestoreState
    """
    volume = {
        "volume_index": 1,
        "first_session": 0,
        "last_session": 2,
        "label": "VOL",
        "previous_volume_label": "",
    }
    base = {
        "archive_id": "arc",
        "dest": "/tmp/out",
        "session": 2,
        "applied_volumes": [1],
        "volumes": [volume],
        "files": {},
        "deleted": {},
        "read_discs": [1],
        "missing_discs": [],
    }
    extra = _with_extra(base)
    extra["volumes"] = [_with_extra(volume)]
    assert _parse_state(extra) == _parse_state(base)


def test_volume_marker_ignores_unknown_keys():
    """
    Input:  volume.json with and without unknown keys
    Output: The same VolumeMarker
    """
    base = {
        "archive_id": "arc",
        "volume_index": 0,
        "first_session": 0,
        "created_at": "now",
        "previous_volume_label": "",
        "format": 2,
    }
    expected = parse_volume_marker_bytes(json.dumps(base).encode("utf-8"))
    extra = parse_volume_marker_bytes(json.dumps(_with_extra(base)).encode("utf-8"))
    assert extra == expected
    assert extra is not None


def test_volume_record_ignores_unknown_keys():
    """
    Input:  A baseline disc record with and without unknown keys
    Output: The same VolumeRecord
    """
    base = {
        "volume_index": 0,
        "label": "DISC",
        "first_session": 0,
        "last_session": 1,
        "encrypted": False,
    }
    assert _record(_with_extra(base)) == _record(base)


def test_ledger_ignores_unknown_keys():
    """
    Input:  Ledger files with and without unknown keys
    Output: The same Ledger
    """
    base = {
        "confirmed_sessions": 3,
        "last_verified": "2026-01-01T00:00:00+00:00",
        "label": "TEST",
    }
    path = ledger_path("TEST", "TEST")
    path.write_text(json.dumps(base), encoding="utf-8")
    expected = load_ledger("TEST", "TEST")
    path.write_text(json.dumps(_with_extra(base)), encoding="utf-8")
    assert load_ledger("TEST", "TEST") == expected
    assert expected is not None

