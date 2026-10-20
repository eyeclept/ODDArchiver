"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    BurnBackend abstract interface plus DiscBackend and
    ISOBackend (genisoimage) implementations.

    DiscBackend uses dvd+rw-mediainfo for capacity, growisofs
    plus genisoimage to write DVD/BD, and xorriso for direct
    SG_IO reads. It refuses CD media.
    ISOBackend serves --test-iso mode and all automated
    testing. DiscBackend targets physical optical drives.
"""
# Imports
from __future__ import annotations

import abc
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Globals
CD_DISC_BYTES = 737_280_000        # ~700 MB CD-R usable capacity
DVD_SL_DISC_BYTES = 4_700_372_992  # ~4.7 GB single-layer DVD usable capacity
DVD_DL_DISC_BYTES = 8_547_991_552  # ~8.5 GB dual-layer DVD usable capacity
# Same GiB-floor headroom as bd-r (23 GiB) and bdxl (93 GiB): floor(50_050_629_632 / 2**30).
BD_DL_DISC_BYTES = 46 * 2**30      # 50 GB dual-layer BD-R usable headroom (~46 GiB)
BDXL_DISC_BYTES = 93 * 2**30       # 100 GB BDXL usable headroom (~93 GiB)
_SMALL_CONTROL_MAX_BYTES = 1024 * 1024
_LARGE_CONTROL_MAX_BYTES = 64 * 1024 * 1024
# Fixed, root-owned path, never the PATH of the invoking user. See
# the module docstring of usb_reauth.py and SETUP.md for why this must
# be a manual `sudo install` target, not something
# `pip install -e .` creates.
_USB_REAUTH_HELPER = "/usr/local/sbin/oddarchiver-usb-reauth"
logger = logging.getLogger(__name__)
_log = logger
# Text growisofs prints when the drive stops answering mid-write. Captured
# from a real mid-burn USB unplug on a Buffalo drive writing BD-R:
#   :-( unable to WRITE@LBA=82ac0h: Input/output error
#   :-( write failed: Input/output error
#   :-( unable to FLUSH CACHE: Input/output error
#   :-( unable to SYNCHRONOUS FLUSH CACHE: Input/output error
# These phrases alone do not prove a disconnect. A dying disc raises
# I/O errors too. _is_disconnect() pairs them with the device node
# vanishing.
_DISCONNECT_SIGNATURES = (
    "input/output error",
    "no such device",
    "transport endpoint is not connected",
    "no medium found",
)


_HW_FAULT_SIGNATURES = (
    "power on, reset, or bus device reset",
    "unit attention",
    "scsi bus reset",
    "bus device reset",
)
# A published ISO session directory. Only names that match this pattern
# count as sessions. Publish temp names start with "." and never match.
_SESSION_DIR_RE = re.compile(r"session_(\d{3})")
# _copy_staging builds each root item under ".<name>.partial-<random>"
# and moves a replaced tree aside as ".<name>.prior-<random>". The
# random part comes from tempfile and has no dot.
_PUBLISH_TEMP_RE = re.compile(r"\.(.+)\.(partial|prior)-[A-Za-z0-9_]+")



@dataclass
class DiscInfo:
    """Snapshot of disc/ISO capacity state."""
    session_count: int
    remaining_bytes: int
    used_bytes: int
    label: str
    mirror_device: str = ""



class BurnInterrupted(RuntimeError):
    """
    Input:  message - human-readable burn-failure description
    Output: N/A (exception)
    Details:
        Distinct from a plain growisofs refusal. Raised when a signal
        kills the growisofs subprocess (POSIX returncode
        < 0) or when its output matches a hardware-level I/O fault
        (Power on / bus device reset / SCSI Unit Attention). Callers
        must not treat this as an ordinary command error.
    """

class BurnBackend(abc.ABC):
    """Abstract interface for disc-write and disc-read operations."""

    @abc.abstractmethod
    def init(
        self,
        staging: Path,
        label: str,
        expected_session_count: int = 0,
        expected_archive_id: str | None = None,
        expected_volume_index: int | None = None,
    ) -> None:
        """
        Input:  staging                - path to prepared session_000 directory tree
                label                  - UDF volume label
                expected_session_count - session count recorded at run start
                expected_archive_id    - archive_id to continue, or None to skip
                expected_volume_index  - volume_index to continue, or None
        Output: None
        Details:
            Write the first session to the target medium.
            Aborts if the archive session count changed since the
            code recorded expected_session_count, or if expected identity
            does not match the disc.
        """

    @abc.abstractmethod
    def append(
        self,
        staging: Path,
        label: str,
        expected_session_count: int,
        expected_archive_id: str | None = None,
        expected_volume_index: int | None = None,
    ) -> None:
        """
        Input:  staging                - path to prepared session_NNN directory tree
                label                  - UDF volume label
                expected_session_count - session count recorded at run start
                expected_archive_id    - archive_id to continue, or None to skip
                expected_volume_index  - volume_index to continue, or None
        Output: None
        Details:
            Append a new session to an already-initialized medium.
            Aborts if the archive session count changed since the
            code recorded expected_session_count, or if expected identity
            does not match the disc.
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
    def read_paths(self, paths: Sequence[str]) -> dict[str, bytes]:
        """
        Input:  paths - relative paths within the disc/ISO filesystem
        Output: dict mapping each found path to its raw bytes
        Details:
            Read many files from the target medium in one operation.
            The function omits missing paths from the result and does
            not raise. A drive/medium failure raises RuntimeError.
        """

    def list_session_numbers(self) -> list[int] | None:
        """
        Input:  None
        Output: session numbers of the session_NNN root directories, or
                None when the backend cannot list them
        Details:
            load_session_manifests uses the list to request only real
            control files. None makes the caller probe all 1000 sessions.
        """
        return None

    def invalidate_session_list(self) -> None:
        """
        Input:  None
        Output: None
        Details:
            Clears any cached session listing. A burn or a disc change
            makes the cached list stale. The base backend keeps no
            cache, so this is a no-op here.
        """
        return None

    @abc.abstractmethod
    def read_path(self, path: str) -> bytes:
        """
        Input:  path - relative path within the disc/ISO filesystem
        Output: bytes - raw file content
        Details:
            Read a file from the target medium into memory.
            Implemented as a one-item wrapper over read_paths().
        """

    @abc.abstractmethod
    def extract_path_to_file(self, path: str, dest: Path) -> None:
        """
        Input:  path - relative path within the disc/ISO filesystem
                dest - destination file path
        Output: None (writes dest)
        Details:
            Extract one blob directly to dest without materializing it as
            a Python bytes object. Missing path raises FileNotFoundError.
            A drive/medium failure raises RuntimeError.
        """

    def extract_paths(
        self,
        paths: Sequence[str],
        dest_root: Path,
        max_bytes: int | None = None,
    ) -> int:
        """
        Input:  paths     - relative paths within the disc/ISO filesystem
                dest_root - local directory that receives the files
                max_bytes - optional cap on total bytes written
        Output: total bytes written
        Details:
            Generic fallback writes each path with extract_path_to_file,
            so a batch of blobs never sits in a dict of bytes. DiscBackend
            and ISOBackend override this with their own batch shape.
        """
        written = 0
        dest_root = Path(dest_root)
        for path in dict.fromkeys(paths):
            dest = dest_root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                prefix=dest.name + ".", suffix=".tmp", dir=str(dest.parent),
            )
            os.close(fd)
            tmp = Path(name)
            try:
                self.extract_path_to_file(path, tmp)
            except FileNotFoundError:
                _log.warning("Path not found on the medium: %s", path)
                tmp.unlink(missing_ok=True)
                continue
            try:
                size = tmp.stat().st_size
                if max_bytes is not None and written + size > max_bytes:
                    raise RuntimeError(
                        "Not enough local space to extract archive blobs."
                    )
                os.chmod(tmp, 0o600)
                os.replace(tmp, dest)
                written += size
            except (OSError, RuntimeError):
                _log.exception("Failed to extract %s to %s", path, dest)
                tmp.unlink(missing_ok=True)
                raise
        return written



def _retry_mediainfo_error(
    stderr: str, attempt: int, retries: int, retry_delay: float, device: str,
) -> bool:
    """
    Input:  stderr, attempt, retries, retry_delay, device
    Output: bool - True if mediainfo should retry
    Details:
        Raises on CD media. Waits before retrying on "no media".
    """
    if "non-dvd media" in stderr.lower():
        _log.warning("mediainfo: CD media detected on %s", device)
        _raise_cd_unsupported()
    if "no media" in stderr.lower() and attempt < retries:
        print(
            f"  Drive not ready (attempt {attempt + 1}/{retries + 1}); "
            f"waiting {retry_delay:.0f}s for disc...",
            flush=True,
        )
        time.sleep(retry_delay)
        return True
    return False


def _control_file_cap(path: str) -> int | None:
    """
    Input:  path - relative path on the disc filesystem
    Output: maximum control-file size, or None for blobs
    """
    return (
        _SMALL_CONTROL_MAX_BYTES if path == "volume.json"
        or path.endswith("/enc_mode.json")
        else _LARGE_CONTROL_MAX_BYTES if path in (
            "volume_baseline.json", "volume_baseline.enc",
        ) or path.endswith(("/manifest.json", "/manifest.enc"))
        else None
    )


