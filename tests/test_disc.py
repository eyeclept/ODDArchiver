"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for disc.py. ISOBackend, DiscInfo, double-burn guard,
    prefill, parse_disc_size, dvd+rw-mediainfo parsing, CD refusal,
    disc-full errors, and wrong-disc volume identity.
"""
# Imports
import argparse
import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from oddarchiver.cli import dispatch

from oddarchiver.disc import (
    BDXL_DISC_BYTES,
    BD_DL_DISC_BYTES,
    CD_DISC_BYTES,
    DVD_DL_DISC_BYTES,
    DVD_SL_DISC_BYTES,
    DiscBackend,
    DiscInfo,
    ISOBackend,
    _parse_mediainfo,
    _is_disconnect,
    parse_disc_size,
)
from oddarchiver.volume import VolumeMarker, write_volume_marker

# Globals
SMALL_DISC = 10 * 2**20  # 10 MiB. Keeps ISO builds fast in tests.

# Blob name for the test file in session_000 (sha256("0:hello.txt"))
_HELLO_BLOB = hashlib.sha256(b"0:hello.txt").hexdigest()

# Blank BD-R as reported by dvd+rw-mediainfo: one implicit empty session.
_MEDIAINFO_BLANK_BD = (
    " Mounted Media:         41h, BD-R SRM+POW\n"
    "READ DISC INFORMATION:\n"
    " Disc status:           blank\n"
    " Number of Sessions:    1\n"
    " State of Last Session: empty\n"
    "READ TRACK INFORMATION[#1]:\n"
    " Free Blocks:           12088320*2KB\n"
    "READ CAPACITY:          12088320*2048=24756879360\n"
)

_MEDIAINFO_CD_REFUSAL_STDERR = ":-( non-DVD media mounted, exiting..."

_FAKE_SR = "/dev/oddarchiver-test-no-drive"



# Functions


def _make_staging(tmp_path: Path, session_name: str = "session_000") -> Path:
    """Build a minimal staging directory with one blob-named file."""
    staging = tmp_path / "staging"
    session_dir = staging / session_name
    blob_dir = session_dir / "full"
    blob_dir.mkdir(parents=True)
    (blob_dir / _HELLO_BLOB).write_bytes(b"hello world")
    return staging


def test_init_creates_iso(tmp_path):
    """
    Input:  tmp_path - pytest temp directory
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
    Input:  tmp_path - pytest temp directory
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


def test_remaining_bytes_decreases_and_clamps_at_zero(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        remaining_bytes falls after init and again after append. When
        used_bytes exceeds disc_size, remaining_bytes is 0, not negative.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    empty = backend.mediainfo()
    assert empty.remaining_bytes == SMALL_DISC
    assert empty.used_bytes == 0

    backend.init(_make_staging(tmp_path), label="TEST", expected_session_count=0)
    after_init = backend.mediainfo()
    assert after_init.used_bytes > empty.used_bytes
    assert after_init.remaining_bytes == SMALL_DISC - after_init.used_bytes
    assert after_init.remaining_bytes < empty.remaining_bytes
    assert after_init.remaining_bytes >= 0

    staging1 = tmp_path / "staging2"
    (staging1 / "session_001").mkdir(parents=True)
    (staging1 / "session_001" / "b.txt").write_bytes(b"second session")
    backend.append(staging1, label="TEST", expected_session_count=1)
    after_append = backend.mediainfo()
    assert after_append.used_bytes > after_init.used_bytes
    assert after_append.remaining_bytes < after_init.remaining_bytes
    assert after_append.remaining_bytes == max(0, SMALL_DISC - after_append.used_bytes)
    assert after_append.remaining_bytes >= 0

    backend.prefill(SMALL_DISC)
    clamped = backend.mediainfo()
    assert clamped.used_bytes > SMALL_DISC
    assert clamped.remaining_bytes == 0


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
        Blank-disc init uses mediainfo().session_count, which is 0 on
        empty media and nonzero after the first session. init() must
        raise RuntimeError if that count is not 0.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)

    stale_info = DiscInfo(session_count=1, remaining_bytes=SMALL_DISC, used_bytes=0, label="")
    with patch.object(backend, "mediainfo", return_value=stale_info):
        with pytest.raises(RuntimeError, match="Double-burn guard"):
            backend.init(_make_staging(tmp_path), label="TEST", expected_session_count=0)


class _PowLyingISO(ISOBackend):
    """ISOBackend whose mediainfo().session_count stays 1, like BD-R SRM+POW."""

    def mediainfo(self) -> DiscInfo:
        """
        Input:  None
        Output: DiscInfo with session_count pinned at 1
        Details:
            Copies capacity fields from ISOBackend.mediainfo and
            overwrites session_count. BD-R SRM+POW reports 1 after
            every append.
        """
        info = super().mediainfo()
        return DiscInfo(
            session_count=1,
            remaining_bytes=info.remaining_bytes,
            used_bytes=info.used_bytes,
            label=info.label,
        )


