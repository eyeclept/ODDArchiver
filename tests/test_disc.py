"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for Task 2: disc.py — ISOBackend, DiscInfo, double-burn guard,
    prefill, parse_disc_size, and _parse_mediainfo.
"""
# Imports
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from oddarchiver.disc import (
    DiscInfo,
    ISOBackend,
    _parse_mediainfo,
    parse_disc_size,
)

# Globals
SMALL_DISC = 10 * 2**20  # 10 MiB — keeps ISO builds fast in tests


# Functions


def _make_staging(tmp_path: Path, session_name: str = "session_000") -> Path:
    """Build a minimal staging directory with one file."""
    staging = tmp_path / "staging"
    session_dir = staging / session_name
    session_dir.mkdir(parents=True)
    (session_dir / "hello.txt").write_bytes(b"hello world")
    return staging


def test_init_creates_iso(tmp_path):
    """
    Input:  tmp_path — pytest temp directory
    Output: None
    Details:
        ISOBackend.init() must produce an ISO file on disk.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path)
    backend.init(staging, label="TEST")
    assert iso.exists()
    assert iso.stat().st_size > 0


def test_init_iso_is_valid_udf(tmp_path):
    """
    Input:  tmp_path — pytest temp directory
    Output: None
    Details:
        isoinfo -d -i must exit 0 on the ISO produced by ISOBackend.init().
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    backend.init(_make_staging(tmp_path), label="TEST")
    result = subprocess.run(
        ["isoinfo", "-d", "-i", str(iso)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_append_increments_session_count(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        ISOBackend.append() must increase session_count by 1.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging0 = _make_staging(tmp_path, "session_000")
    backend.init(staging0, label="TEST")
    count_before = backend.mediainfo().session_count

    staging1 = tmp_path / "staging2"
    (staging1 / "session_001").mkdir(parents=True)
    (staging1 / "session_001" / "b.txt").write_bytes(b"second session")
    backend.append(staging1, label="TEST", expected_session_count=count_before)

    assert backend.mediainfo().session_count == count_before + 1


def test_mediainfo_remaining_bytes(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        remaining_bytes + used_bytes must equal disc_size after init.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    backend.init(_make_staging(tmp_path), label="TEST")
    info = backend.mediainfo()
    assert info.remaining_bytes + info.used_bytes == SMALL_DISC


def test_prefill_increases_used_bytes(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        prefill(N) must cause used_bytes to increase by N in mediainfo().
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    backend.init(_make_staging(tmp_path), label="TEST")
    used_before = backend.mediainfo().used_bytes

    backend.prefill(1 * 2**20)  # 1 MiB
    used_after = backend.mediainfo().used_bytes
    assert used_after == used_before + 1 * 2**20


def test_double_burn_guard_raises(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        init() must raise RuntimeError if the session count at call time
        differs from expected_session_count (simulated via patching mediainfo).
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)

    stale_info = DiscInfo(session_count=1, remaining_bytes=SMALL_DISC, used_bytes=0, label="")
    with patch.object(backend, "mediainfo", return_value=stale_info):
        with pytest.raises(RuntimeError, match="Double-burn guard"):
            backend.init(_make_staging(tmp_path), label="TEST", expected_session_count=0)


def test_read_path_returns_file_bytes(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        read_path() must return the exact bytes written into the session tree.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path)
    backend.init(staging, label="TEST")
    data = backend.read_path("session_000/hello.txt")
    assert data == b"hello world"


def test_read_path_missing_raises(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        read_path() must raise FileNotFoundError for a path not in the ISO.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    backend.init(_make_staging(tmp_path), label="TEST")
    with pytest.raises(FileNotFoundError):
        backend.read_path("session_000/does_not_exist.bin")


@pytest.mark.parametrize("size_str,expected", [
    ("25gb", 25 * 10**9),
    ("25GB", 25 * 10**9),
    ("23gib", 23 * 2**30),
    ("23GiB", 23 * 2**30),
    ("512mb", 512 * 10**6),
    ("512mib", 512 * 2**20),
    ("1024", 1024),
])
def test_parse_disc_size(size_str, expected):
    """
    Input:  size_str, expected
    Output: None
    Details:
        parse_disc_size must convert human-readable strings to exact byte counts.
    """
    assert parse_disc_size(size_str) == expected


def test_parse_mediainfo_parses_fields():
    """
    Input:  None
    Output: None
    Details:
        _parse_mediainfo must extract session count, remaining, used, and label
        from dvd+rw-mediainfo stdout fixture text.
    """
    fixture = (
        "Sessions: 2\n"
        "Remaining: 10000*2KB\n"
        "READ CAPACITY: 12000*2KB\n"
        "Volume id: MYDISC\n"
    )
    info = _parse_mediainfo(fixture)
    assert info.session_count == 2
    assert info.remaining_bytes == 10000 * 2048
    assert info.used_bytes == 2000 * 2048
    assert info.label == "MYDISC"