def _retry_xorriso_read(
    device: str, stderr: str, attempt: int, retries: int, retry_delay: float,
) -> bool:
    """
    Input:  device, stderr, attempt, retries, retry_delay
    Output: True if the read should be retried
    """
    if "no media" in stderr.lower() and attempt < retries:
        print(
            f"  Drive not ready (attempt {attempt + 1}/{retries + 1}); "
            f"waiting {retry_delay:.0f}s for disc...",
            flush=True,
        )
        time.sleep(retry_delay)
        return True
    if (
        ("busy device" in stderr.lower() or "resource busy" in stderr.lower())
        and attempt < retries
    ):
        print(
            f"  Drive busy (attempt {attempt + 1}/{retries + 1}); "
            "releasing an automount and retrying...",
            flush=True,
        )
        subprocess.run(
            ["udisksctl", "unmount", "-b", device],
            capture_output=True,
        )
        return True
    return False


def _publish_extracted_paths(
    unique_paths: list[str], scratch: Path, dest_root: Path, max_bytes: int | None,
) -> int:
    """
    Input:  unique_paths - requested paths in extraction order
            scratch - temporary extraction root
            dest_root - final output root
            max_bytes - optional total-byte cap
    Output: total bytes published
    """
    written = 0
    for path in unique_paths:
        src = scratch / path
        if not src.is_file():
            continue
        size = src.stat().st_size
        if max_bytes is not None and written + size > max_bytes:
            raise RuntimeError(
                "Not enough local space to extract archive blobs."
            )
        dest = dest_root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)
        os.chmod(dest, 0o600)
        written += size
    return written