def test_double_burn_guard_detects_concurrent_append_when_mmc_stays_one(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        BD-R SRM+POW keeps mediainfo().session_count at 1 after every
        append. A concurrent second archive session must still trip
        the guard, and session_002 must not be written.
    """
    iso = tmp_path / "test.iso"
    real = ISOBackend(iso, disc_size=SMALL_DISC)
    real.init(_make_staging(tmp_path), label="TEST")
    (real._sessions_root / "session_001").mkdir()
    (real._sessions_root / "session_001" / "racer.txt").write_bytes(b"racer")

    lying = _PowLyingISO(iso, disc_size=SMALL_DISC)
    assert lying.mediainfo().session_count == 1
    staging2 = tmp_path / "staging_ours"
    (staging2 / "session_002").mkdir(parents=True)
    (staging2 / "session_002" / "ours.txt").write_bytes(b"ours")
    with pytest.raises(RuntimeError, match="Double-burn guard"):
        lying.append(staging2, label="TEST", expected_session_count=1)
    assert not (real._sessions_root / "session_002").exists()


def test_double_burn_guard_allows_append_when_mmc_stays_one(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        After two archive sessions, a BD-R-like backend still reports
        session_count 1. append with expected archive count 2 must
        proceed, because the archive count matches.
    """
    iso = tmp_path / "test.iso"
    real = ISOBackend(iso, disc_size=SMALL_DISC)
    real.init(_make_staging(tmp_path), label="TEST")
    real.append(_append_staging(tmp_path), label="TEST", expected_session_count=1)
    assert real.mediainfo().session_count == 2

    lying = _PowLyingISO(iso, disc_size=SMALL_DISC)
    assert lying.mediainfo().session_count == 1
    staging3 = tmp_path / "staging3"
    (staging3 / "session_002").mkdir(parents=True)
    (staging3 / "session_002" / "c.txt").write_bytes(b"third")
    lying.append(staging3, label="TEST", expected_session_count=2)
    assert (lying._sessions_root / "session_002").is_dir()


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
    data = backend.read_path(f"session_000/full/{_HELLO_BLOB}")
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
    missing_blob = "a" * 64  # valid hex length but never written
    with pytest.raises(FileNotFoundError):
        backend.read_path(f"session_000/full/{missing_blob}")


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
        parse_disc_size must convert human-readable strings to exact byte
        counts. These are pre-existing raw-byte and unit-suffix inputs --
        regression coverage that the new named presets below leave them
        completely unaffected.
    """
    assert parse_disc_size(size_str) == expected


@pytest.mark.parametrize("preset,expected", [
    ("cd", CD_DISC_BYTES),
    ("dvd-sl", DVD_SL_DISC_BYTES),
    ("dvd-dl", DVD_DL_DISC_BYTES),
    ("bd-r", ISOBackend.DEFAULT_DISC_BYTES),
    ("bd-dl", BD_DL_DISC_BYTES),
    ("bdxl", BDXL_DISC_BYTES),
])
def test_parse_disc_size_presets(preset, expected):
    """
    Input:  preset, expected
    Output: None
    Details:
        Each named media preset must parse to its exact documented byte
        count.
    """
    assert parse_disc_size(preset) == expected


@pytest.mark.parametrize("preset,expected", [
    ("CD", CD_DISC_BYTES),
    ("Dvd-Sl", DVD_SL_DISC_BYTES),
    ("DVD-DL", DVD_DL_DISC_BYTES),
    ("BD-R", ISOBackend.DEFAULT_DISC_BYTES),
    ("Bd-Dl", BD_DL_DISC_BYTES),
    ("BdXl", BDXL_DISC_BYTES),
])
def test_parse_disc_size_presets_case_insensitive(preset, expected):
    """
    Input:  preset, expected
    Output: None
    Details:
        Preset name matching must be case-insensitive.
    """
    assert parse_disc_size(preset) == expected


@pytest.mark.parametrize("preset,expected", [
    ("cd", CD_DISC_BYTES),
    ("dvd-sl", DVD_SL_DISC_BYTES),
    ("dvd-dl", DVD_DL_DISC_BYTES),
    ("bd-r", ISOBackend.DEFAULT_DISC_BYTES),
    ("bd-dl", BD_DL_DISC_BYTES),
    ("bdxl", BDXL_DISC_BYTES),
])
def test_media_preset_empty_isobackend_remaining_bytes(preset, expected, tmp_path):
    """
    Input:  preset, expected, tmp_path
    Output: None
    Details:
        Each of the six media presets parses to its documented byte count
        case-insensitively. An empty ISOBackend built with that size
        reports remaining_bytes equal to the same count. After init of a
        tiny staging tree, session_count is 1 and remaining_bytes drops.
    """
    assert parse_disc_size(preset) == expected
    assert parse_disc_size(preset.upper()) == expected
    backend = ISOBackend(tmp_path / f"{preset}.iso", disc_size=parse_disc_size(preset))
    info = backend.mediainfo()
    assert info.remaining_bytes == expected
    assert info.used_bytes == 0
    assert info.session_count == 0
    backend.init(
        _make_staging(tmp_path), label="TEST", expected_session_count=0,
    )
    after = backend.mediainfo()
    assert after.session_count == 1
    assert after.used_bytes > 0
    assert after.remaining_bytes == expected - after.used_bytes
    assert after.remaining_bytes < expected


def test_parse_disc_size_unrecognized_preset_like_string_raises():
    """
    Input:  None
    Output: None
    Details:
        A string that is not a raw int, a unit-suffixed size, or a known
        preset name must still raise the same error parse_disc_size
        already gives today for an unparseable value.
    """
    with pytest.raises(ValueError):
        parse_disc_size("dvd-xl")


def test_parse_mediainfo_parses_fields():
    """
    Input:  None
    Output: None
    Details:
        _parse_mediainfo must extract session count, remaining bytes,
        used bytes, and the volume label from dvd+rw-mediainfo text.
        Remaining: wins over Free Blocks when both are present.
    """
    fixture = (
        " Mounted Media:         41h, BD-R SRM+POW\n"
        "READ DISC INFORMATION:\n"
        " Disc status:           appendable\n"
        " Number of Sessions:    2\n"
        " State of Last Session: incomplete\n"
        " Remaining:             8000*2KB\n"
        "READ TRACK INFORMATION[#1]:\n"
        " Free Blocks:           0*2KB\n"
        "READ TRACK INFORMATION[#2]:\n"
        " Free Blocks:           10000*2KB\n"
        "READ CAPACITY:          12000*2048=24576000\n"
        " Volume id:             MYDISC\n"
    )
    info = _parse_mediainfo(fixture)
    assert info.session_count == 2
    assert info.remaining_bytes == 8000 * 2048
    assert info.used_bytes == (12000 - 8000) * 2048
    assert info.label == "MYDISC"


def test_parse_mediainfo_blank_disc_returns_zero_sessions():
    """
    Input:  None
    Output: None
    Details:
        Blank BD-R discs report "Number of Sessions: 1" with
        "Disc status: blank". _parse_mediainfo must return
        session_count=0 so init() proceeds instead of reporting
        the disc already initialized.
    """
    info = _parse_mediainfo(_MEDIAINFO_BLANK_BD)
    assert info.session_count == 0
    assert info.remaining_bytes == 12088320 * 2048
    assert info.label == ""


def _xorriso_result(returncode, stderr=""):
    """Build a subprocess.CompletedProcess like `xorriso -extract` would return."""
    return subprocess.CompletedProcess(
        args=["xorriso"], returncode=returncode, stdout=b"", stderr=stderr.encode(),
    )


def test_read_path_extracts_via_xorriso(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        read_path() must invoke `xorriso -indev DEVICE -osirrox on -extract`
        and return the extracted bytes on the first attempt when the drive
        is ready.
    """
    backend = DiscBackend("/dev/sr0")
    blob = b"real disc blob content"

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(blob)
        return _xorriso_result(0)

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)

    result = backend.read_path(f"session_000/full/{'a' * 64}")
    assert result == blob


