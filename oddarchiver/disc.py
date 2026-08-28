"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    BurnBackend abstract interface plus DiscBackend (growisofs to write,
    dvd+rw-mediainfo for capacity, xorriso for direct SG_IO reads) and
    ISOBackend (genisoimage) implementations. ISOBackend is used for
    --test-iso mode and all automated testing. DiscBackend targets
    physical BD-R drives.
"""
# Imports
from __future__ import annotations

import abc
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Globals
BDXL_DISC_BYTES = 93 * 2**30   # 100 GB BDXL usable headroom (~93 GiB)
# Fixed, root-owned path -- never the invoking user's own PATH. See
# usb_reauth.py's module docstring and SETUP.md for why this must be a
# manual `sudo install` target, not something `pip install -e .` creates.
_USB_REAUTH_HELPER = "/usr/local/sbin/oddarchiver-usb-reauth"
_log = logging.getLogger(__name__)


@dataclass
class DiscInfo:
    """Snapshot of disc/ISO capacity state."""
    session_count: int
    remaining_bytes: int
    used_bytes: int
    label: str
    mirror_device: str = ""


class BurnBackend(abc.ABC):
    """Abstract interface for disc-write and disc-read operations."""

    @abc.abstractmethod
    def init(self, staging: Path, label: str, expected_session_count: int = 0) -> None:
        """
        Input:  staging               - path to prepared session_000 directory tree
                label                 - UDF volume label
                expected_session_count - session count recorded at run start (double-burn guard)
        Output: None
        Details:
            Write the first session to the target medium.
            Aborts if session count changed since expected_session_count was read.
        """

    @abc.abstractmethod
    def append(self, staging: Path, label: str, expected_session_count: int) -> None:
        """
        Input:  staging               - path to prepared session_NNN directory tree
                label                 - UDF volume label
                expected_session_count - session count recorded at run start (double-burn guard)
        Output: None
        Details:
            Append a new session to an already-initialized medium.
            Aborts if session count changed since expected_session_count was read.
        """

    @abc.abstractmethod
    def mediainfo(self) -> DiscInfo:
        """
        Input:  None
        Output: DiscInfo - current capacity snapshot
        Details:
            Query the target medium for session count and space.
        """

    @abc.abstractmethod
    def read_path(self, path: str) -> bytes:
        """
        Input:  path - relative path within the disc/ISO filesystem
        Output: bytes - raw file content
        Details:
            Read a file from the target medium into memory.
        """


class DiscBackend(BurnBackend):
    """
    Input:  device - block device path (e.g. /dev/sr0)
    Output: N/A (class)
    Details:
        Wraps growisofs for writes, dvd+rw-mediainfo for capacity queries,
        and xorriso for reads. All three talk to the drive directly over
        SG_IO rather than through the kernel block device layer, so reads
        never depend on the OS's own capacity revalidation -- which was
        observed to stay stale for 5+ minutes after a real multi-minute
        burn, well past any bounded retry, with no way to force it short
        of a physical eject/reinsert or root.
    """

    def __init__(self, device: str) -> None:
        self.device = device

    def init(self, staging: Path, label: str, expected_session_count: int = 0) -> None:
        """
        Input:  staging, label, expected_session_count
        Output: None
        Details:
            Burns session 0 with growisofs -Z (new disc). Progress is
            streamed as a single in-place bar via _run_growisofs() instead
            of growisofs's own repeated percentage lines.
        """
        self._guard(expected_session_count)
        cmd = [
            "growisofs", "-Z", self.device,
            "-R", "-T",
            "-V", label,
            "-use-the-force-luke=notray",
            str(staging),
        ]
        returncode, _ = _run_growisofs(cmd)
        if returncode != 0:
            raise RuntimeError(f"growisofs -Z failed with exit code {returncode}")

    def append(self, staging: Path, label: str, expected_session_count: int) -> None:
        """
        Input:  staging, label, expected_session_count
        Output: None
        Details:
            Appends a session with growisofs -M (existing disc). Progress
            is streamed the same way as init(). On the specific
            stale-kernel-block-layer failure, attempts one automatic
            recovery (USB re-enumeration via _attempt_usb_reauth()) and
            retries once before giving up -- see
            Assist/HANDOVER_growisofs_append_kernel_limit.md for why this
            failure happens and why the retry works.
        """
        self._guard(expected_session_count)
        cmd = [
            "growisofs", "-M", self.device,
            "-R", "-T",
            "-V", label,
            "-use-the-force-luke=notray",
            str(staging),
        ]
        returncode, output_lines = _run_growisofs(cmd)
        stale_cache = returncode != 0 and any(
            "pread64" in ln and "primary volume descriptor" in ln
            for ln in output_lines
        )
        if stale_cache:
            print(
                "  growisofs could not read the prior session back (stale "
                "kernel cache); attempting automatic USB re-enumeration...",
                flush=True,
            )
            if self._attempt_usb_reauth():
                print(
                    "  Re-enumeration succeeded; waiting for the device "
                    "node to reappear...",
                    flush=True,
                )
                if self._wait_for_device_node():
                    print("  Device is back; retrying append...", flush=True)
                    returncode, output_lines = _run_growisofs(cmd)
                else:
                    _log.warning(
                        "%s did not reappear within the wait budget after "
                        "USB re-enumeration; not retrying growisofs",
                        self.device,
                    )
        if returncode != 0:
            if stale_cache:
                raise RuntimeError(
                    f"growisofs -M could not read the existing session(s) back "
                    f"from {self.device} before appending, and automatic "
                    "recovery did not clear it. This is a stale kernel "
                    "block-device cache, not disc corruption: nothing signals "
                    "a media-change event to the kernel after an in-place "
                    "burn, so growisofs's own read of the prior session fails "
                    "even though the drive and disc are fine. Do NOT follow "
                    "growisofs's own '-Z' suggestion in the output above -- "
                    "that option re-initializes the disc and would overwrite "
                    "the existing archive. Either the oddarchiver-usb-reauth "
                    "sudoers entry is not set up (see SETUP.md) or it failed "
                    "-- check the warning logged above it. Physically eject "
                    "and reinsert the disc (or reboot), then retry this sync."
                )
            raise RuntimeError(f"growisofs -M failed with exit code {returncode}")

    def _attempt_usb_reauth(self) -> bool:
        """
        Input:  None
        Output: bool - True if the reauth helper ran and reported success
        Details:
            Invokes _USB_REAUTH_HELPER via `sudo -n` (non-interactive:
            fails immediately instead of hanging on a password prompt in
            an unattended cron job). That path is a fixed, root-owned
            system location, never the invoking user's own PATH -- it
            must be installed with a manual `sudo install` step (see
            SETUP.md), not via `pip install -e .`, since sudoers trusts
            an exact path string, not who currently controls the file
            living there. Logs a warning (not an exception -- this is an
            expected, recoverable condition when the one-time root
            install/sudoers setup has not been done yet) naming exactly
            why it did not run, so a cron job's logs show the real
            reason.
        """
        if not Path(_USB_REAUTH_HELPER).is_file():
            _log.warning(
                "%s is not installed; cannot attempt automatic recovery "
                "(see SETUP.md for the one-time root-owned install step)",
                _USB_REAUTH_HELPER,
            )
            return False
        result = subprocess.run(
            ["sudo", "-n", _USB_REAUTH_HELPER, Path(self.device).name],
            capture_output=True,
        )
        if result.returncode != 0:
            _log.warning(
                "%s failed (exit %d): %s",
                _USB_REAUTH_HELPER,
                result.returncode,
                result.stderr.decode("utf-8", errors="replace").strip(),
            )
            return False
        return True

    def _wait_for_device_node(self, retries: int = 10, retry_delay: float = 1.0) -> bool:
        """
        Input:  retries     - number of polls before giving up
                retry_delay - seconds to wait between polls
        Output: bool - True once the device node exists
        Details:
            A successful "authorized" write returns well before the
            kernel finishes destroying and recreating the USB device:
            confirmed live -- an immediate growisofs retry right after
            _attempt_usb_reauth() reported success hit
            "unable to open64(...): No such file or directory", with the
            device node back correctly (and readable at the right
            capacity) moments later with no further action taken. Polls
            for the node's existence with a bounded wait instead of
            retrying immediately into that race.
        """
        for _ in range(retries):
            if Path(self.device).exists():
                return True
            time.sleep(retry_delay)
        return Path(self.device).exists()

    def mediainfo(self, retries: int = 6, retry_delay: float = 5.0) -> DiscInfo:
        """
        Input:  retries     - number of extra attempts if the drive reports no media
                retry_delay - seconds to wait between attempts
        Output: DiscInfo
        Details:
            Runs dvd+rw-mediainfo and parses session count and capacity.
            Retries when the drive reports "no media". That happens right after
            a burn while the drive reloads the tray (often 5 to 30 seconds).
        """
        last_err = ""
        for attempt in range(retries + 1):
            result = subprocess.run(
                ["dvd+rw-mediainfo", self.device],
                capture_output=True,
            )
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            if result.returncode == 0:
                stdout = result.stdout.decode("utf-8", errors="replace")
                return _parse_mediainfo(stdout)
            last_err = stderr
            if "no media" in stderr.lower() and attempt < retries:
                print(
                    f"  Drive not ready (attempt {attempt + 1}/{retries + 1}); "
                    f"waiting {retry_delay:.0f}s for disc...",
                    flush=True,
                )
                time.sleep(retry_delay)
            else:
                break
        raise RuntimeError(f"dvd+rw-mediainfo failed: {last_err}")

    def read_path(self, path: str, retries: int = 6, retry_delay: float = 5.0) -> bytes:
        """
        Input:  path        - relative path on the disc filesystem
                retries     - extra attempts on a transient drive-not-ready error
                retry_delay - seconds to wait between attempts
        Output: bytes
        Details:
            Extracts the file directly from the medium with
            `xorriso -indev DEVICE -osirrox on -extract`, the same SG_IO-level
            access mediainfo() already uses through dvd+rw-mediainfo, instead
            of mounting a filesystem through the kernel block device layer.
            A real several-minute burn was observed to leave the kernel's own
            block-layer capacity for this device stuck at a stale placeholder
            for 5+ minutes of retries (mount, blkid, even a raw read past the
            first block all stayed broken) even though the SCSI-level TOC and
            the burned data were both intact and immediately readable via
            SG_IO the whole time -- nothing signals a kernel media-change
            event for an in-place session append, only a physical eject and
            reinsert. xorriso reads the medium the same direct way, so this
            never depends on that kernel-level state at all.
            Raises ValueError if path is not a known-safe blob or manifest path.
            Raises FileNotFoundError if xorriso loads the ISO tree fine but
            path is not present in it (e.g. probing for a session that does
            not exist yet).
            Raises RuntimeError if the drive/medium itself cannot be read,
            after retrying a transient not-ready condition (mirroring
            mediainfo()'s retry). A physical eject/reinsert -- the
            documented remedy for append()'s stale-block-layer error --
            was observed to make the desktop's automount monitor grab the
            device via a real kernel mount as a side effect, which blocks
            xorriso's SG_IO access with "Cannot open busy device" /
            "Device or resource busy" until released. On that specific
            signature this unmounts the device (best-effort, ignoring
            failure if it was never mounted) and retries immediately
            rather than waiting the full settle delay, since releasing an
            existing mount is near-instant, unlike a drive genuinely
            still settling after a burn.
        """
        from oddarchiver.manifest import validate_disc_read_path
        validate_disc_read_path(path)

        last_err = ""
        for attempt in range(retries + 1):
            with tempfile.TemporaryDirectory(prefix="oddarchiver_read_") as tmp:
                dest = Path(tmp) / "blob"
                result = subprocess.run(
                    [
                        "xorriso", "-indev", self.device,
                        "-osirrox", "on",
                        "-extract", "/" + path, str(dest),
                    ],
                    capture_output=True,
                )
                if result.returncode == 0 and dest.exists():
                    return dest.read_bytes()
                stderr = result.stderr.decode("utf-8", errors="replace")
                if "Cannot determine attributes of" in stderr:
                    raise FileNotFoundError(f"Path not found on disc: {path!r}")
            last_err = next(
                (line for line in reversed(stderr.strip().splitlines()) if line.strip()),
                "unknown xorriso failure",
            )
            if "no media" in stderr.lower() and attempt < retries:
                print(
                    f"  Drive not ready (attempt {attempt + 1}/{retries + 1}); "
                    f"waiting {retry_delay:.0f}s for disc...",
                    flush=True,
                )
                time.sleep(retry_delay)
            elif (
                ("busy device" in stderr.lower() or "resource busy" in stderr.lower())
                and attempt < retries
            ):
                print(
                    f"  Drive busy (attempt {attempt + 1}/{retries + 1}); "
                    "releasing an automount and retrying...",
                    flush=True,
                )
                subprocess.run(
                    ["udisksctl", "unmount", "-b", self.device],
                    capture_output=True,
                )
            else:
                break
        raise RuntimeError(f"Failed to read {path!r} from {self.device} via xorriso: {last_err}")

    def _guard(self, expected: int) -> None:
        """
        Input:  expected - session count recorded at start of run
        Output: None
        Details:
            Re-reads mediainfo and aborts if session count differs from expected.
            Prevents double-burn if the disc was written concurrently.
        """
        current = self.mediainfo().session_count
        if current != expected:
            raise RuntimeError(
                f"Double-burn guard: expected {expected} sessions, "
                f"found {current} — aborting to prevent duplicate session"
            )


class ISOBackend(BurnBackend):
    """
    Input:  iso_path  - filesystem path for the ISO file
            disc_size - simulated capacity in bytes (default 23 GiB for 25 GB BD-R)
    Output: N/A (class)
    Details:
        Wraps genisoimage to build UDF ISOs for --test-iso mode and CI.
        Sessions accumulate in a sibling directory that replaces the .iso
        suffix with .d. The ISO is rebuilt from that tree on every write.
        read_path() reads from that directory. No loop mount is required.
    """

    DEFAULT_DISC_BYTES = 23 * 2**30   # 23 GiB (~25 GB BD-R usable headroom)
    _META_SUFFIX = ".meta.json"

    def __init__(self, iso_path: Path, disc_size: int | None = None) -> None:
        self.iso_path = Path(iso_path)
        self.disc_size = disc_size if disc_size is not None else self.DEFAULT_DISC_BYTES
        # Sibling directory accumulates all session content for ISO rebuilds.
        self._sessions_root = self.iso_path.with_suffix(".d")
        self._meta_path = self.iso_path.with_name(
            self.iso_path.name + self._META_SUFFIX
        )

    # --- public interface ---

    def init(self, staging: Path, label: str, expected_session_count: int = 0) -> None:
        """
        Input:  staging, label, expected_session_count
        Output: None
        Details:
            Copies staging contents into sessions_root and builds a new UDF ISO.
        """
        self._guard(expected_session_count)
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        _copy_staging(staging, self._sessions_root)
        self._build_iso(label)

    def append(self, staging: Path, label: str, expected_session_count: int) -> None:
        """
        Input:  staging, label, expected_session_count
        Output: None
        Details:
            Adds a new session to sessions_root and rebuilds the ISO from all sessions.
        """
        self._guard(expected_session_count)
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        _copy_staging(staging, self._sessions_root)
        self._build_iso(label)

    def prefill(self, prefill_bytes: int) -> None:
        """
        Input:  prefill_bytes - bytes to simulate as already used
        Output: None
        Details:
            Records simulated used space in a metadata file so mediainfo()
            reports reduced remaining_bytes. No real data is written to disc.
            Used with --prefill to test capacity warning paths.
        """
        self._meta_path.write_text(json.dumps({"prefill_bytes": prefill_bytes}))

    def mediainfo(self) -> DiscInfo:
        """
        Input:  None
        Output: DiscInfo
        Details:
            Counts session_* directories in sessions_root, measures ISO size,
            adds prefill_bytes from metadata, and computes remaining capacity.
        """
        if not self._sessions_root.exists():
            return DiscInfo(
                session_count=0,
                remaining_bytes=self.disc_size,
                used_bytes=0,
                label="",
            )
        sessions = sorted(self._sessions_root.glob("session_*"))
        session_count = len(sessions)
        iso_bytes = self.iso_path.stat().st_size if self.iso_path.exists() else 0
        prefill_bytes = self._read_prefill()
        used_bytes = iso_bytes + prefill_bytes
        remaining_bytes = max(0, self.disc_size - used_bytes)
        label = self._read_label(sessions)
        return DiscInfo(
            session_count=session_count,
            remaining_bytes=remaining_bytes,
            used_bytes=used_bytes,
            label=label,
        )

    def read_path(self, path: str) -> bytes:
        """
        Input:  path - relative path within the disc filesystem
        Output: bytes
        Details:
            Reads from the sessions_root directory tree. No loop mount is required.
            Raises ValueError if path is not a known-safe blob or manifest path.
        """
        from oddarchiver.manifest import validate_disc_read_path
        validate_disc_read_path(path)
        target = self._sessions_root / path
        if not target.exists():
            raise FileNotFoundError(f"Path not found in ISO sessions: {path!r}")
        return target.read_bytes()

    # --- private helpers ---

    def _guard(self, expected: int) -> None:
        """
        Input:  expected - session count recorded at start of run
        Output: None
        Details:
            Re-reads session count and aborts if it changed (double-burn guard).
        """
        current = self.mediainfo().session_count
        if current != expected:
            raise RuntimeError(
                f"Double-burn guard: expected {expected} sessions, "
                f"found {current} — aborting to prevent duplicate session"
            )

    def _build_iso(self, label: str) -> None:
        """
        Input:  label - UDF volume label (truncated to 32 chars per UDF spec)
        Output: None
        Details:
            Calls genisoimage to build a UDF ISO from sessions_root.
            Writes to a .tmp file then renames to avoid a partial ISO.
        """
        tmp = self.iso_path.with_suffix(".tmp")
        cmd = [
            "genisoimage",
            "-udf", "-R",
            "-V", label[:32],
            "-o", str(tmp),
            str(self._sessions_root),
        ]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"genisoimage failed (exit {result.returncode})")
        tmp.rename(self.iso_path)

    def _read_prefill(self) -> int:
        """
        Input:  None
        Output: int - prefill_bytes from metadata file, or 0
        """
        if not self._meta_path.exists():
            return 0
        try:
            return json.loads(self._meta_path.read_text()).get("prefill_bytes", 0)
        except (json.JSONDecodeError, OSError):
            return 0

    def _read_label(self, sessions: list[Path]) -> str:
        """
        Input:  sessions - sorted list of session_NNN directories
        Output: str - label from the first readable manifest.json, or ""
        """
        for session_dir in sessions:
            manifest = session_dir / "manifest.json"
            if manifest.exists():
                try:
                    return json.loads(manifest.read_text()).get("label", "")
                except (json.JSONDecodeError, OSError):
                    pass
        return ""


# --- module-level helpers ---

# growisofs's own progress line, straight from its usage strings:
# "%11lld/%lld (%4.1f%%) @%.1fx, remaining %d:%02d RBU %5.1f%% UBU %5.1f%%"
# (or "@0x, remaining ??:??" when speed/ETA are not yet known).
_GROWISOFS_PROGRESS_RE = re.compile(
    r"^\s*(\d+)/(\d+)\s+\(\s*([\d.]+)%\)\s+@([^,]+),\s*remaining\s+(\S+):(\S+)"
)
# The mkisofs/xorriso child process's own build-phase update, e.g.
# "xorriso : UPDATE :       8 files added in 1 seconds".
_XORRISO_FILES_ADDED_RE = re.compile(r"UPDATE\s*:\s*(\d+)\s+files added")
# xorriso's own percentage-based progress line for long-running scans/writes
# on a large tree, e.g.
# "xorriso : UPDATE : 62.29% done, estimate finish Sun Apr 09 10:35:29 2023"
# -- repeats once per ~0.15-0.2% on a large real archive, flooding the
# terminal with one new line per update if left to print through raw.
_XORRISO_PERCENT_DONE_RE = re.compile(
    r"UPDATE\s*:\s*([\d.]+)%\s+done,\s*estimate finish\s+(.+)"
)


def _run_growisofs(cmd: list[str]) -> tuple[int, list[str]]:
    """
    Input:  cmd - full growisofs argv
    Output: (returncode, output_lines) - process return code and every raw
            line growisofs printed (stdout+stderr merged, one entry per
            line, in order), so callers can recognize a specific known
            failure signature in the text growisofs already printed.
    Details:
        Streams growisofs live instead of letting it dump raw output.
        growisofs itself repeats a
        "<written>/<total> (<pct>%) @<speed>x, remaining M:SS ..." line
        very frequently during the actual write, and the mkisofs/xorriso
        child process it shells out to for building the image repeats
        either "<n> files added in <n> seconds" for a moderate source
        tree or "<pct>% done, estimate finish <timestamp>" for a large
        one (observed once per ~0.15-0.2% on a real 6000+ file, 8+ GB
        archive) -- all three flood the terminal with one new line per
        update on a multi-minute burn. Recognized progress lines are
        re-rendered as a single, in-place updating line: the growisofs
        percentage and the xorriso percent-done line as a bar in the
        same style as session._print_bar, the build-phase file count as
        a plain live counter since it has no known total. Anything else
        -- errors, "Executing '...'", "flushing cache", "closing
        track", and any line this does not recognize -- prints through
        unchanged so nothing real is ever swallowed.
    """
    from oddarchiver.session import _print_bar

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    bar_active = False
    output_lines: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            output_lines.append(line)
            progress = _GROWISOFS_PROGRESS_RE.search(line)
            if progress:
                written, total, pct, speed, mins, secs = progress.groups()
                _print_bar(
                    int(written), int(total),
                    suffix=f"{pct}% @{speed}, {mins}:{secs} remaining",
                )
                bar_active = True
                continue
            added = _XORRISO_FILES_ADDED_RE.search(line)
            if added:
                print(f"\r  Building ISO image: {added.group(1)} files added...", end="", flush=True)
                bar_active = True
                continue
            percent_done = _XORRISO_PERCENT_DONE_RE.search(line)
            if percent_done:
                pct_str, finish = percent_done.groups()
                _print_bar(
                    round(float(pct_str)), 100,
                    suffix=f"estimate finish {finish.strip()}",
                )
                bar_active = True
                continue
            if bar_active:
                print()
                bar_active = False
            print(line, flush=True)
    finally:
        if bar_active:
            print()
    return proc.wait(), output_lines


def resolve_usb_device_sysfs_path(
    device: str, sys_class_block: Path = Path("/sys/class/block"),
) -> Path | None:
    """
    Input:  device         - block device path, e.g. "/dev/sr0"
            sys_class_block - root of the sysfs block-class tree; override
                              in tests to point at a fake tree
    Output: Path to the device's USB sysfs directory (the one whose
            "authorized" attribute controls whether the kernel treats it
            as plugged in), or None if it cannot be resolved (not a USB
            device, or the block device does not exist).
    Details:
        Walks <sys_class_block>/<name>/device (a symlink into the real
        device tree) up through its ancestors until it finds a directory
        that has both "idVendor" and "authorized" -- the unique
        combination that identifies the actual USB device node, as
        opposed to a USB interface (has neither), a SCSI target, or a
        SCSI host underneath it. This makes no assumption about bus
        number, port number, or hub depth -- it works for any USB
        optical drive on any machine's topology.

        Confirmed live (Assist/HANDOVER_growisofs_append_kernel_limit.md):
        deauthorizing then reauthorizing the path this resolves to
        forces a full USB re-enumeration, including the real capacity
        probe growisofs -M's own kernel-block-layer read otherwise never
        gets after an in-place burn -- fixing the append failure with no
        tray action and no interactive session. Used by
        DiscBackend._attempt_usb_reauth() and the standalone
        oddarchiver-usb-reauth helper script (oddarchiver/usb_reauth.py).
    """
    class_path = sys_class_block / Path(device).name
    try:
        node = (class_path / "device").resolve(strict=True)
    except OSError:
        return None
    while node != node.parent:
        if (node / "idVendor").exists() and (node / "authorized").exists():
            return node
        node = node.parent
    return None


def parse_disc_size(size_str: str) -> int:
    """
    Input:  size_str - human-readable size e.g. "25gb", "100GB", "93GiB"
    Output: int - bytes
    Details:
        Supports decimal (kb/mb/gb/tb) and binary (kib/mib/gib/tib) suffixes.
        A string with no suffix is parsed as a raw integer byte count.
    """
    s = size_str.lower().strip()
    units = {
        "tib": 2**40, "gib": 2**30, "mib": 2**20, "kib": 2**10,
        "tb": 10**12, "gb": 10**9, "mb": 10**6, "kb": 10**3,
    }
    # longest suffix first to avoid prefix collisions (gib before gb)
    for suffix, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * multiplier)
    return int(s)


def _copy_staging(staging: Path, dest_root: Path) -> None:
    """
    Input:  staging   - source directory containing session_NNN/
            dest_root - destination root to merge into
    Output: None
    Details:
        Copies each top-level item from staging into dest_root, merging trees.
    """
    for item in staging.iterdir():
        dest = dest_root / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)


def _parse_mediainfo(stdout: str) -> DiscInfo:
    """
    Input:  stdout - text output from dvd+rw-mediainfo
    Output: DiscInfo
    Details:
        Extracts session count, remaining blocks (2KB units), total capacity,
        and label. Remaining and capacity blocks are multiplied by 2048.
        Blank BD-R discs report "Number of Sessions: 1" with an implicit empty
        session. Those discs are treated as 0 written sessions.
    """
    session_match = re.search(r"Sessions:\s+(\d+)", stdout)
    # dvd+rw-mediainfo uses *2KB for DVD and *2048 for BD. Accept both.
    _BLOCKS_RE = r"(\d+)\*(?:2KB|2048)"
    remaining_match = re.search(r"Remaining:\s+" + _BLOCKS_RE, stdout)
    # Free Blocks: appears once per track. The last entry is the writable track.
    free_blocks_all = re.findall(r"Free Blocks:\s+" + _BLOCKS_RE, stdout)
    capacity_match = re.search(r"READ CAPACITY:\s+" + _BLOCKS_RE, stdout)
    label_match = re.search(r"Volume id:\s+(\S+)", stdout, re.IGNORECASE)
    disc_status_match = re.search(r"Disc status:\s+(\S+)", stdout, re.IGNORECASE)
    last_session_match = re.search(r"State of Last Session:\s+(\S+)", stdout, re.IGNORECASE)

    session_count = int(session_match.group(1)) if session_match else 0

    # Prefer the explicit Remaining: field. Fall back to the last Free Blocks
    # entry (the invisible/writable track on BD-R SRM, which holds the actual
    # remaining capacity. Earlier tracks show 0 and must be ignored).
    if remaining_match:
        remaining_bytes = int(remaining_match.group(1)) * 2048
    elif free_blocks_all:
        remaining_bytes = int(free_blocks_all[-1]) * 2048
    else:
        remaining_bytes = 0

    total_bytes = int(capacity_match.group(1)) * 2048 if capacity_match else 0
    used_bytes = max(0, total_bytes - remaining_bytes)
    label = label_match.group(1) if label_match else ""

    disc_status = disc_status_match.group(1).lower() if disc_status_match else ""
    last_session_state = last_session_match.group(1).lower() if last_session_match else ""

    # A blank BD-R reports "Number of Sessions: 1" for the implicit empty session.
    # Treat the disc as uninitialized so init() proceeds rather than skipping.
    if disc_status == "blank" or (last_session_state == "empty" and session_count == 1):
        session_count = 0

    return DiscInfo(
        session_count=session_count,
        remaining_bytes=remaining_bytes,
        used_bytes=used_bytes,
        label=label,
    )