class DiscBackend(BurnBackend):
    """
    Input:  device - block device path (e.g. /dev/sr0)
    Output: N/A (class)
    Details:
        Wraps dvd+rw-mediainfo for capacity, growisofs plus genisoimage
        for DVD and BD writes, and xorriso for reads. This class refuses
        CD media. See _raise_cd_unsupported.
        All of those tools talk to the drive directly over SG_IO, not
        through the kernel block device layer. Reads never depend on
        the OS to revalidate capacity. That cached capacity
        stayed stale for 5+ minutes after a real multi-minute burn, well
        past any bounded retry. There is no way to force it short of a
        physical eject/reinsert or root.
    """

    def __init__(self, device: str) -> None:
        self.device = device
        self._session_numbers_cache: list[int] | None = None

    def init(
        self,
        staging: Path,
        label: str,
        expected_session_count: int = 0,
        expected_archive_id: str | None = None,
        expected_volume_index: int | None = None,
    ) -> None:
        """
        Input:  staging, label, expected_session_count,
                expected_archive_id, expected_volume_index
        Output: None
        Details:
            Burns session 0 with growisofs -Z plus genisoimage. This
            tool refuses CD profiles (MMC 0x0008/0x0009/0x000A),
            because a CD cannot stay appendable. See
            _raise_cd_unsupported. Progress streams as a single
            in-place bar. A disc-full signature raises a capacity
            error, not a bare exit code.
        """
        self._guard(
            expected_session_count,
            expected_archive_id=expected_archive_id,
            expected_volume_index=expected_volume_index,
        )
        self.invalidate_session_list()
        cmd = [
            "growisofs", "-Z", self.device,
            "-udf", "-allow-limited-size", "-R", "-T",
            "-V", label,
            "-use-the-force-luke=notray",
            str(staging),
        ]
        returncode, output_lines = _run_growisofs(cmd)
        if returncode != 0:
            if _is_disconnect(output_lines, self.device):
                _log.warning(
                    "%s: %s vanished mid-write; treating as disconnect",
                    "init" if True else "append", self.device,
                )
                _raise_disconnected(self.device, 0)
            self._raise_if_interrupted(
                returncode, output_lines, staging, expected_session_count,
            )
            self._raise_if_disc_full(
                output_lines, staging, expected_session_count, is_init=True,
            )
            raise RuntimeError(f"growisofs -Z failed with exit code {returncode}")

    def append(
        self,
        staging: Path,
        label: str,
        expected_session_count: int,
        expected_archive_id: str | None = None,
        expected_volume_index: int | None = None,
    ) -> None:
        """
        Input:  staging, label, expected_session_count,
                expected_archive_id, expected_volume_index
        Output: None
        Details:
            Appends a session with growisofs -M. This tool refuses
            CD profiles, because a CD cannot stay appendable. See
            _raise_cd_unsupported. Progress streams the same way
            as init().

            On the growisofs stale-kernel-block-layer failure, this
            method attempts one automatic recovery (USB
            re-enumeration via _attempt_usb_reauth()) and retries
            once before giving up. See the "Environment-specific
            hardware limitation" section of STATE_OF_THE_PROGRAM.md
            for why this failure happens and why the retry works.
            That recovery stays on the growisofs path only. A
            disc-full signature re-verifies prior sessions and
            raises a diagnosed error instead of a bare exit code.
        """
        self._guard(
            expected_session_count,
            expected_archive_id=expected_archive_id,
            expected_volume_index=expected_volume_index,
        )
        self.invalidate_session_list()
        cmd = [
            "growisofs", "-M", self.device,
            "-udf", "-allow-limited-size", "-R", "-T",
            "-V", label,
            "-use-the-force-luke=notray",
            str(staging),
        ]
        returncode, output_lines = _run_growisofs(cmd)
        returncode, output_lines, stale_cache = self._retry_stale_append(
            cmd, returncode, output_lines,
        )
        self._raise_append_failure(
            returncode, output_lines, stale_cache, staging, expected_session_count,
        )

    def _retry_stale_append(
        self, cmd: list[str], returncode: int, output_lines: list[str],
    ) -> tuple[int, list[str], bool]:
        """
        Input:  cmd, returncode, output_lines - growisofs -M command and result
        Output: tuple - (returncode, output_lines, stale_cache) after any retry
        Details:
            On the stale-cache signature, re-enumerates USB once, waits
            for the device node, and retries growisofs once.
        """
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
                    stale_cache = returncode != 0 and any(
                        "pread64" in ln and "primary volume descriptor" in ln
                        for ln in output_lines
                    )
                else:
                    _log.warning(
                        "%s did not reappear within the wait budget after "
                        "USB re-enumeration; not retrying growisofs",
                        self.device,
                    )
        return returncode, output_lines, stale_cache

    def _raise_append_failure(
        self, returncode: int, output_lines: list[str], stale_cache: bool,
        staging: Path, expected_session_count: int,
    ) -> None:
        """
        Input:  returncode, output_lines, stale_cache, staging,
                expected_session_count
        Output: None
        Details:
            Raises the diagnosed error for a failed growisofs -M run.
            Returns without raising when returncode is 0.
        """
        if returncode == 0:
            return
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
        if _is_disconnect(output_lines, self.device):
            _log.warning(
                "%s: %s vanished mid-write; treating as disconnect",
                "init" if False else "append", self.device,
            )
            _raise_disconnected(self.device, _session_n_from_staging(staging))
        self._raise_if_interrupted(
            returncode, output_lines, staging, expected_session_count,
        )
        self._raise_if_disc_full(
            output_lines, staging, expected_session_count, is_init=False,
        )
        raise RuntimeError(f"growisofs -M failed with exit code {returncode}")

    def _prior_session_clause(self, attempted: int) -> tuple[bool, str]:
        """
        Input:  attempted - session number that just failed to burn
        Output: tuple - (prior_ok, prior_clause) for the error message
        Details:
            Re-verifies prior sessions once and words the result.
        """
        prior_ok = self._reverify_prior_sessions(attempted)
        if attempted <= 0:
            prior_clause = "no prior sessions to re-verify"
        elif prior_ok:
            prior_clause = (
                f"sessions 000-{attempted - 1:03d} re-verified readable"
            )
        else:
            prior_clause = (
                f"sessions 000-{attempted - 1:03d} could NOT be re-verified "
                "- do not retry blindly; run 'oddarchiver verify --level fast' "
                "first"
            )
        return prior_ok, prior_clause

    def _raise_if_interrupted(
        self,
        returncode: int,
        output_lines: list[str],
        staging: Path,
        expected_session_count: int,
    ) -> None:
        """
        Input:  returncode             - growisofs wait() status
                output_lines           - captured growisofs stdout/stderr
                staging                - staging tree (used to infer session N)
                expected_session_count - double-burn guard count
        Output: None
        Details:
            Raises BurnInterrupted on a signal kill or hardware-fault
            signature. Re-reads every session below the one just
            attempted and folds that result into the exception message.
            Returns without raising for ordinary non-zero exits.
        """
        signal_killed = returncode < 0
        hw_fault = _is_hardware_fault(output_lines)
        if not signal_killed and not hw_fault:
            return
        attempted = _session_n_from_staging(staging)
        if attempted is None:
            attempted = expected_session_count
        prior_ok, prior_clause = self._prior_session_clause(attempted)
        joiner = "; " if prior_ok or attempted <= 0 else " and "
        raise BurnInterrupted(
            f"burn for session {attempted:03d} failed (exit {returncode})"
            f"{joiner}{prior_clause}"
        )

    def _raise_if_disc_full(
        self,
        output_lines: list[str],
        staging: Path,
        expected_session_count: int,
        *,
        is_init: bool,
    ) -> None:
        """
        Input:  output_lines           - captured writer stdout/stderr
                staging                - staging tree (used to infer session N)
                expected_session_count - double-burn guard count
                is_init                - True for first session, False for append
        Output: None
        Details:
            Raises RuntimeError when the writer printed the growisofs
            disc-full signature. Commit c2c8ba7 removed the xorriso
            writer, so no code path can produce xorriso disc-full
            wording. Init explains a packing/capacity miss and does not
            re-verify prior sessions. Append re-verifies prior sessions
            and folds that result into the message. Returns without
            raising when the signature is absent.
        """
        if not _is_disc_full_signature(output_lines):
            return
        if is_init:
            raise RuntimeError(
                "disc ran out of space during init (session 000). "
                "The source data does not fit the configured --disc-size "
                "or the real disc capacity, even after the pre-flight "
                "packing check. Check --disc-size, or free or reserve "
                "room on the medium."
            )
        attempted = _session_n_from_staging(staging)
        if attempted is None:
            attempted = expected_session_count
        prior_ok, prior_clause = self._prior_session_clause(attempted)
        if prior_ok or attempted <= 0:
            retry = (
                "Run 'oddarchiver verify --level fast', then retry the "
                "same sync or init command."
            )
        else:
            retry = "Retry the same sync or init command after verify passes."
        raise RuntimeError(
            f"disc ran out of space during append of session "
            f"{attempted:03d}. BD-R incremental recording fails the write "
            f"before a new session closes, so a failed append cannot "
            f"overwrite already-closed prior sessions. {prior_clause}. "
            f"{retry}"
        )

    def _reverify_session(self, s: int) -> bool:
        """
        Input:  s - prior session number
        Output: bool - True if manifest.enc or manifest.json reads back
        Details:
            Tries manifest.enc first; a successful read skips manifest.json.
        """
        for disc_path in (
            f"session_{s:03d}/manifest.enc",
            f"session_{s:03d}/manifest.json",
        ):
            try:
                self.read_path(disc_path)
                return True
            except FileNotFoundError:
                _log.warning(
                    "re-verify: %s not present for session %03d",
                    disc_path, s,
                )
                continue
            except Exception:
                _log.exception(
                    "re-verify: failed reading %s for session %03d",
                    disc_path, s,
                )
                return False
        return False

    def _reverify_prior_sessions(self, attempted_session: int) -> bool:
        """
        Input:  attempted_session - session number that just failed to burn
        Output: bool - True if every session below attempted_session is readable
        Details:
            Reads manifest.enc then manifest.json from each prior
            session. Used to annotate BurnInterrupted and disc-full
            append errors. Does not repair anything.
        """
        if attempted_session <= 0:
            return True
        for s in range(attempted_session):
            if not self._reverify_session(s):
                return False
        return True

    def _attempt_usb_reauth(self) -> bool:
        """
        Input:  None
        Output: bool - True if the reauth helper ran and reported success
        Details:
            Invokes _USB_REAUTH_HELPER via `sudo -n` (non-interactive:
            fails immediately instead of hanging on a password prompt in
            an unattended cron job). That path is a fixed, root-owned
            system location, never the PATH of the invoking user. A
            manual `sudo install` step (see SETUP.md) must install it,
            not `pip install -e .`, because sudoers trusts an exact
            path string, not who currently controls the file living
            there. Logs a warning, not an exception. This is an
            expected, recoverable condition when the one-time root
            install/sudoers setup is still missing. The warning
            names exactly why it did not run, so the logs of a cron
            job show the real reason.
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
            kernel finishes destroying and recreating the USB device.
            A live test confirmed this race: an immediate growisofs
            retry right after _attempt_usb_reauth() reported success
            hit "unable to open64(...): No such file or directory".
            The device node came back correctly (and readable at the
            right capacity) moments later with no further action.
            This function polls for the node with a bounded wait
            instead of retrying immediately into that race.
        """
        for _ in range(retries):
            if Path(self.device).exists():
                return True
            time.sleep(retry_delay)
        return Path(self.device).exists()

    def _read_label(self) -> str:
        """
        Input:  None
        Output: str - ISO volume identifier from the device, or ""
        Details:
            Reads the primary volume descriptor from the device node.
            An unreadable or invalid descriptor gives an empty label.
        """
        try:
            fd = os.open(self.device, os.O_RDONLY)
            try:
                descriptor = os.pread(fd, 2048, 32768)
            finally:
                os.close(fd)
        except OSError as exc:
            _log.warning("Cannot read the disc volume label from %s: %s", self.device, exc)
            return ""
        return _pvd_label(descriptor)

    def mediainfo(self, retries: int = 6, retry_delay: float = 5.0) -> DiscInfo:
        """
        Input:  retries     - number of extra attempts if the drive reports no media
                retry_delay - seconds to wait between attempts
        Output: DiscInfo
        Details:
            Runs dvd+rw-mediainfo and parses session count and capacity.
            Reads the label from the primary volume descriptor.
            Retries when the drive reports "no media". That happens right
            after a burn while the drive reloads the tray (often 5 to 30
            seconds).

            This class calls dvd+rw-mediainfo rather than xorriso -toc
            because growisofs formats BD-R for Pseudo OverWrite.
            xorriso reports such a medium as "is unsuitable" and
            prints no session count, so a BD-R holding one session
            parsed as zero sessions. That would let init treat a
            written archive as blank and overwrite it.
            dvd+rw-mediainfo reports the same disc correctly as
            "appendable" with "Number of Sessions: 1". Confirmed on
            real M-DISC BD-R hardware.

            dvd+rw-mediainfo refuses CD with "non-DVD media mounted". That
            error is the CD detector: it raises the unsupported-media
            error instead of a raw tool failure.
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
                info = _parse_mediainfo(stdout)
                info.label = self._read_label()
                return info
            last_err = stderr
            if not _retry_mediainfo_error(
                stderr, attempt, retries, retry_delay, self.device,
            ):
                break
        raise RuntimeError(f"dvd+rw-mediainfo failed: {last_err}")

    def list_session_numbers(self) -> list[int]:
        """
        Input:  None
        Output: session numbers in the disc root
        Details:
            One `xorriso -ls /` call lists the root names without
            extracting files. xorriso quotes each name. The result is
            cached on the backend so one command pays for one listing.
            A burn or a disc change calls invalidate_session_list().
            A failure retries once, then raises RuntimeError. This
            backend never falls back to probing 3000 control paths.
        """
        if self._session_numbers_cache is not None:
            return list(self._session_numbers_cache)
        last_err = ""
        for _attempt in range(2):
            result = subprocess.run(
                ["xorriso", "-indev", self.device, "-ls", "/"],
                capture_output=True,
            )
            if result.returncode == 0:
                entries = result.stdout.decode("utf-8", errors="replace").splitlines()
                numbers = sorted(
                    int(match.group(1))
                    for line in entries
                    if (match := re.fullmatch(r"'session_(\d{3})'", line.strip()))
                )
                self._session_numbers_cache = numbers
                return numbers
            last_err = result.stderr.decode("utf-8", errors="replace").strip()
            _log.warning(
                "Cannot list sessions on %s (attempt %d): %s",
                self.device, _attempt + 1, last_err,
            )
        raise RuntimeError(
            f"Cannot list sessions on {self.device} after 2 attempts: {last_err}"
        )

    def invalidate_session_list(self) -> None:
        """
        Input:  None
        Output: None
        Details: Clears the cached root listing so the next call lists fresh.
        """
        self._session_numbers_cache = None

    def _read_xorriso_attempt(
        self, unique_paths: list[str],
    ) -> tuple[dict[str, bytes], str, bool]:
        """
        Input:  unique_paths - validated disc paths to extract
        Output: found bytes, xorriso stderr, and whether reading is complete
        """
        with tempfile.TemporaryDirectory(prefix="oddarchiver_read_") as tmp:
            tmp_path = Path(tmp)
            dests: list[tuple[str, Path]] = []
            cmd = [
                "xorriso", "-indev", self.device,
                "-osirrox", "on",
                "-abort_on", "NEVER",
            ]
            for i, path in enumerate(unique_paths):
                dest = tmp_path / f"{i:04d}"
                cmd.extend(["-extract", "/" + path, str(dest)])
                dests.append((path, dest))
            result = subprocess.run(cmd, capture_output=True)
            stderr = result.stderr.decode("utf-8", errors="replace")
            found: dict[str, bytes] = {}
            for path, dest in dests:
                if dest.is_file():
                    size = dest.stat().st_size
                    cap = _control_file_cap(path)
                    if cap is not None and size > cap:
                        raise RuntimeError(f"Control file exceeds size limit: {path}")
                    found[path] = dest.read_bytes()
            return found, stderr, bool(found) or "Cannot determine attributes of" in stderr

    def read_paths(
        self,
        paths: Sequence[str],
        retries: int = 6,
        retry_delay: float = 5.0,
    ) -> dict[str, bytes]:
        """
        Input:  paths       - relative paths on the disc filesystem
                retries     - extra attempts on a transient drive-not-ready error
                retry_delay - seconds to wait between attempts
        Output: dict mapping each found path to its raw bytes
        Details:
            Extracts every requested file in one
            `xorriso -indev DEVICE -osirrox on` invocation with multiple
            `-extract src dest` pairs, so the backend parses the ISO
            volume descriptor once. `-abort_on NEVER` keeps a missing
            member from aborting the rest of the batch. Omitted paths
            stay absent from the result.

            Drive-not-ready and busy-device retries match the previous
            single-file read_path() behavior.

            Raises ValueError if any path is not a known-safe blob or
            manifest path. Raises RuntimeError if the backend still
            cannot read the drive/medium after retries.
        """
        from oddarchiver.manifest import validate_disc_read_path

        unique_paths = list(dict.fromkeys(paths))
        for path in unique_paths:
            validate_disc_read_path(path)
        if not unique_paths:
            return {}

        last_err = ""
        for attempt in range(retries + 1):
            found, stderr, complete = self._read_xorriso_attempt(unique_paths)
            if complete:
                return found
            last_err = next(
                (line for line in reversed(stderr.strip().splitlines()) if line.strip()),
                "unknown xorriso failure",
            )
            if not _retry_xorriso_read(self.device, stderr, attempt, retries, retry_delay):
                break
        raise RuntimeError(
            f"Failed to read {len(unique_paths)} path(s) from {self.device} "
            f"via xorriso: {last_err}"
        )

    def read_path(self, path: str, retries: int = 6, retry_delay: float = 5.0) -> bytes:
        """
        Input:  path        - relative path on the disc filesystem
                retries     - extra attempts on a transient drive-not-ready error
                retry_delay - seconds to wait between attempts
        Output: bytes
        Details:
            One-item wrapper over read_paths(). Raises FileNotFoundError
            if the path is not present in an otherwise-readable ISO tree.
        """
        found = self.read_paths([path], retries=retries, retry_delay=retry_delay)
        if path not in found:
            raise FileNotFoundError(f"Path not found on disc: {path!r}")
        return found[path]

    def _extract_xorriso_attempt(self, path: str, dest: Path) -> tuple[bool, str]:
        """
        Input:  path - relative path on the disc filesystem
                dest - destination file path
        Output: whether dest was extracted, and xorriso stderr
        """
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            _log.exception("Could not remove stale extract dest %s", dest)
        cmd = [
            "xorriso", "-indev", self.device,
            "-osirrox", "on",
            "-abort_on", "NEVER",
            "-extract", "/" + path, str(dest),
        ]
        result = subprocess.run(cmd, capture_output=True)
        stderr = result.stderr.decode("utf-8", errors="replace")
        if dest.exists() and dest.is_file():
            return True, stderr
        if "Cannot determine attributes of" in stderr:
            raise FileNotFoundError(f"Path not found on disc: {path!r}")
        return False, stderr

    def extract_path_to_file(
        self, path: str, dest: Path, retries: int = 6, retry_delay: float = 5.0,
    ) -> None:
        """
        Input:  path        - relative path on the disc filesystem
                dest        - destination file path
                retries     - extra attempts on a transient drive-not-ready error
                retry_delay - seconds to wait between attempts
        Output: None (writes dest)
        Details:
            One `xorriso -extract` straight to dest. Drive-not-ready and
            busy-device retries match read_paths(). Missing path raises
            FileNotFoundError. Exhausted drive failure raises RuntimeError.
        """
        from oddarchiver.manifest import validate_disc_read_path

        validate_disc_read_path(path)
        last_err = ""
        for attempt in range(retries + 1):
            extracted, stderr = self._extract_xorriso_attempt(path, dest)
            if extracted:
                return
            last_err = next(
                (line for line in reversed(stderr.strip().splitlines()) if line.strip()),
                "unknown xorriso failure",
            )
            if not _retry_xorriso_read(self.device, stderr, attempt, retries, retry_delay):
                break
        raise RuntimeError(
            f"Failed to read 1 path(s) from {self.device} "
            f"via xorriso: {last_err}"
        )

    def _paths_within_disc_budget(
        self, unique_paths: list[str], max_bytes: int,
    ) -> list[str]:
        """
        Input:  unique_paths - requested paths in extraction order
                max_bytes - maximum total listed bytes
        Output: listed paths that fit the budget
        """
        cmd = [
            "xorriso", "-indev", self.device,
            "-find", "/", "-type", "f", "-exec", "report_lba", "--",
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Cannot list disc file sizes: {error}")
        sizes: dict[str, int] = {}
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            match = re.fullmatch(
                r"File data lba:\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,"
                r"\s*(\d+)\s*,\s*'(/[^']+)'", line,
            )
            if match:
                sizes[match.group(2)[1:]] = int(match.group(1))
        written = 0
        for path in unique_paths:
            size = sizes.get(path)
            if size is None:
                continue
            if written + size > max_bytes:
                raise RuntimeError("Not enough local space to extract archive blobs.")
            written += size
        return [path for path in unique_paths if path in sizes]

    def extract_paths(
        self,
        paths: Sequence[str],
        dest_root: Path,
        max_bytes: int | None = None,
        retries: int = 6,
        retry_delay: float = 5.0,
    ) -> int:
        """
        Input:  paths     - relative paths on the disc filesystem
                dest_root - local directory that receives the files
                max_bytes - optional cap on total bytes written
                retries, retry_delay - drive-not-ready policy
        Output: total bytes written
        Details:
            When max_bytes is set, one xorriso listing checks the real
            file sizes before extraction. A batch above the cap is
            refused before the backend writes a scratch file.
        """
        from oddarchiver.manifest import validate_disc_read_path

        unique_paths = list(dict.fromkeys(paths))
        for path in unique_paths:
            validate_disc_read_path(path)
        if not unique_paths:
            return 0
        if max_bytes is not None:
            unique_paths = self._paths_within_disc_budget(unique_paths, max_bytes)
            if not unique_paths:
                return 0
        dest_root = Path(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="oddarchiver_extract_", dir=str(dest_root),
        ) as scratch_name:
            scratch = Path(scratch_name)
            self._xorriso_extract_all(unique_paths, scratch, retries, retry_delay)
            return _publish_extracted_paths(unique_paths, scratch, dest_root, max_bytes)

    def _xorriso_extract_attempt(
        self, paths: list[str], scratch: Path,
    ) -> tuple[bool, str]:
        """
        Input: paths and scratch root
        Output: whether extraction is complete and xorriso stderr
        """
        dests: list[tuple[str, Path]] = []
        cmd = [
            "xorriso", "-indev", self.device,
            "-osirrox", "on",
            "-abort_on", "NEVER",
        ]
        for path in paths:
            dest = scratch / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            cmd.extend(["-extract", "/" + path, str(dest)])
            dests.append((path, dest))
        result = subprocess.run(cmd, capture_output=True)
        stderr = result.stderr.decode("utf-8", errors="replace")
        if any(dest.is_file() for _path, dest in dests):
            return True, stderr
        if "Cannot determine attributes of" in stderr:
            return True, stderr
        return False, stderr

    def _xorriso_extract_all(
        self,
        paths: list[str],
        scratch: Path,
        retries: int,
        retry_delay: float,
    ) -> None:
        """
        Input: disc paths, scratch root, retry policy
        Output: None
        Details: One xorriso call for the whole batch. Missing paths stay absent.
        """
        last_err = ""
        for attempt in range(retries + 1):
            complete, stderr = self._xorriso_extract_attempt(paths, scratch)
            if complete:
                return
            last_err = next(
                (line for line in reversed(stderr.strip().splitlines()) if line.strip()),
                "unknown xorriso failure",
            )
            if not _retry_xorriso_read(self.device, stderr, attempt, retries, retry_delay):
                break
        raise RuntimeError(
            f"Failed to read {len(paths)} path(s) from {self.device} "
            f"via xorriso: {last_err}"
        )


    def _guard(
        self,
        expected: int,
        expected_archive_id: str | None = None,
        expected_volume_index: int | None = None,
    ) -> None:
        """
        Input:  expected               - archive session count recorded at
                                        start of run
                expected_archive_id    - archive_id to continue, or None to skip
                expected_volume_index  - volume_index to continue, or None
        Output: None
        Details:
            When the caller sets expected_archive_id, re-reads volume.json and
            refuses a mismatched or missing marker.

            Then compares the archive session count (VolumeMarker.first_session plus the
            sessions on this volume) to expected. MMC session_count is
            not that number: BD-R SRM+POW reports 1 after the first
            burn, because growisofs appends tracks inside one MMC
            session.

            expected == 0 is blank-disc init. A blank disc has no
            marker and no manifests, so this path does not scan
            control files. It uses mediainfo().session_count, which is
            0 on empty media and becomes 1 after the first session.
            That call also refuses CD media. The BD-R stuck-at-1
            failure only starts after that first session.

            expected != 0 still calls mediainfo() so this tool refuses
            CD media, then re-reads the archive session count.
        """
        if expected_archive_id is not None:
            check_volume_identity(
                self, expected_archive_id, expected_volume_index,
            )
        info = self.mediainfo()
        if expected == 0:
            if info.session_count != 0:
                raise RuntimeError(
                    f"Double-burn guard: expected 0 archive sessions, "
                    f"found session_count {info.session_count}; "
                    f"aborting to prevent duplicate session"
                )
            return
        current = archive_session_count(self)
        if current != expected:
            raise RuntimeError(
                f"Double-burn guard: expected {expected} archive sessions, "
                f"found {current}; aborting to prevent duplicate session"
            )


def _recover_publish_item(
    entry: Path, match: re.Match[str], sessions_root: Path,
) -> None:
    """
    Input: interrupted publish entry, match, and sessions root
    Output: None
    """
    from oddarchiver.session import remove_tree

    target = sessions_root / match.group(1)
    is_tree = entry.is_dir() and not entry.is_symlink()
    if match.group(2) == "prior" and is_tree and not os.path.lexists(target):
        _log.warning(
            "Restoring %s from interrupted ISO publish item %s",
            target, entry,
        )
        os.replace(entry, target)
        return
    _log.warning("Removing stale ISO publish item %s", entry)
    if is_tree:
        if not remove_tree(entry, "stale ISO publish directory"):
            raise RuntimeError(
                f"Cannot remove stale ISO publish directory: {entry}"
            )
    else:
        entry.unlink()


def _copy_iso_blob(src: Path, dest: Path, size: int, path: str) -> None:
    """
    Input:  src  - source blob in the ISO sessions tree
            dest - destination file path
            size - expected source size in bytes
            path - relative disc path, for logging
    Output: None (writes dest atomically with mode 0600)
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=dest.name + ".", suffix=".tmp", dir=str(dest.parent),
    )
    tmp = Path(name)
    owned = True
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as outf, src.open("rb") as inf:
            owned = False
            shutil.copyfileobj(inf, outf)
        if tmp.stat().st_size != size:
            raise RuntimeError(f"Extracted blob size differs: {tmp}")
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    except (OSError, RuntimeError):
        _log.exception("Failed to extract %s to %s", path, dest)
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if owned:
            os.close(fd)