def test_read_path_retries_on_drive_not_ready(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        A real several-minute burn was observed to leave the kernel's own
        block device layer stuck reporting stale, tiny capacity for well
        past the old mount-retry window (5+ minutes), even though the
        disc's actual content was intact and immediately readable via
        xorriso's direct SG_IO access the whole time. read_path() must
        retry a transient "no media" failure (mirroring mediainfo()'s
        retry) instead of raising immediately.
        Regression for bug: post-burn verify crashed on a real, multi-GB
        burn with "Device ... not a mountable filesystem" that never
        settled, even though the burned data was completely intact.
    """
    backend = DiscBackend("/dev/sr0")
    blob = b"settled after retries"
    attempts = []

    def fake_run(cmd, **kwargs):
        attempts.append(cmd)
        if len(attempts) < 3:
            return _xorriso_result(1, stderr="xorriso : FAILURE : Cannot inquire ATIP: no media")
        Path(cmd[-1]).write_bytes(blob)
        return _xorriso_result(0)

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    monkeypatch.setattr("oddarchiver.disc.time.sleep", lambda seconds: None)

    result = backend.read_path(f"session_000/full/{'a' * 64}", retries=6, retry_delay=5.0)

    assert result == blob
    assert len(attempts) == 3


def test_read_path_unmounts_and_retries_on_busy_device(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        Real-hardware regression: a physical eject/reinsert -- the
        documented remedy for append()'s stale-block-layer error --
        was observed to make the desktop's automount monitor grab the
        device via a real kernel mount immediately afterward, which
        blocks xorriso's SG_IO access with libburn's
        "Cannot open busy device ... Device or resource busy" until the
        mount is released. read_path() must recognize this signature,
        best-effort unmount the device, and retry -- not just wait, since
        an existing mount does not clear on its own the way a genuinely
        still-settling drive does.
    """
    backend = DiscBackend("/dev/sr0")
    blob = b"read after unmount"
    run_calls = []

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        if cmd[0] == "udisksctl":
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")
        if sum(1 for c in run_calls if c[0] == "xorriso") < 2:
            return _xorriso_result(
                1,
                stderr="libburn : SORRY : Cannot open busy device '/dev/sr0' : "
                       "Device or resource busy\n"
                       "libburn : FAILURE : Cannot access '/dev/sr0' as SG_IO CDROM drive",
            )
        Path(cmd[-1]).write_bytes(blob)
        return _xorriso_result(0)

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    monkeypatch.setattr("oddarchiver.disc.time.sleep", lambda seconds: None)

    result = backend.read_path(f"session_000/full/{'a' * 64}", retries=6, retry_delay=5.0)

    assert result == blob
    assert any(c[0] == "udisksctl" and c[1] == "unmount" for c in run_calls)


def test_read_path_raises_after_exhausting_retries(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        If the drive never becomes readable within the retry budget,
        read_path() must raise RuntimeError naming the device, not hang
        forever or silently return nothing.
    """
    backend = DiscBackend("/dev/sr0")
    attempts = []

    def fake_run(cmd, **kwargs):
        attempts.append(cmd)
        return _xorriso_result(1, stderr="xorriso : FAILURE : Cannot inquire ATIP: no media")

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    monkeypatch.setattr("oddarchiver.disc.time.sleep", lambda seconds: None)

    with pytest.raises(RuntimeError, match="/dev/sr0"):
        backend.read_path(f"session_000/full/{'a' * 64}", retries=2, retry_delay=5.0)

    assert len(attempts) == 3  # initial attempt + 2 retries


def test_read_path_raises_file_not_found_for_missing_path(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        When xorriso loads the ISO tree fine but the specific path is not
        present (e.g. probing for a session that does not exist yet),
        read_path() must raise FileNotFoundError, not RuntimeError, so
        callers that treat a missing session as "no more sessions" via
        `except (OSError, ValueError)` (verify.py, restore.py) keep working.
    """
    backend = DiscBackend("/dev/sr0")

    def fake_run(cmd, **kwargs):
        return _xorriso_result(
            5,
            stderr="xorriso : FAILURE : Cannot determine attributes of (ISO) "
                   "source file '/session_999/manifest.enc' : Invalid argument",
        )

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)

    with pytest.raises(FileNotFoundError):
        backend.read_path("session_999/manifest.enc")


class _FakePopen:
    """Fake subprocess.Popen: yields canned stdout lines, returns a fixed exit code."""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def test_run_growisofs_collapses_progress_lines_into_a_bar(monkeypatch, capsys):
    """
    Input:  monkeypatch, capsys
    Output: None
    Details:
        growisofs repeats a "<written>/<total> (<pct>%) @<speed>x, remaining
        M:SS ..." line very frequently during a real, multi-minute burn --
        one new terminal line per update. _run_growisofs() must collapse
        these into a single in-place updating bar instead of letting every
        update print as its own line, while still printing genuine
        one-off status lines (like "Burn complete.") through unchanged.
        Regression for the "wall of progress lines" complaint on a real,
        several-minute, multi-GB burn.
    """
    from oddarchiver.disc import _run_growisofs

    lines = [
        "    65536/1337587200 ( 0.0%) @4.0x, remaining 3:45 RBU 100.0% UBU  50.0%\n",
        "   131072/1337587200 ( 0.0%) @4.0x, remaining 3:44 RBU 100.0% UBU  50.0%\n",
        "Burn complete.\n",
    ]
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(lines, returncode=0),
    )

    returncode, _ = _run_growisofs(["growisofs", "-Z", "/dev/sr0"])
    out = capsys.readouterr().out

    assert returncode == 0
    assert out.count("Burn complete.") == 1
    assert "65536/1337587200" in out
    assert "131072/1337587200" in out
    # Both progress updates redraw the same line (carriage return), never
    # two separate terminal lines.
    assert out.count("\r") >= 2


def test_run_growisofs_collapses_build_phase_file_counter(monkeypatch, capsys):
    """
    Input:  monkeypatch, capsys
    Output: None
    Details:
        The mkisofs/xorriso child process growisofs shells out to for
        building the image repeats its own "<n> files added in <n>
        seconds" line during that phase for a large source tree.
        _run_growisofs() must collapse these into a single in-place
        updating counter too, not one line per update.
    """
    from oddarchiver.disc import _run_growisofs

    lines = [
        "xorriso : UPDATE :       8 files added in 1 seconds\n",
        "xorriso : UPDATE :    1244 files added in 3 seconds\n",
    ]
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(lines, returncode=0),
    )

    returncode, _ = _run_growisofs(["growisofs", "-Z", "/dev/sr0"])
    out = capsys.readouterr().out

    assert returncode == 0
    assert "1244 files added" in out
    assert out.count("\r") >= 2


def test_run_growisofs_collapses_xorriso_percent_done_lines(monkeypatch, capsys):
    """
    Input:  monkeypatch, capsys
    Output: None
    Details:
        For a large real source tree (thousands of files, several GB),
        the mkisofs/xorriso child process reports progress as
        "<pct>% done, estimate finish <timestamp>" instead of the
        smaller-tree "<n> files added" counter, repeating once per
        ~0.15-0.2% -- one new terminal line per update on a large real
        burn, e.g. the user's real 6499-file/8.3 GB archive.
        _run_growisofs() must collapse these into a single in-place
        updating bar too, using the given percentage directly (no
        written/total byte counts are available for this format).
        Regression for the "flooded with 62.14%, 62.29%, 62.44%... one
        line each" complaint on that real archive.
    """
    from oddarchiver.disc import _run_growisofs

    lines = [
        "xorriso : UPDATE : 62.29% done, estimate finish Sun Apr 09 10:35:29 2023\n",
        "xorriso : UPDATE : 62.44% done, estimate finish Sun Apr 09 10:35:30 2023\n",
        "Burn complete.\n",
    ]
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(lines, returncode=0),
    )

    returncode, _ = _run_growisofs(["growisofs", "-Z", "/dev/sr0"])
    out = capsys.readouterr().out

    assert returncode == 0
    assert out.count("Burn complete.") == 1
    assert "62/100" in out
    assert "estimate finish Sun Apr 09 10:35:30 2023" in out
    # Both progress updates redraw the same line, never two separate
    # terminal lines, and the raw percentage text never leaks through.
    assert out.count("\r") >= 2
    assert "62.29% done" not in out
    assert "62.44% done" not in out


