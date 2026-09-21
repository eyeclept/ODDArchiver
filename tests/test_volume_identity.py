"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for volume identity. VolumeMarker write/read round-trip against
    ISOBackend, absent-marker None, malformed marker None, path whitelist,
    and backend-failure re-raise.
"""
# Imports
import hashlib
import logging
from pathlib import Path

import pytest

from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import validate_disc_read_path
from oddarchiver.volume import (
    VolumeMarker,
    read_volume_marker,
    write_volume_marker,
)

# Globals
SMALL_DISC = 10 * 2**20  # 10 MiB. Keeps ISO builds fast in tests.
_HELLO_BLOB = hashlib.sha256(b"0:hello.txt").hexdigest()
_MARKER = VolumeMarker(
    archive_id="archive-test-id",
    volume_index=2,
    first_session=42,
    created_at="2026-09-16T00:00:00Z",
    previous_volume_label="ARCHIVE-VOL1",
)


# Functions


def _make_staging(tmp_path: Path, with_marker: bool = False) -> Path:
    """Build a minimal staging directory, optionally with volume.json."""
    staging = tmp_path / "staging"
    blob_dir = staging / "session_000" / "full"
    blob_dir.mkdir(parents=True)
    (blob_dir / _HELLO_BLOB).write_bytes(b"hello world")
    if with_marker:
        write_volume_marker(staging, _MARKER)
    return staging


def test_validate_disc_read_path_allows_volume_json():
    """
    Input:  None
    Output: None
    Details:
        Root-level volume.json must be a legal disc-read path so
        ISOBackend.read_paths() will accept it.
    """
    assert validate_disc_read_path("volume.json") == "volume.json"


def test_validate_disc_read_path_still_rejects_other_root_files():
    """
    Input:  None
    Output: None
    Details:
        Whitelisting volume.json must not open the rest of the disc root.
    """
    with pytest.raises(ValueError, match="Refusing"):
        validate_disc_read_path("not-volume.json")


def test_write_then_read_round_trips_via_iso_backend(tmp_path):
    """
    Input:  tmp_path - pytest temp directory
    Output: None
    Details:
        write_volume_marker() at staging root, burn via ISOBackend.init(),
        then read_volume_marker() must return an equal VolumeMarker.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path, with_marker=True)
    backend.init(staging, label="TEST")
    loaded = read_volume_marker(backend)
    assert loaded == _MARKER


def test_read_returns_none_when_volume_json_absent(tmp_path):
    """
    Input:  tmp_path - pytest temp directory
    Output: None
    Details:
        An ISO that never received volume.json must read back as None,
        not raise and not invent a stub VolumeMarker.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path, with_marker=False)
    backend.init(staging, label="TEST")
    assert read_volume_marker(backend) is None


def test_read_returns_none_when_volume_json_is_malformed(tmp_path, caplog):
    """
    Input:  tmp_path - pytest temp directory
            caplog   - pytest log capture
    Output: None
    Details:
        A present but undecodable volume.json is treated as absent.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path, with_marker=False)
    (staging / "volume.json").write_bytes(b"not-json{")
    backend.init(staging, label="TEST")
    with caplog.at_level(logging.WARNING):
        assert read_volume_marker(backend) is None
    assert any("unreadable volume.json" in r.message for r in caplog.records)


def test_read_reraises_backend_failure(caplog):
    """
    Input:  caplog - pytest log capture
    Output: None
    Details:
        A genuine backend/transport failure is logged and re-raised.
        None is reserved for "no marker on this disc."
    """

    class _FailingBackend:
        def read_paths(self, paths):
            raise RuntimeError("drive failed")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="drive failed"):
            read_volume_marker(_FailingBackend())
    assert any("Failed to read volume.json" in r.message for r in caplog.records)