class ISOBackend(BurnBackend):
    """
    Input:  iso_path  - filesystem path for the ISO file
            disc_size - simulated capacity in bytes (default 23 GiB for 25 GB BD-R)
    Output: N/A (class)
    Details:
        Wraps genisoimage to build UDF ISOs for --test-iso mode and CI.
        Sessions accumulate in a sibling directory that replaces the .iso
        suffix with .d. The backend rebuilds the ISO from that tree on
        every write. read_paths()/read_path() read from that directory.
        The code needs no loop mount. _copy_staging publishes each
        session with one directory rename, so a killed write leaves no
        partial session_NNN directory. The next write removes the
        leftover temp directories.
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
        self._session_numbers_cache: list[int] | None = None

    # --- public interface ---

    def init(
        self,
        staging: Path,
        label: str,
        expected_session_count: int = 0,
        expected_archive_id: str | None = None,
        expected_volume_index: int | None = None,
    ) -> None:
        """
        Input:  staging, label, expected_session_count,
                expected_archive_id, expected_volume_index
        Output: None
        Details:
            Removes leftover publish temp items, then copies staging
            contents into sessions_root and builds a new UDF ISO.
        """
        self._recover_interrupted_publish()
        self._guard(
            expected_session_count,
            expected_archive_id=expected_archive_id,
            expected_volume_index=expected_volume_index,
        )
        self.invalidate_session_list()
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        _copy_staging(staging, self._sessions_root)
        self._build_iso(label)
        self.invalidate_session_list()

    def append(
        self,
        staging: Path,
        label: str,
        expected_session_count: int,
        expected_archive_id: str | None = None,
        expected_volume_index: int | None = None,
    ) -> None:
        """
        Input:  staging, label, expected_session_count,
                expected_archive_id, expected_volume_index
        Output: None
        Details:
            Removes leftover publish temp items, then adds a new session
            to sessions_root and rebuilds the ISO from all sessions.
        """
        self._recover_interrupted_publish()
        self._guard(
            expected_session_count,
            expected_archive_id=expected_archive_id,
            expected_volume_index=expected_volume_index,
        )
        self.invalidate_session_list()
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        _copy_staging(staging, self._sessions_root)
        self._build_iso(label)
        self.invalidate_session_list()

    def prefill(self, prefill_bytes: int) -> None:
        """
        Input:  prefill_bytes - bytes to simulate as already used
        Output: None
        Details:
            Records simulated used space in a metadata file so mediainfo()
            reports reduced remaining_bytes. The backend writes no real
            data to disc. Used with --prefill to test capacity warning paths.
        """
        self._meta_path.write_text(json.dumps({"prefill_bytes": prefill_bytes}))

    def mediainfo(self) -> DiscInfo:
        """
        Input:  None
        Output: DiscInfo
        Details:
            Counts session_NNN directories in sessions_root, measures ISO
            size, adds prefill_bytes from metadata, and computes remaining
            capacity. Publish temp directories do not match session_NNN,
            so they do not count.
        """
        if not self._sessions_root.exists():
            return DiscInfo(
                session_count=0,
                remaining_bytes=self.disc_size,
                used_bytes=0,
                label="",
            )
        numbers = self._scan_session_numbers()
        session_count = len(numbers)
        iso_bytes = self.iso_path.stat().st_size if self.iso_path.exists() else 0
        prefill_bytes = self._read_prefill()
        used_bytes = iso_bytes + prefill_bytes
        remaining_bytes = max(0, self.disc_size - used_bytes)
        label = self._read_label(
            [self._sessions_root / f"session_{n:03d}" for n in numbers]
        )
        return DiscInfo(
            session_count=session_count,
            remaining_bytes=remaining_bytes,
            used_bytes=used_bytes,
            label=label,
        )

    def list_session_numbers(self) -> list[int]:
        """
        Input:  None
        Output: session numbers in the ISO staging root
        Details:
            Lists only root directories, without reading file contents.
            The result is cached so one command lists once. A burn
            clears the cache.
        """
        if self._session_numbers_cache is not None:
            return list(self._session_numbers_cache)
        numbers = self._scan_session_numbers()
        self._session_numbers_cache = numbers
        return numbers

    def invalidate_session_list(self) -> None:
        """
        Input:  None
        Output: None
        Details: Clears the cached session tree listing after a write.
        """
        self._session_numbers_cache = None

    def read_paths(self, paths: Sequence[str]) -> dict[str, bytes]:
        """
        Input:  paths - relative paths within the disc filesystem
        Output: dict mapping each found path to its raw bytes
        Details:
            Reads from the sessions_root directory tree. The function
            omits missing paths. Raises ValueError if a path is not a
            known-safe blob or manifest path.
        """
        from oddarchiver.manifest import validate_disc_read_path

        found: dict[str, bytes] = {}
        for path in paths:
            validate_disc_read_path(path)
            target = self._sessions_root / path
            if target.is_file():
                size = target.stat().st_size
                cap = _control_file_cap(path)
                if cap is not None and size > cap:
                    raise RuntimeError(f"Control file exceeds size limit: {path}")
                found[path] = target.read_bytes()
        return found

    def read_path(self, path: str) -> bytes:
        """
        Input:  path - relative path within the disc filesystem
        Output: bytes
        Details:
            One-item wrapper over read_paths().
            Raises ValueError if path is not a known-safe blob or manifest path.
        """
        found = self.read_paths([path])
        if path not in found:
            raise FileNotFoundError(f"Path not found in ISO sessions: {path!r}")
        return found[path]

    def extract_path_to_file(self, path: str, dest: Path) -> None:
        """
        Input:  path - relative path within the disc filesystem
                dest - destination file path
        Output: None (writes dest)
        Details:
            Copies from the sessions_root tree. Raises FileNotFoundError
            if the source does not exist. Raises ValueError if path is
            not a known-safe blob or manifest path.
        """
        from oddarchiver.manifest import validate_disc_read_path

        validate_disc_read_path(path)
        src = self._sessions_root / path
        if not src.is_file():
            raise FileNotFoundError(f"Path not found in ISO sessions: {path!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def extract_paths(
        self,
        paths: Sequence[str],
        dest_root: Path,
        max_bytes: int | None = None,
    ) -> int:
        """
        Input:  paths     - relative paths within the disc filesystem
                dest_root - local directory that receives the files
                max_bytes - optional cap on total bytes written
        Output: total bytes written
        Details:
            Copy from sessions_root. Check the source size before the copy
            so an understated manifest cannot pass the budget.
        """
        from oddarchiver.manifest import validate_disc_read_path

        written = 0
        dest_root = Path(dest_root)
        for path in dict.fromkeys(paths):
            validate_disc_read_path(path)
            src = self._sessions_root / path
            if not src.is_file():
                continue
            size = src.stat().st_size
            if max_bytes is not None and written + size > max_bytes:
                raise RuntimeError(
                    "Not enough local space to extract archive blobs."
                )
            _copy_iso_blob(src, dest_root / path, size, path)
            written += size
        return written


    # --- private helpers ---

    def _scan_session_numbers(self) -> list[int]:
        """
        Input:  None
        Output: sorted session numbers of the session_NNN directories
        Details:
            Lists the sessions_root directory with no cache. Only
            directories whose full name matches session_NNN count.
        """
        if not self._sessions_root.exists():
            return []
        return sorted(
            int(match.group(1))
            for entry in self._sessions_root.iterdir() if entry.is_dir()
            if (match := _SESSION_DIR_RE.fullmatch(entry.name))
        )

    def _recover_interrupted_publish(self) -> None:
        """
        Input:  None
        Output: None
        Details:
            Runs at the start of each write. A killed _copy_staging can
            leave ".<name>.partial-*" temp items and, during a swap, a
            ".<name>.prior-*" directory. This function removes each
            partial item. When <name> is missing, it moves the prior
            directory back to <name>. Otherwise it removes the prior
            directory. Each action logs a warning. A failed removal
            raises RuntimeError, so the leftover never goes into the ISO.
        """

        if not self._sessions_root.is_dir():
            return
        for entry in sorted(self._sessions_root.iterdir()):
            match = _PUBLISH_TEMP_RE.fullmatch(entry.name)
            if match is None:
                continue
            _recover_publish_item(entry, match, self._sessions_root)
        self.invalidate_session_list()

    def _guard(
        self,
        expected: int,
        expected_archive_id: str | None = None,
        expected_volume_index: int | None = None,
    ) -> None:
        """
        Input:  expected               - archive session count recorded at
                                        start of run
                expected_archive_id    - archive_id to continue, or None to skip
                expected_volume_index  - volume_index to continue, or None
        Output: None
        Details:
            When the caller sets expected_archive_id, re-reads volume.json and
            refuses a mismatched or missing marker.

            Then compares the archive session count (VolumeMarker.first_session plus the
            sessions on this volume) to expected. MMC session_count is
            not that number: BD-R SRM+POW reports 1 after the first
            burn, because growisofs appends tracks inside one MMC
            session.

            expected == 0 is blank-disc init. A blank disc has no
            marker and no session_* directories, so this path does
            not scan control files. It uses mediainfo().session_count,
            which is 0 when the session tree is empty and becomes 1
            after the first session. The BD-R stuck-at-1 failure only
            starts after that first session.

            ISOBackend.session_count itself does increment per archive
            session (it counts session_* directories), but callers
            pass the archive session count, and a BD-R-like wrapper
            can lie about mediainfo().session_count, so this guard
            still uses archive_session_count when expected != 0.
        """
        if expected_archive_id is not None:
            check_volume_identity(
                self, expected_archive_id, expected_volume_index,
            )
        info = self.mediainfo()
        if expected == 0:
            if info.session_count != 0:
                raise RuntimeError(
                    f"Double-burn guard: expected 0 archive sessions, "
                    f"found session_count {info.session_count}; "
                    f"aborting to prevent duplicate session"
                )
            return
        current = archive_session_count(self)
        if current != expected:
            raise RuntimeError(
                f"Double-burn guard: expected {expected} archive sessions, "
                f"found {current}; aborting to prevent duplicate session"
            )

    def _build_iso(self, label: str) -> None:
        """
        Input:  label - UDF volume label (truncated to 32 chars per UDF spec)
        Output: None
        Details:
            Calls genisoimage to build a UDF ISO from sessions_root.
            genisoimage needs -allow-limited-size alongside -udf.
            Verified directly: genisoimage still refuses a 4 GiB+ file
            with "-allow-limited-size was not specified" in UDF mode
            without it, so -udf alone does not suffice. Writes to a
            .tmp file then renames to avoid a partial ISO.
        """
        tmp = self.iso_path.with_suffix(".tmp")
        cmd = [
            "genisoimage",
            "-udf", "-allow-limited-size", "-R",
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
        Input:  sessions - sorted list of session_NNN directories (unused)
        Output: str - ISO volume identifier from the image, or ""
        Details:
            Reads the volume identifier from the primary volume
            descriptor at offset 32768 + 40 (32 bytes). The label lives
            in the image, not in a plaintext manifest.json, so an
            encrypted archive still shows its label. No image yet is ""
            and a short or unreadable image logs a warning and returns "".
        """
        if not self.iso_path.exists():
            return ""
        try:
            with self.iso_path.open("rb") as handle:
                handle.seek(32768)
                descriptor = handle.read(2048)
        except OSError as exc:
            _log.warning("Cannot read the ISO volume label from %s: %s", self.iso_path, exc)
            return ""
        return _pvd_label(descriptor)