def test_run_growisofs_collapses_genisoimage_percent_done_lines(monkeypatch, capsys):
    """
    Input:  monkeypatch, capsys
    Output: None
    Details:
        _run_growisofs forces MKISOFS=genisoimage (disc.py) because
        genisoimage, not xorriso, handles a 4 GiB+ source file. genisoimage
        prints its build-phase percentage line bare, with no "UPDATE :" tag,
        unlike the xorriso line the sibling test above covers. Before this
        fix, that bare line matched none of the three progress regexes and
        fell through to the raw-print path, flooding the terminal with one
        line per ~0.15-0.2% on a large real archive. Regression for exactly
        that complaint, using the literal lines observed during a real
        87 GiB, ~148,000-file burn.
    """
    from oddarchiver.disc import _run_growisofs

    lines = [
        "  0.67% done, estimate finish Mon Sep 21 11:15:49 2026\n",
        "  0.83% done, estimate finish Mon Sep 21 11:05:49 2026\n",
        "  1.00% done, estimate finish Mon Sep 21 10:59:10 2026\n",
        "  1.17% done, estimate finish Mon Sep 21 10:54:25 2026\n",
        "Burn complete.\n",
    ]
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(lines, returncode=0),
    )

    returncode, _ = _run_growisofs(["growisofs", "-Z", "/dev/sr0"])
    out = capsys.readouterr().out

    assert returncode == 0
    assert out.count("Burn complete.") == 1
    assert "1/100" in out
    assert "estimate finish Mon Sep 21 10:54:25 2026" in out
    # Four progress updates redraw the same line, never four separate
    # terminal lines, and the raw percentage text never leaks through.
    assert out.count("\r") >= 4
    assert "0.67% done" not in out
    assert "1.17% done" not in out


def test_run_growisofs_returns_nonzero_on_failure(monkeypatch, capsys):
    """
    Input:  monkeypatch, capsys
    Output: None
    Details:
        A real error line must still print through unchanged, and the
        real growisofs exit code must still propagate so init()/append()
        raise on failure exactly as before this change.
    """
    from oddarchiver.disc import _run_growisofs

    lines = ["FATAL: unable to open device.\n"]
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(lines, returncode=1),
    )

    returncode, output_lines = _run_growisofs(["growisofs", "-Z", "/dev/sr0"])
    out = capsys.readouterr().out

    assert returncode == 1
    assert "FATAL: unable to open device." in out
    assert output_lines == ["FATAL: unable to open device."]


