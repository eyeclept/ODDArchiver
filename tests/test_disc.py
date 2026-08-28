"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for disc.py. ISOBackend, DiscInfo, double-burn guard,
    prefill, parse_disc_size, and _parse_mediainfo.
"""
# Imports
import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from oddarchiver.disc import (
    DiscBackend,
    DiscInfo,
    ISOBackend,
    _parse_mediainfo,
    parse_disc_size,
)

# Globals
SMALL_DISC = 10 * 2**20  # 10 MiB. Keeps ISO builds fast in tests.

# Blob name for the test file in session_000 (sha256("0:hello.txt"))
_HELLO_BLOB = hashlib.sha256(b"0:hello.txt").hexdigest()


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


def test_parse_mediainfo_blank_disc_returns_zero_sessions():
    """
    Input:  None
    Output: None
    Details:
        Blank BD-R discs report "Number of Sessions: 1" with "Disc status: blank".
        _parse_mediainfo must return session_count=0 so init() proceeds instead
        of reporting "disc already initialized".
        Regression for bug: false "disc already initialized" on blank BD-R media.
    """
    fixture = (
        "Disc status:           blank\n"
        "Number of Sessions:    1\n"
        "State of Last Session: empty\n"
        "Next Writable Address: 0\n"
        "Free Blocks:           12219392*2KB\n"
    )
    info = _parse_mediainfo(fixture)
    assert info.session_count == 0
    assert info.remaining_bytes == 12219392 * 2048


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
        Live-confirmed fix (Assist/HANDOVER_growisofs_append_kernel_limit.md):
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