def _pvd_label(descriptor: bytes) -> str:
    """
    Input:  descriptor - 2048-byte ISO9660 primary volume descriptor
    Output: str - ASCII volume identifier, or "" for invalid data
    Details:
        Checks the descriptor type and identifier before reading the label.
    """
    if len(descriptor) != 2048 or descriptor[0] != 1 or descriptor[1:6] != b"CD001":
        _log.warning("Cannot read ISO volume label: invalid primary volume descriptor")
        return ""
    raw = descriptor[40:72]
    if not raw.isascii():
        _log.warning("Cannot read ISO volume label: non-ASCII volume identifier")
        return ""
    return raw.decode("ascii").strip()


# --- module-level helpers ---

# The growisofs progress line, straight from the growisofs usage strings:
# "%11lld/%lld (%4.1f%%) @%.1fx, remaining %d:%02d RBU %5.1f%% UBU %5.1f%%"
# (or "@0x, remaining ??:??" when speed/ETA are not yet known).
_GROWISOFS_PROGRESS_RE = re.compile(
    r"^\s*(\d+)/(\d+)\s+\(\s*([\d.]+)%\)\s+@([^,]+),\s*remaining\s+(\S+):(\S+)"
)
# The build-phase update from the mkisofs/xorriso child process, e.g.
# "xorriso : UPDATE :       8 files added in 1 seconds".
_XORRISO_FILES_ADDED_RE = re.compile(r"UPDATE\s*:\s*(\d+)\s+files added")