def test_append_raises_clear_error_for_stale_block_layer(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        Real-hardware regression: on a real BD-R drive, appending a session
        with growisofs -M requires the kernel block-device layer to
        pread64() the previous session's primary volume descriptor
        (confirmed from dvd+rw-tools' own growisofs.c: this pread64 call
        goes through a plain open()/pread64() on the raw device node, not
        the SG_IO path DiscBackend's own reads/mediainfo use). After an
        in-place burn, nothing signals a kernel media-change event, so
        this pread64 fails with growisofs's well-known
        ":-( unable to pread64(2) primary volume descriptor: Input/output
        error / you most likely want to use -Z option." even though the
        drive, disc, and prior session are all completely intact.
        growisofs's own "-Z" suggestion is actively dangerous here, since
        -Z re-initializes the disc and would overwrite the existing
        archive. When automatic USB-reauth recovery is unavailable (e.g.
        the sudoers entry is not configured), append() must raise a clear
        RuntimeError naming the real cause (stale kernel cache, not
        corruption) and the real remedy, instead of letting the
        misleading advice propagate or a bare exit-code message through.
    """
    monkeypatch.setattr(
        DiscBackend, "mediainfo",
        lambda self, retries=6, retry_delay=5.0: DiscInfo(
            session_count=1, remaining_bytes=10**9, used_bytes=0, label="TEST",
        ),
    )
    monkeypatch.setattr(
        "oddarchiver.disc.archive_session_count", lambda backend: 1,
    )
    monkeypatch.setattr(DiscBackend, "_attempt_usb_reauth", lambda self: False)
    lines = [
        ":-( unable to pread64(2) primary volume descriptor: Input/output error\n",
        "    you most likely want to use -Z option.\n",
    ]
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(lines, returncode=1),
    )

    backend = DiscBackend("/dev/sr0")
    with pytest.raises(RuntimeError) as exc_info:
        backend.append(tmp_path, "TEST", 1)

    message = str(exc_info.value)
    assert "stale kernel" in message.lower()
    assert "eject" in message.lower()
    assert "Do NOT follow" in message


def test_append_recovers_via_usb_reauth_and_succeeds(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        Live-confirmed fix (see STATE_OF_THE_PROGRAM.md,
        "Environment-specific hardware limitation"):
        deauthorizing then reauthorizing the drive's USB device node
        forces a full re-enumeration that clears the stale kernel
        capacity cache, with no tray action. When append() hits the
        pread64 signature and _attempt_usb_reauth() reports success, it
        must retry growisofs -M once and succeed silently -- not raise --
        matching the real, live-tested end-to-end behavior.
    """
    monkeypatch.setattr(
        DiscBackend, "mediainfo",
        lambda self, retries=6, retry_delay=5.0: DiscInfo(
            session_count=1, remaining_bytes=10**9, used_bytes=0, label="TEST",
        ),
    )
    monkeypatch.setattr(
        "oddarchiver.disc.archive_session_count", lambda backend: 1,
    )
    monkeypatch.setattr(DiscBackend, "_attempt_usb_reauth", lambda self: True)
    monkeypatch.setattr(DiscBackend, "_wait_for_device_node", lambda self: True)

    attempts = []

    def fake_popen(cmd, **kwargs):
        attempts.append(cmd)
        if len(attempts) == 1:
            return _FakePopen(
                [":-( unable to pread64(2) primary volume descriptor: "
                 "Input/output error\n"],
                returncode=1,
            )
        return _FakePopen(["Burn complete.\n"], returncode=0)

    monkeypatch.setattr("oddarchiver.disc.subprocess.Popen", fake_popen)

    backend = DiscBackend("/dev/sr0")
    backend.append(tmp_path, "TEST", 1)  # must not raise

    assert len(attempts) == 2  # original attempt + one retry after reauth


def test_append_raises_clear_error_when_usb_reauth_also_fails(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        If automatic USB-reauth recovery runs but does not clear the
        failure (e.g. it fixed the kernel cache but growisofs -M fails
        again for some other reason), append() must still raise the
        clear error rather than silently giving up -- and the message
        must say recovery was attempted, not just recommend a physical
        eject as if nothing had been tried.
    """
    monkeypatch.setattr(
        DiscBackend, "mediainfo",
        lambda self, retries=6, retry_delay=5.0: DiscInfo(
            session_count=1, remaining_bytes=10**9, used_bytes=0, label="TEST",
        ),
    )
    monkeypatch.setattr(
        "oddarchiver.disc.archive_session_count", lambda backend: 1,
    )
    monkeypatch.setattr(DiscBackend, "_attempt_usb_reauth", lambda self: True)
    monkeypatch.setattr(DiscBackend, "_wait_for_device_node", lambda self: True)
    lines = [":-( unable to pread64(2) primary volume descriptor: Input/output error\n"]
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(lines, returncode=1),
    )

    backend = DiscBackend("/dev/sr0")
    with pytest.raises(RuntimeError) as exc_info:
        backend.append(tmp_path, "TEST", 1)

    message = str(exc_info.value)
    assert "automatic" in message.lower()
    assert "did not clear it" in message.lower()


def test_wait_for_device_node_polls_until_it_reappears(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        Real-hardware regression: an immediate growisofs retry right
        after _attempt_usb_reauth() reported success hit "No such file
        or directory" because the device node had not yet been
        recreated by the kernel's asynchronous USB re-enumeration.
        _wait_for_device_node() must poll rather than check once, and
        return True as soon as the node exists.
    """
    backend = DiscBackend("/dev/sr0")
    checks = []

    def fake_exists(self):
        checks.append(str(self))
        return len(checks) >= 3

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr("oddarchiver.disc.time.sleep", lambda seconds: None)

    assert backend._wait_for_device_node(retries=10, retry_delay=1.0) is True
    assert len(checks) == 3


def test_wait_for_device_node_gives_up_after_exhausting_retries(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        If the device node never reappears within the retry budget,
        _wait_for_device_node() must return False, not hang forever or
        raise.
    """
    backend = DiscBackend("/dev/sr0")
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr("oddarchiver.disc.time.sleep", lambda seconds: None)

    assert backend._wait_for_device_node(retries=3, retry_delay=1.0) is False


def _make_fake_usb_sysfs(tmp_path: Path) -> Path:
    """
    Input:  tmp_path
    Output: Path - fake /sys/class/block root
    Details:
        Builds a minimal fake sysfs tree mirroring this real drive's
        actual topology (confirmed live via readlink -f /sys/block/sr0):
        block/sr0/device -> .../host8/target8:0:0/8:0:0:0 (SCSI LUN),
        whose ancestor .../usb5/5-1/5-1.2 is the actual USB device (has
        idVendor + authorized), with an interface node
        5-1.2:1.0/host8 (no idVendor) in between -- the case that
        distinguishes "nearest ancestor with both files" from a naive
        "first ancestor with idVendor" (a USB interface has neither).
    """
    real = tmp_path / "sys" / "devices" / "usb5" / "5-1" / "5-1.2"
    real.mkdir(parents=True)
    (real / "idVendor").write_text("0411")
    (real / "authorized").write_text("1")
    iface = real / "5-1.2:1.0"
    host = iface / "host8"
    target = host / "target8:0:0"
    lun = target / "8:0:0:0"
    lun.mkdir(parents=True)

    block_root = tmp_path / "sys" / "class" / "block"
    sr0 = block_root / "sr0"
    sr0.mkdir(parents=True)
    (sr0 / "device").symlink_to(lun, target_is_directory=True)
    return block_root


def test_resolve_usb_device_sysfs_path_walks_to_usb_ancestor(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Given a fake sysfs tree matching this drive's real topology,
        resolve_usb_device_sysfs_path() must return the actual USB
        device directory (idVendor + authorized), not the SCSI LUN, the
        USB interface directory (which has neither file), or some other
        ancestor -- with no hardcoded bus/port assumption.
    """
    from oddarchiver.disc import resolve_usb_device_sysfs_path

    block_root = _make_fake_usb_sysfs(tmp_path)

    result = resolve_usb_device_sysfs_path("/dev/sr0", sys_class_block=block_root)

    assert result == (tmp_path / "sys" / "devices" / "usb5" / "5-1" / "5-1.2").resolve()


def test_resolve_usb_device_sysfs_path_returns_none_for_missing_device(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A block device name with no corresponding sysfs entry must
        return None, not raise.
    """
    from oddarchiver.disc import resolve_usb_device_sysfs_path

    block_root = tmp_path / "sys" / "class" / "block"
    block_root.mkdir(parents=True)

    assert resolve_usb_device_sysfs_path("/dev/sr9", sys_class_block=block_root) is None


def test_resolve_usb_device_sysfs_path_returns_none_for_non_usb_device(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A device whose ancestry never reaches a node with both idVendor
        and authorized (e.g. a SATA/NVMe drive) must return None, not
        an unrelated ancestor.
    """
    from oddarchiver.disc import resolve_usb_device_sysfs_path

    lun = tmp_path / "sys" / "devices" / "pci0000:00" / "ata1" / "target0:0:0" / "0:0:0:0"
    lun.mkdir(parents=True)
    block_root = tmp_path / "sys" / "class" / "block"
    sda = block_root / "sda"
    sda.mkdir(parents=True)
    (sda / "device").symlink_to(lun, target_is_directory=True)

    assert resolve_usb_device_sysfs_path("/dev/sda", sys_class_block=block_root) is None


_DISC_FULL_LINES = [
    ":-( /dev/sr0: 128 blocks are free, 4096 to be written!\n",
    ":-( write failed: No space left on device\n",
]


def _stub_disc_mediainfo(monkeypatch, session_count: int) -> None:
    """Point DiscBackend.mediainfo and archive_session_count at canned values."""
    monkeypatch.setattr(
        DiscBackend, "mediainfo",
        lambda self, retries=6, retry_delay=5.0: DiscInfo(
            session_count=session_count,
            remaining_bytes=10**9,
            used_bytes=0,
            label="TEST",
        ),
    )
    monkeypatch.setattr(
        "oddarchiver.disc.archive_session_count",
        lambda backend: session_count,
    )


def _stub_growisofs(monkeypatch, lines, returncode: int) -> None:
    """Point growisofs Popen at canned stdout and an exit code."""
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(lines, returncode=returncode),
    )


def test_append_disc_full_reassures_after_reverify(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        growisofs -M printing the disc-full two-line signature must raise
        a diagnosed RuntimeError, not a bare exit code. The message must
        say the append ran out of space, that prior sessions were
        re-verified, and that verify --level fast then a retry is the
        next step. _reverify_prior_sessions must actually run.
    """
    staging = tmp_path / "staging"
    (staging / "session_001").mkdir(parents=True)
    _stub_disc_mediainfo(monkeypatch, session_count=1)
    _stub_growisofs(monkeypatch, _DISC_FULL_LINES, returncode=1)
    reverify_calls = []

    def fake_reverify(self, attempted):
        reverify_calls.append(attempted)
        return True

    monkeypatch.setattr(DiscBackend, "_reverify_prior_sessions", fake_reverify)
    backend = DiscBackend("/dev/sr0")
    with pytest.raises(RuntimeError) as exc_info:
        backend.append(staging, "TEST", 1)
    message = str(exc_info.value)
    assert "out of space" in message.lower()
    assert "append" in message.lower()
    assert "re-verified readable" in message
    assert "verify --level fast" in message
    assert reverify_calls == [1]


def test_append_disc_full_reports_unreadable_prior_sessions(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        When re-verify of prior sessions fails, the disc-full append
        error must say they could not be re-verified instead of claiming
        they are safe.
    """
    staging = tmp_path / "staging"
    (staging / "session_001").mkdir(parents=True)
    _stub_disc_mediainfo(monkeypatch, session_count=1)
    _stub_growisofs(monkeypatch, _DISC_FULL_LINES, returncode=1)
    monkeypatch.setattr(
        DiscBackend, "_reverify_prior_sessions", lambda self, attempted: False,
    )
    backend = DiscBackend("/dev/sr0")
    with pytest.raises(RuntimeError) as exc_info:
        backend.append(staging, "TEST", 1)
    message = str(exc_info.value)
    assert "out of space" in message.lower()
    assert "could NOT be re-verified" in message


def test_init_disc_full_capacity_message_skips_reverify(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        growisofs -Z printing the disc-full signature must raise a
        capacity-oriented RuntimeError. _reverify_prior_sessions must not
        run: session 0 has nothing prior to check.
    """
    staging = tmp_path / "staging"
    (staging / "session_000").mkdir(parents=True)
    _stub_disc_mediainfo(monkeypatch, session_count=0)
    _stub_growisofs(monkeypatch, _DISC_FULL_LINES, returncode=1)
    reverify_calls = []
    monkeypatch.setattr(
        DiscBackend,
        "_reverify_prior_sessions",
        lambda self, attempted: reverify_calls.append(attempted) or True,
    )
    backend = DiscBackend("/dev/sr0")
    with pytest.raises(RuntimeError) as exc_info:
        backend.init(staging, "TEST", 0)
    message = str(exc_info.value)
    assert "out of space" in message.lower()
    assert "init" in message.lower()
    assert "--disc-size" in message
    assert reverify_calls == []


def test_append_enospc_without_blocks_free_stays_generic(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        A lone "No space left on device" line without the blocks-are-free
        signature must stay the generic growisofs -M exit-code error.
    """
    staging = tmp_path / "staging"
    (staging / "session_001").mkdir(parents=True)
    _stub_disc_mediainfo(monkeypatch, session_count=1)
    _stub_growisofs(
        monkeypatch,
        ["write failed: No space left on device\n"],
        returncode=1,
    )
    backend = DiscBackend("/dev/sr0")
    with pytest.raises(RuntimeError) as exc_info:
        backend.append(staging, "TEST", 1)
    message = str(exc_info.value)
    assert "exit code 1" in message
    assert "out of space during append" not in message.lower()


def _backend_with_marker(
    tmp_path: Path, archive_id: str = "id-expected", volume_index: int = 0,
) -> ISOBackend:
    """Init an ISOBackend whose volume.json carries the given identity."""
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    staging = _make_staging(tmp_path)
    write_volume_marker(
        staging,
        VolumeMarker(
            archive_id=archive_id,
            volume_index=volume_index,
            first_session=0,
            created_at="2026-09-20T00:00:00Z",
            previous_volume_label="",
        ),
    )
    backend.init(staging, label="TEST")
    return backend


def _append_staging(tmp_path: Path) -> Path:
    """Build a session_001 staging tree for an identity-guard append."""
    staging = tmp_path / "staging2"
    (staging / "session_001").mkdir(parents=True)
    (staging / "session_001" / "b.txt").write_bytes(b"second session")
    return staging


def test_append_refuses_wrong_archive_id(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        append() with an expected archive_id that does not match the
        disc's volume.json must raise a wrong-disc error naming both
        ids and must not write the new session.
    """
    backend = _backend_with_marker(tmp_path, archive_id="id-expected")
    with pytest.raises(RuntimeError) as exc_info:
        backend.append(
            _append_staging(tmp_path), "TEST", 1,
            expected_archive_id="id-other",
            expected_volume_index=0,
        )
    message = str(exc_info.value)
    assert "wrong disc" in message.lower()
    assert "id-expected" in message
    assert "id-other" in message
    assert backend.mediainfo().session_count == 1


def test_append_accepts_matching_archive_id(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        append() with the disc's own archive_id and volume_index must
        proceed and increment session count.
    """
    backend = _backend_with_marker(tmp_path, archive_id="id-expected")
    backend.append(
        _append_staging(tmp_path), "TEST", 1,
        expected_archive_id="id-expected",
        expected_volume_index=0,
    )
    assert backend.mediainfo().session_count == 2


def test_append_refuses_markerless_disc_when_identity_expected(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A disc with no volume.json, when the caller expected an
        archive_id, must raise a wrong-disc error naming the expected
        id. It must not crash and must not write the new session.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    backend.init(_make_staging(tmp_path), label="TEST")
    with pytest.raises(RuntimeError) as exc_info:
        backend.append(
            _append_staging(tmp_path), "TEST", 1,
            expected_archive_id="id-expected",
        )
    message = str(exc_info.value)
    assert "wrong disc" in message.lower()
    assert "id-expected" in message
    assert "volume marker" in message.lower()
    assert backend.mediainfo().session_count == 1


def test_append_refuses_wrong_volume_index(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Same archive_id but a different volume_index must refuse the
        burn and name both volume indexes.
    """
    backend = _backend_with_marker(
        tmp_path, archive_id="id-same", volume_index=0,
    )
    with pytest.raises(RuntimeError) as exc_info:
        backend.append(
            _append_staging(tmp_path), "TEST", 1,
            expected_archive_id="id-same",
            expected_volume_index=1,
        )
    message = str(exc_info.value)
    assert "wrong disc" in message.lower()
    assert "id-same" in message
    assert "volume_index 1" in message
    assert "volume_index 0" in message
    assert backend.mediainfo().session_count == 1


def _mediainfo_completed(text="", returncode=0, stderr=""):
    """Build a CompletedProcess as `dvd+rw-mediainfo DEVICE` would return."""
    return subprocess.CompletedProcess(
        args=["dvd+rw-mediainfo", "/dev/sr0"],
        returncode=returncode,
        stdout=text.encode(),
        stderr=stderr.encode(),
    )


# Verbatim dvd+rw-mediainfo output captured from a real M-DISC BD-R in a
# Buffalo USB drive, after ODDArchiver burned session 000 through growisofs.
# growisofs formats BD-R for Pseudo OverWrite (SRM+POW), which is what broke
# an earlier xorriso-based parser. Media ID MILLEN/MR1 is the M-DISC marker.
_REAL_POW_BDR_MEDIAINFO = """INQUIRY:                [BUFFALO ][Optical Drive   ][]
GET [CURRENT] CONFIGURATION:
 Mounted Media:         41h, BD-R SRM+POW
 Media ID:              MILLEN/MR1
 Current Write Speed:   4.0x4495=17984KB/s
 Write Speed #0:        4.0x4495=17984KB/s
 Write Speed #1:        2.0x4495=8992KB/s
 Speed Descriptor#0:    08/12088319 R@6.0x4495=26976KB/s W@4.0x4495=17984KB/s
 Speed Descriptor#1:    00/12088319 R@6.0x4495=26976KB/s W@2.0x4495=8992KB/s
BD SPARE AREA INFORMATION:
 Spare Area:            65536/65536=100.0% free
POW RESOURCES INFORMATION:
 Remaining Replacements:16843296
 Remaining Map Entries: 0
 Remaining Updates:     0
READ DISC INFORMATION:
 Disc status:           appendable
 Number of Sessions:    1
 State of Last Session: incomplete
 "Next" Track:          1
 Number of Tracks:      2
READ TRACK INFORMATION[#1]:
 Track State:           partial incremental
 Track Start Address:   0*2KB
 Free Blocks:           0*2KB
 Track Size:            154048*2KB
READ TRACK INFORMATION[#2]:
 Track State:           invisible incremental
 Track Start Address:   154048*2KB
 Next Writable Address: 154048*2KB
 Free Blocks:           11934272*2KB
 Track Size:            11934272*2KB
FABRICATED TOC:
 Track#1  :             14@0
 Track#AA :             14@12088320
 Multi-session Info:    #1@0
READ CAPACITY:          12088320*2048=24756879360
"""


def test_parse_mediainfo_bdr_srm_pow_real_output():
    """
    Input:  None
    Output: None
    Details:
        BD-R SRM+POW drives (e.g. Buffalo) use *2048 in READ CAPACITY and
        have two Free Blocks: lines. Track#1 (written, 0 free) and Track#2
        (invisible writable, full remaining space). Parser must take the last
        Free Blocks entry and parse *2048 as well as *2KB.
        Regression for bug: remaining_bytes reported as 0 on appendable BD-R.
    """
    fixture = (
        " Mounted Media:         41h, BD-R SRM+POW\n"
        "READ DISC INFORMATION:\n"
        " Disc status:           appendable\n"
        " Number of Sessions:    1\n"
        " State of Last Session: incomplete\n"
        "READ TRACK INFORMATION[#1]:\n"
        " Track State:           partial incremental\n"
        " Free Blocks:           0*2KB\n"
        " Track Size:            55872*2KB\n"
        "READ TRACK INFORMATION[#2]:\n"
        " Track State:           invisible incremental\n"
        " Free Blocks:           12032448*2KB\n"
        " Track Size:            12032448*2KB\n"
        "READ CAPACITY:          12088320*2048=24756879360\n"
    )
    info = _parse_mediainfo(fixture)
    assert info.session_count == 1
    assert info.remaining_bytes == 12032448 * 2048
    assert info.used_bytes == (12088320 - 12032448) * 2048  # ~109 MB written


def test_parse_mediainfo_empty_last_session_returns_zero_sessions():
    """
    Input:  None
    Output: None
    Details:
        Some drives omit "Disc status" but report "State of Last Session: empty"
        with a single session. _parse_mediainfo must treat this as 0 sessions.
    """
    fixture = (
        "Number of Sessions:    1\n"
        "State of Last Session: empty\n"
        "Free Blocks:           5000*2KB\n"
    )
    info = _parse_mediainfo(fixture)
    assert info.session_count == 0
    assert info.remaining_bytes == 5000 * 2048

def test_mediainfo_retries_on_no_media_then_raises(monkeypatch, capsys):
    """
    Input:  monkeypatch, capsys
    Output: None
    Details:
        A transient "no media" from dvd+rw-mediainfo must be retried,
        because the drive reloads the tray for 5 to 30 seconds after a
        burn. retries=3 means 4 attempts, then a raise.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _mediainfo_completed(returncode=1, stderr="no media mounted")

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    monkeypatch.setattr("oddarchiver.disc.time.sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="dvd\\+rw-mediainfo failed"):
        DiscBackend("/dev/sr0").mediainfo(retries=3, retry_delay=0)
    assert len(calls) == 4


def test_mediainfo_refuses_cd_with_unsupported_error(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        dvd+rw-mediainfo refuses CD with "non-DVD media mounted". That
        error is the CD detector, so it must surface as the
        unsupported-media error rather than a raw tool failure, and it
        must not be retried as a transient drive problem.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _mediainfo_completed(
            returncode=1, stderr=_MEDIAINFO_CD_REFUSAL_STDERR)

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="CD media is not supported"):
        DiscBackend("/dev/sr0").mediainfo()
    assert len(calls) == 1


def test_mediainfo_counts_sessions_on_pow_formatted_bdr():
    """
    Input:  None
    Output: None
    Details:
        Regression guard for a data-safety bug found on real hardware.
        growisofs formats BD-R for Pseudo OverWrite. A parser built on
        xorriso -toc read such a disc as "is unsuitable" with no session
        count, so a BD-R holding one session reported zero sessions.
        init() treats zero sessions as a blank disc, so it would have
        run growisofs -Z and destroyed the archive already on the medium.

        This asserts the real captured output of a written M-DISC BD-R
        parses as one session, with capacity taken from the writable
        track rather than the whole disc.
    """
    info = _parse_mediainfo(_REAL_POW_BDR_MEDIAINFO)
    assert info.session_count == 1
    assert info.remaining_bytes == 11934272 * 2048
    assert info.used_bytes == (12088320 - 11934272) * 2048


def test_mediainfo_pow_bdr_is_never_mistaken_for_blank():
    """
    Input:  None
    Output: None
    Details:
        States the safety property directly, independent of exact byte
        counts: a written disc must never parse as zero sessions,
        because that is the value init() uses to decide a medium is
        blank and safe to overwrite.
    """
    info = _parse_mediainfo(_REAL_POW_BDR_MEDIAINFO)
    assert info.session_count > 0, (
        "a written BD-R parsed as blank; init() would overwrite the archive"
    )


def test_cd_media_is_refused_on_init(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        A CD profile must refuse init with a clear error naming the
        reason, and must never invoke a burn program. ODDArchiver
        appends sessions to write-once media, and two real CD-R burns
        both closed the disc after session 000, leaving 0 writable
        blocks.
    """
    def fake_popen(cmd, **kwargs):
        raise AssertionError(f"no burn program may run for CD: {cmd}")

    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.run",
        lambda cmd, **kwargs: _mediainfo_completed(
            returncode=1, stderr=_MEDIAINFO_CD_REFUSAL_STDERR),
    )
    monkeypatch.setattr("oddarchiver.disc.subprocess.Popen", fake_popen)
    backend = DiscBackend("/dev/sr0")
    with pytest.raises(RuntimeError, match="CD media is not supported"):
        backend.init(_make_staging(tmp_path, "session_000"), "TEST", 0)


def test_cd_media_is_refused_on_append(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        Append must refuse a CD for the same reason, and must not run
        a burn program.
    """
    def fake_popen(cmd, **kwargs):
        raise AssertionError(f"no burn program may run for CD: {cmd}")

    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.run",
        lambda cmd, **kwargs: _mediainfo_completed(
            returncode=1, stderr=_MEDIAINFO_CD_REFUSAL_STDERR),
    )
    monkeypatch.setattr("oddarchiver.disc.subprocess.Popen", fake_popen)
    backend = DiscBackend("/dev/sr0")
    with pytest.raises(RuntimeError, match="CD media is not supported"):
        backend.append(_make_staging(tmp_path, "session_001"), "TEST", 1)

def test_bd_profile_keeps_growisofs_argv(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        A BD-R profile must keep the growisofs -Z argv byte for byte,
        including -udf and -allow-limited-size. Regression guard for
        the 4 GiB genisoimage fix.
    """
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        return _FakePopen(["ok\n"], 0)

    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.run",
        lambda cmd, **kwargs: _mediainfo_completed(_MEDIAINFO_BLANK_BD),
    )
    monkeypatch.setattr("oddarchiver.disc.subprocess.Popen", fake_popen)
    staging = _make_staging(tmp_path)
    backend = DiscBackend(_FAKE_SR)
    backend.init(staging, "TEST", 0)
    assert captured[0] == [
        "growisofs", "-Z", _FAKE_SR,
        "-udf", "-allow-limited-size", "-R", "-T",
        "-V", "TEST",
        "-use-the-force-luke=notray",
        str(staging),
    ]


def test_bd_append_keeps_growisofs_argv(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        growisofs -M for append must keep -udf and -allow-limited-size.
        A plain ISO9660 build would reintroduce the 4 GiB genisoimage crash.
    """
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        return _FakePopen(["ok\n"], 0)

    _stub_disc_mediainfo(monkeypatch, session_count=1)
    monkeypatch.setattr("oddarchiver.disc.subprocess.Popen", fake_popen)
    staging = _make_staging(tmp_path, "session_001")
    backend = DiscBackend(_FAKE_SR)
    backend.append(staging, "TEST", 1)
    assert captured[0] == [
        "growisofs", "-M", _FAKE_SR,
        "-udf", "-allow-limited-size", "-R", "-T",
        "-V", "TEST",
        "-use-the-force-luke=notray",
        str(staging),
    ]


def _cli_burn_args(tmp_path: Path, command: str) -> argparse.Namespace:
    """Build a dispatch namespace that selects DiscBackend on a fake device."""
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    (source / "a.txt").write_bytes(b"hello")
    return argparse.Namespace(
        command=command,
        source=str(source),
        dest=None,
        device=_FAKE_SR,
        test_iso=None,
        label="TEST",
        encrypt="none",
        key=None,
        dry_run=False,
        no_cache=False,
        disc_size="25gb",
        prefill=None,
        session=None,
        force=False,
        level="fast",
        slow=False,
        mirror=None,
    )


def _stub_cd_refusal(monkeypatch) -> list:
    """Refuse CD via dvd+rw-mediainfo and fail if a burn program starts."""
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(list(cmd))
        raise AssertionError(f"no burn program may run for CD: {cmd}")

    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.run",
        lambda cmd, **kwargs: _mediainfo_completed(
            returncode=1, stderr=_MEDIAINFO_CD_REFUSAL_STDERR),
    )
    monkeypatch.setattr("oddarchiver.disc.subprocess.Popen", fake_popen)
    return popen_calls


def test_cli_init_refuses_cd(monkeypatch, tmp_path, capsys):
    """
    Input:  monkeypatch, tmp_path, capsys
    Output: None
    Details:
        CLI init reaches a burn through DiscBackend.mediainfo. A CD
        must exit 1 with the unsupported-media error and must not start
        growisofs.
    """
    popen_calls = _stub_cd_refusal(monkeypatch)
    rc = dispatch(_cli_burn_args(tmp_path, "init"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "CD media is not supported" in err
    assert popen_calls == []


def test_cli_sync_refuses_cd(monkeypatch, tmp_path, capsys):
    """
    Input:  monkeypatch, tmp_path, capsys
    Output: None
    Details:
        CLI sync also reads mediainfo before any append. A CD must exit
        1 with the unsupported-media error and must not start growisofs.
    """
    popen_calls = _stub_cd_refusal(monkeypatch)
    rc = dispatch(_cli_burn_args(tmp_path, "sync"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "CD media is not supported" in err
    assert popen_calls == []


# Verbatim growisofs output captured from a real mid-burn USB unplug: a
# Buffalo drive writing a 7 GiB session to M-DISC BD-R, cable pulled at 13%.
_REAL_DISCONNECT_OUTPUT = [
    " 13.34% done, estimate finish Sun Sep 20 19:52:14 2026",
    ":-( unable to WRITE@LBA=82ac0h: Input/output error",
    ":-( write failed: Input/output error",
    "/dev/sr0: flushing cache",
    ":-( unable to FLUSH CACHE: Input/output error",
    ":-( unable to SYNCHRONOUS FLUSH CACHE: Input/output error",
]


def test_disconnect_needs_both_io_error_and_missing_device(tmp_path):
    """
    Input:  tmp_path - used to build a device path that exists
    Output: None
    Details:
        The I/O-error phrases alone must not be read as a disconnect.
        Failing media raises the same text while the drive stays on the
        bus, and that case needs the SCSI-fault path instead. Only a
        missing device node proves the drive left. Uses the real
        captured output from a physical unplug.
    """
    present = tmp_path / "sr0"
    present.write_bytes(b"")
    assert _is_disconnect(_REAL_DISCONNECT_OUTPUT, str(present)) is False
    assert _is_disconnect(_REAL_DISCONNECT_OUTPUT, str(tmp_path / "gone")) is True


def test_disc_full_text_is_not_a_disconnect(tmp_path):
    """
    Input:  tmp_path - provides a path that does not exist
    Output: None
    Details:
        A full disc must keep routing to the capacity error even when
        the device node is absent, so the two diagnoses cannot swap.
    """
    full = [
        "1234 blocks are free, 5678 to be written!",
        ":-( write failed: No space left on device",
    ]
    assert _is_disconnect(full, str(tmp_path / "gone")) is False


def test_append_disconnect_raises_diagnosed_error(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        A disconnect during append must not surface as a bare growisofs
        exit code, which is what shipped before. The message must say
        the drive disappeared, must NOT claim prior sessions were
        checked (they cannot be, the drive is gone), must warn that the
        device path can change on reconnect, and must steer away from
        init and from growisofs's own -Z suggestion.
    """
    missing = str(tmp_path / "sr-gone")

    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.run",
        lambda cmd, **kwargs: _mediainfo_completed(_MEDIAINFO_BLANK_BD),
    )
    monkeypatch.setattr(
        "oddarchiver.disc._run_growisofs",
        lambda cmd, **kwargs: (5, _REAL_DISCONNECT_OUTPUT),
    )

    backend = DiscBackend(missing)
    monkeypatch.setattr(backend, "_guard", lambda *a, **k: None)
    with pytest.raises(RuntimeError) as exc:
        backend.append(_make_staging(tmp_path, "session_002"), "TEST", 1)

    msg = str(exc.value)
    assert "disappeared" in msg
    assert "could NOT be checked" in msg
    assert "device path did not change" in msg
    assert "verify --level fast" in msg
    assert "exit code" not in msg


def test_disconnect_never_claims_prior_sessions_verified(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        States the safety property on its own. Other failure paths call
        _reverify_prior_sessions and report the result. A disconnect
        cannot, because the device is gone, so the message must never
        assert prior sessions are readable. Claiming verified data
        without reading it is the failure this guards.
    """
    missing = str(tmp_path / "sr-gone")
    called = []

    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.run",
        lambda cmd, **kwargs: _mediainfo_completed(_MEDIAINFO_BLANK_BD),
    )
    monkeypatch.setattr(
        "oddarchiver.disc._run_growisofs",
        lambda cmd, **kwargs: (5, _REAL_DISCONNECT_OUTPUT),
    )
    backend = DiscBackend(missing)
    monkeypatch.setattr(backend, "_guard", lambda *a, **k: None)
    monkeypatch.setattr(
        backend, "_reverify_prior_sessions",
        lambda n: called.append(n) or True,
    )

    with pytest.raises(RuntimeError) as exc:
        backend.append(_make_staging(tmp_path, "session_002"), "TEST", 1)

    assert called == [], "must not read the medium when the drive is gone"
    assert "re-verified readable" not in str(exc.value)