# The percentage-based progress line from xorriso for long-running scans/writes
# on a large tree, e.g.
# "xorriso : UPDATE : 62.29% done, estimate finish Sun Apr 09 10:35:29 2023".
# It repeats once per ~0.15-0.2% on a large real archive and floods the
# terminal with one new line per update if left to print through raw.
_XORRISO_PERCENT_DONE_RE = re.compile(
    r"UPDATE\s*:\s*([\d.]+)%\s+done,\s*estimate finish\s+(.+)"
)
# genisoimage prints the same percentage-based progress line for this
# phase, when growisofs shells out to it via MKISOFS=genisoimage
# (forced unconditionally in _run_growisofs, because genisoimage, not
# xorriso, handles a 4 GiB+ file). genisoimage prints the bare line
# with no "UPDATE :" tag, e.g.
# "  0.67% done, estimate finish Mon Sep 21 11:15:49 2026". Anchored
# at the start of the line (after optional whitespace) so it can
# never match the xorriso line above, which always starts with
# "xorriso : UPDATE :". Without this, every line falls through to
# the raw-print path and floods the terminal with one new line per
# ~0.15-0.2%, the same complaint that motivated the xorriso regex
# above, on the tool that this file actually invokes.
_GENISOIMAGE_PERCENT_DONE_RE = re.compile(
    r"^\s*([\d.]+)%\s+done,\s*estimate finish\s+(.+)$"
)
_XORRISO_MEDIA_BLOCKS_RE = re.compile(
    r"Media blocks\s*:\s*([\d.]+)\s+\S+\s*,\s*([\d.]+)\s+\S+\s*,\s*([\d.]+)\s+overall",
    re.IGNORECASE,
)
_XORRISO_MEDIA_SUMMARY_SESSIONS_RE = re.compile(
    r"Media summary:\s*(\d+)\s+session",
    re.IGNORECASE,
)
_XORRISO_MEDIA_STATUS_RE = re.compile(
    r"Media status\s*:\s*(.+)",
    re.IGNORECASE,
)
_XORRISO_MEDIA_CURRENT_RE = re.compile(
    r"Media current:\s*(.+)",
    re.IGNORECASE,
)
_XORRISO_VOLUME_ID_QUOTED_RE = re.compile(
    r"Volume id\s*:\s*'([^']*)'",
    re.IGNORECASE,
)
_XORRISO_ISO_SESSION_VOLID_RE = re.compile(
    r"ISO session\s*:\s*\d+\s*,.*,\s*(\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_XORRISO_PROFILE_HEX_RE = re.compile(r"\(([0-9A-Fa-f]{4})h\)")



def _display_burn_progress(line: str) -> bool:
    """
    Input: output line
    Output: whether the line was rendered as progress
    """
    from oddarchiver.session import _print_bar

    progress = _GROWISOFS_PROGRESS_RE.search(line)
    if progress:
        written, total, pct, speed, mins, secs = progress.groups()
        _print_bar(
            int(written), int(total),
            suffix=f"{pct}% @{speed}, {mins}:{secs} remaining",
        )
        return True
    added = _XORRISO_FILES_ADDED_RE.search(line)
    if added:
        print(f"\r  Building ISO image: {added.group(1)} files added...", end="", flush=True)
        return True
    percent_done = _XORRISO_PERCENT_DONE_RE.search(line)
    if percent_done:
        pct_str, finish = percent_done.groups()
        _print_bar(
            round(float(pct_str)), 100,
            suffix=f"estimate finish {finish.strip()}",
        )
        return True
    genisoimage_percent_done = _GENISOIMAGE_PERCENT_DONE_RE.search(line)
    if genisoimage_percent_done:
        pct_str, finish = genisoimage_percent_done.groups()
        _print_bar(
            round(float(pct_str)), 100,
            suffix=f"estimate finish {finish.strip()}",
        )
        return True
    return False


def _log_burn_signal(cmd: list[str], returncode: int) -> None:
    """
    Input: command and process return code
    Output: None
    """
    if returncode < 0:
        _log.warning(
            "%s terminated by signal %d (returncode %d)",
            cmd[0] if cmd else "burn command",
            -returncode, returncode,
        )


def _run_burn_cmd(
    cmd: list[str], env: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    """
    Input:  cmd - full writer argv (growisofs or xorriso)
            env - optional child environment. None inherits the parent
    Output: (returncode, output_lines) - process return code and every raw
            line printed (stdout+stderr merged, one entry per line, in
            order), so callers can recognize a known failure signature.
    Details:
        Streams the writer live instead of letting it dump raw output.
        growisofs repeats a
        "<written>/<total> (<pct>%) @<speed>x, remaining M:SS ..." line
        very frequently during the actual write.

        The mkisofs child it shells out to (genisoimage in this
        project, forced by MKISOFS=genisoimage. Xorriso if invoked
        directly) repeats either
        "<n> files added in <n> seconds" for a moderate source tree or
        "<pct>% done, estimate finish <timestamp>" for a large one.
        genisoimage prints that last line bare, and xorriso tags it
        "UPDATE :". The function matches both forms. It re-renders
        recognized progress lines as a single in-place updating line.
        Anything else prints through unchanged so nothing real is
        ever swallowed.
    """

    popen_kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
    }
    if env is not None:
        popen_kwargs["env"] = env
    proc = subprocess.Popen(cmd, **popen_kwargs)
    bar_active = False
    output_lines: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            output_lines.append(line)
            if _display_burn_progress(line):
                bar_active = True
                continue
            if bar_active:
                print()
                bar_active = False
            print(line, flush=True)
    finally:
        if bar_active:
            print()
    returncode = proc.wait()
    _log_burn_signal(cmd, returncode)
    return returncode, output_lines


def _run_growisofs(cmd: list[str]) -> tuple[int, list[str]]:
    """
    Input:  cmd - full growisofs argv
    Output: (returncode, output_lines) - process return code and every raw
            line growisofs printed (stdout+stderr merged, one entry per
            line, in order), so callers can recognize a specific known
            failure signature in the text growisofs already printed.
    Details:
        Streams growisofs through _run_burn_cmd. Forces
        MKISOFS=genisoimage in the child environment. growisofs looks
        for a binary literally named 'mkisofs' unless overridden.

        On a system where that name resolves to the
        mkisofs-emulation layer of xorriso (a common packaging
        choice), that layer accepts neither -udf nor
        -allow-limited-size ("Unsupported option"), so any file 4 GiB
        or larger fails the ISO9660 size field regardless of the -udf
        flag passed on this cmd. genisoimage is a real, separate
        mkisofs-family binary that honors both flags and represents
        such a file as UDF-only, verified directly against this
        failure signature before shipping this fix.
    """
    env = {**os.environ, "MKISOFS": "genisoimage"}
    return _run_burn_cmd(cmd, env=env)


def _is_hardware_fault(output_lines: list[str]) -> bool:
    """
    Input:  output_lines - captured growisofs stdout/stderr lines
    Output: bool - True if output matches a SCSI/power-reset fault
    Details:
        Matches the Power on / bus device reset / SCSI Unit Attention
        class of hardware-fault signatures. Does not match the
        stale-kernel-cache pread64 failure, which append() handles
        separately.
    """
    blob = "\n".join(output_lines).lower()
    return any(sig in blob for sig in _HW_FAULT_SIGNATURES)


def _is_disc_full_signature(output_lines: list[str]) -> bool:
    """
    Input:  output_lines - captured growisofs stdout/stderr lines
    Output: bool - True if output matches growisofs disc-full text
    Details:
        growisofs prints `"<free> blocks are free, <needed> to be
        written"` (optional trailing "!") then `write failed: No space
        left on device`. Requires all three phrases so a lone ENOSPC
        line does not match.
    """
    blob = "\n".join(output_lines).lower()
    return (
        "blocks are free" in blob
        and "to be written" in blob
        and "no space left on device" in blob
    )



def archive_session_count(
    backend: BurnBackend,
    listed: list[int] | None = None,
    first_session: int | None = None,
) -> int:
    """
    Input:  backend       - BurnBackend to read the current volume from
            listed        - session numbers the caller already knows
            first_session - first session on this volume, if the caller knows it
    Output: int - first_session plus the number of archive sessions on
            this volume
    Details:
        Authoritative counter for the double-burn guard. MMC
        session_count is not this number. BD-R SRM+POW reports 1 after
        the first burn, because growisofs appends each new archive
        session as a track inside one MMC session.

        When the caller passes a session listing, the count is the
        listing size plus the first session, with no control-file read.
        Without a listing, the backend lists once (cached per command).
        A backend that cannot list falls back to loading session
        manifests from first_session.

        A missing volume marker treats first_session as 0 (legacy or
        single-volume).
    """
    from oddarchiver.volume import read_volume_marker
    from oddarchiver.manifest import load_session_manifests

    if listed is None:
        listed = backend.list_session_numbers()
    if listed is not None:
        if first_session is None:
            first_session = min(listed, default=0)
        return first_session + len(listed)
    marker = read_volume_marker(backend)
    first = first_session if first_session is not None else (
        marker.first_session if marker else 0
    )
    return first + len(load_session_manifests(
        backend, start_session=first, marker=marker,
    ))


def check_volume_identity(
    backend: BurnBackend,
    expected_archive_id: str,
    expected_volume_index: int | None = None,
) -> None:
    """
    Input:  backend                - backend to re-read volume.json from
            expected_archive_id    - archive_id the caller intends to continue
            expected_volume_index  - volume_index, or None to skip that check
    Output: None
    Details:
        Reads volume.json immediately before a burn. Raises RuntimeError
        that names expected vs found identity when they differ, or when
        the marker does not exist. Callers with no expected identity
        skip this function, so blank-disc init does not pay an extra
        disc read.
    """
    from oddarchiver.volume import read_volume_marker

    marker = read_volume_marker(backend)
    vol_bit = ""
    if expected_volume_index is not None:
        vol_bit = f" volume_index {expected_volume_index}"
    if marker is None:
        raise RuntimeError(
            f"wrong disc inserted: expected archive_id {expected_archive_id}"
            f"{vol_bit}, found no volume marker"
        )
    if marker.archive_id != expected_archive_id:
        raise RuntimeError(
            f"wrong disc inserted: expected archive_id {expected_archive_id}, "
            f"found {marker.archive_id}"
        )
    if (
        expected_volume_index is not None
        and marker.volume_index != expected_volume_index
    ):
        raise RuntimeError(
            f"wrong disc inserted: expected archive_id {expected_archive_id} "
            f"volume_index {expected_volume_index}, found archive_id "
            f"{marker.archive_id} volume_index {marker.volume_index}"
        )


def _session_n_from_staging(staging: Path) -> int | None:
    """
    Input:  staging - directory that should contain session_NNN/
    Output: int session index, or None if no session_* dir is present
    Details:
        Uses the highest session_* directory name. Tests that pass a
        bare tmp path without a session dir get None.
    """
    names = sorted(
        p.name for p in staging.glob("session_*") if p.is_dir()
    )
    if not names:
        return None
    try:
        return int(names[-1].split("_", 1)[1])
    except (IndexError, ValueError):
        _log.warning("Could not parse session number from %s", names[-1])
        return None


def resolve_usb_device_sysfs_path(
    device: str, sys_class_block: Path = Path("/sys/class/block"),
) -> Path | None:
    """
    Input:  device         - block device path, e.g. "/dev/sr0"
            sys_class_block - root of the sysfs block-class tree. Override
                              in tests to point at a fake tree
    Output: Path to the USB sysfs directory of the device (the one
            whose "authorized" attribute controls whether the kernel
            treats it as plugged in), or None if resolution fails
            (not a USB device, or the block device does not exist).
    Details:
        Walks <sys_class_block>/<name>/device (a symlink into the real
        device tree) up through its ancestors until it finds a directory
        that has both "idVendor" and "authorized". That combination
        uniquely identifies the actual USB device node, as opposed to a
        USB interface (has neither), a SCSI target, or a SCSI host
        underneath it.

        This makes no assumption about bus number, port number, or hub
        depth. It works for any USB optical drive on the topology of
        any machine.

        Confirmed live (see STATE_OF_THE_PROGRAM.md,
        "Environment-specific hardware limitation"):
        deauthorizing then reauthorizing the path this resolves to
        forces a full USB re-enumeration, including the real capacity
        probe that the kernel-block-layer read inside growisofs -M
        otherwise never gets after an in-place burn. This fixes the
        append failure with no tray action and no interactive session.
        Used by DiscBackend._attempt_usb_reauth() and the standalone
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


# Named media-size presets for parse_disc_size(), matched case-insensitively
# before the raw-suffix/int fallback. "bd-r" aliases ISOBackend.DEFAULT_DISC_BYTES
# so the code defines that size in exactly one place.
_DISC_SIZE_PRESETS: dict[str, int] = {
    "cd": CD_DISC_BYTES,
    "dvd-sl": DVD_SL_DISC_BYTES,
    "dvd-dl": DVD_DL_DISC_BYTES,
    "bd-r": ISOBackend.DEFAULT_DISC_BYTES,
    "bd-dl": BD_DL_DISC_BYTES,
    "bdxl": BDXL_DISC_BYTES,
}


def parse_disc_size(size_str: str) -> int:
    """
    Input:  size_str - human-readable size e.g. "25gb", "100GB", "93GiB",
                        or a named preset e.g. "dvd-sl", "bd-dl", "BDXL"
                        (case-insensitive)
    Output: int - bytes
    Details:
        Supports decimal (kb/mb/gb/tb) and binary (kib/mib/gib/tib) suffixes,
        the named media presets in _DISC_SIZE_PRESETS ("cd", "dvd-sl",
        "dvd-dl", "bd-r", "bd-dl", "bdxl"), or a raw integer byte count.
        Presets match before the unit-suffix parse. None of the
        preset names collide with a recognized size suffix. The code
        parses a string with no suffix and no matching preset as a
        raw integer byte count.
    """
    s = size_str.lower().strip()
    if s in _DISC_SIZE_PRESETS:
        return _DISC_SIZE_PRESETS[s]
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
        Publishes each top-level item from staging into dest_root.
        A directory goes through _publish_dir and a file through
        _publish_file. Each one appears at its final name with one
        rename, so a killed copy never leaves a partial session_NNN.
        At the end, fsyncs dest_root so the renames are durable.
    """
    for item in staging.iterdir():
        if item.is_dir():
            _publish_dir(item, dest_root)
        else:
            _publish_file(item, dest_root)
    _fsync_dir(dest_root)


def _restore_failed_publish(
    prior: Path | None, dest: Path, tmp: Path,
) -> None:
    """
    Input: prior tree, destination, and temporary tree
    Output: None
    """
    from oddarchiver.session import remove_tree

    if prior is not None and not os.path.lexists(dest):
        try:
            os.replace(prior, dest)
        except OSError:
            _log.exception("Cannot move %s back to %s", prior, dest)
    if prior is not None and prior.is_dir() and os.path.lexists(dest):
        remove_tree(prior, "replaced ISO publish directory")
    remove_tree(tmp, "partial ISO publish directory")


def _publish_dir(item: Path, dest_root: Path) -> None:
    """
    Input:  item      - staging directory, for example session_NNN/
            dest_root - destination root
    Output: None
    Details:
        Builds the complete tree in ".<name>.partial-<random>" under
        dest_root, then renames it to <name>. When <name> already exists
        (for example a changed oddarchiver-software/), the temp tree
        first gets a copy of the existing tree, then the staging tree on
        top. This keeps the old merge result. The existing tree moves
        aside to ".<name>.prior-<random>" just before the rename, and
        the function removes it after. On any exception, the function
        moves the prior tree back, removes the temp tree, and re-raises.
        ISOBackend._recover_interrupted_publish cleans up after a kill.
    """
    from oddarchiver.session import remove_tree

    dest = dest_root / item.name
    tmp = Path(tempfile.mkdtemp(dir=dest_root, prefix=f".{item.name}.partial-"))
    prior: Path | None = None
    try:
        if dest.is_dir():
            shutil.copytree(dest, tmp, dirs_exist_ok=True, copy_function=_copy_file_synced)
        shutil.copytree(item, tmp, dirs_exist_ok=True, copy_function=_copy_file_synced)
        for root, _dirs, _files in os.walk(tmp):
            _fsync_dir(Path(root))
        if os.path.lexists(dest):
            prior = Path(tempfile.mkdtemp(dir=dest_root, prefix=f".{item.name}.prior-"))
            os.replace(dest, prior)
        os.replace(tmp, dest)
    except BaseException:
        _log.exception("Cannot publish %s into %s", item.name, dest_root)
        _restore_failed_publish(prior, dest, tmp)
        raise
    if prior is not None:
        remove_tree(prior, "replaced ISO publish directory")


def _publish_file(item: Path, dest_root: Path) -> None:
    """
    Input:  item      - staging file at the staging root, for example volume.json
            dest_root - destination root
    Output: None
    Details:
        Copies the file to ".<name>.partial-<random>" under dest_root,
        then renames it over <name>. On any exception, the function
        removes the temp file and re-raises.
    """
    fd, name = tempfile.mkstemp(dir=dest_root, prefix=f".{item.name}.partial-")
    os.close(fd)
    tmp = Path(name)
    try:
        _copy_file_synced(item, tmp)
        os.replace(tmp, dest_root / item.name)
    except BaseException:
        _log.exception("Cannot publish %s into %s", item.name, dest_root)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            _log.exception("Cannot remove partial ISO publish file %s", tmp)
        raise


def _copy_file_synced(src: str | Path, dst: str | Path) -> str | Path:
    """
    Input:  src - source file
            dst - destination file
    Output: dst
    Details:
        shutil.copy2 followed by fsync of dst, so the data is on disk
        before the publish rename. Used as the copytree copy_function.
    """
    shutil.copy2(src, dst)
    fd = os.open(dst, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return dst


def _fsync_dir(path: Path) -> None:
    """
    Input:  path - directory
    Output: None
    Details: fsyncs the directory so its entries are durable.
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_no_media_text(text: str) -> bool:
    """
    Input:  text - combined xorriso stdout and stderr
    Output: True if the drive has no medium or is not ready
    Details:
        Matches xorriso "Cannot inquire ATIP: no media", "No media
        detected in drive", libburn "Drive not ready", and "empty
        drive". Used by the mediainfo() retry loop.
    """
    blob = text.lower()
    return (
        "no media" in blob
        or "drive not ready" in blob
        or "empty drive" in blob
        or "medium not present" in blob
    )


def _is_disconnect(output_lines: list[str], device: str) -> bool:
    """
    Input:  output_lines - captured growisofs stdout and stderr
            device       - device path under an in-progress write
    Output: True if the drive vanished from the bus mid-write
    Details:
        Requires BOTH an I/O-error-class phrase in the output AND a
        missing device node. The phrases alone are ambiguous: failing
        media raises "Input/output error" while the drive stays present.
        A missing device node separates an unplug from bad media, and it
        is also why the backend cannot re-read prior sessions for
        reassurance.
    """
    blob = "\n".join(output_lines).lower()
    if not any(s in blob for s in _DISCONNECT_SIGNATURES):
        return False
    return not os.path.exists(device)


def _raise_disconnected(device: str, attempted_session: int | None) -> None:
    """
    Input:  device            - device path that disappeared
            attempted_session - session number the write targets, or None
    Output: never returns
    Details:
        Raises the diagnosed error for a mid-write disconnect. Unlike a
        disc-full or SCSI-reset failure, re-verifying prior sessions
        is impossible here, because the drive is gone. The message says so
        instead of claiming the data is safe, and warns that the device
        path can change when the drive comes back.
    """
    session_bit = (
        f"session {attempted_session:03d}" if attempted_session is not None
        else "the session being written"
    )
    raise RuntimeError(
        f"The drive at {device} disappeared while writing {session_bit}. "
        f"growisofs reported I/O errors and {device} no longer exists, so "
        "the device left the bus rather than the disc going bad. The "
        "partial write stops where it stopped; on write-once media an "
        "incomplete track does not rewrite or erase sessions that were "
        "already closed. Prior sessions could NOT be checked "
        "automatically, because the drive is gone. Reconnect the drive, "
        "then confirm the device path did not change, because a USB "
        "drive often returns as a different node. Then run 'oddarchiver "
        "verify --level fast' before retrying the same command. Do NOT "
        "run init, and do NOT follow any '-Z' suggestion above; that "
        "would overwrite the archive."
    )


def _raise_cd_unsupported() -> None:
    """
    Input:  None
    Output: never returns
    Details:
        Refuses a CD burn. ODDArchiver appends sessions to write-once
        media, and CD cannot stay appendable through this toolchain.
        Two real-hardware test burns on blank CD-Rs both came back
        `is written , is closed` with 0 writable blocks, once with
        xorriso -outdev and again with -dev plus -close off, the
        documented growing method.

        A single session writes fine, so the refusal is about append,
        not about writing.

        growisofs cannot substitute here: dvd+rw-tools targets DVD and
        BD only. DVD and BD stay supported.
    """
    raise RuntimeError(
        "CD media is not supported. ODDArchiver appends sessions to "
        "write-once media, but a CD cannot be kept appendable: both test "
        "burns closed the disc after session 000, leaving 0 writable "
        "blocks. Use DVD or BD media instead. See docs/media.md."
    )


def _remaining_media_bytes(
    remaining_match: re.Match[str] | None, free_blocks_all: list[str],
) -> int:
    """
    Input: remaining_match, free_blocks_all
    Output: remaining media capacity in bytes
    """
    if remaining_match:
        return int(remaining_match.group(1)) * 2048
    if free_blocks_all:
        return int(free_blocks_all[-1]) * 2048
    return 0


def _parse_mediainfo(stdout: str) -> DiscInfo:
    """
    Input:  stdout - text output from dvd+rw-mediainfo
    Output: DiscInfo
    Details:
        Extracts session count, remaining blocks (2KB units), and total capacity.
        The parser multiplies remaining and capacity blocks by 2048.
        Blank BD-R discs report "Number of Sessions: 1" with an implicit empty
        session. The parser treats those discs as 0 written sessions.
    """
    session_match = re.search(r"Sessions:\s+(\d+)", stdout)
    # dvd+rw-mediainfo uses *2KB for DVD and *2048 for BD. Accept both.
    _BLOCKS_RE = r"(\d+)\*(?:2KB|2048)"
    remaining_match = re.search(r"Remaining:\s+" + _BLOCKS_RE, stdout)
    # Free Blocks: appears once per track. The last entry is the writable track.
    free_blocks_all = re.findall(r"Free Blocks:\s+" + _BLOCKS_RE, stdout)
    capacity_match = re.search(r"READ CAPACITY:\s+" + _BLOCKS_RE, stdout)
    disc_status_match = re.search(r"Disc status:\s+(\S+)", stdout, re.IGNORECASE)
    last_session_match = re.search(r"State of Last Session:\s+(\S+)", stdout, re.IGNORECASE)

    session_count = int(session_match.group(1)) if session_match else 0

    # Prefer the explicit Remaining: field. Fall back to the last Free
    # Blocks entry: the invisible/writable track on BD-R SRM, which holds
    # the actual remaining capacity. Earlier tracks show 0, so the
    # parser must ignore them.
    remaining_bytes = _remaining_media_bytes(remaining_match, free_blocks_all)

    total_bytes = int(capacity_match.group(1)) * 2048 if capacity_match else 0
    used_bytes = max(0, total_bytes - remaining_bytes)

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
        label="",
    )
