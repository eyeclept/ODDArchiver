"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    BurnBackend abstract interface plus DiscBackend (growisofs/dvd+rw-mediainfo)
    and ISOBackend (genisoimage) implementations. ISOBackend is used for --test-iso
    mode and all automated testing; DiscBackend targets physical BD-R drives.
"""
# Imports
from __future__ import annotations

import abc
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Globals
BDXL_DISC_BYTES = 93 * 2**30   # 100 GB BDXL usable headroom (~93 GiB)


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
        Input:  staging               — path to prepared session_000 directory tree
                label                 — UDF volume label
                expected_session_count — session count recorded at run start (double-burn guard)
        Output: None
        Details:
            Write the first session to the target medium.
            Aborts if session count changed since expected_session_count was read.
        """

    @abc.abstractmethod
    def append(self, staging: Path, label: str, expected_session_count: int) -> None:
        """
        Input:  staging               — path to prepared session_NNN directory tree
                label                 — UDF volume label
                expected_session_count — session count recorded at run start (double-burn guard)
        Output: None
        Details:
            Append a new session to an already-initialized medium.
            Aborts if session count changed since expected_session_count was read.
        """

    @abc.abstractmethod
    def mediainfo(self) -> DiscInfo:
        """
        Input:  None
        Output: DiscInfo — current capacity snapshot
        Details:
            Query the target medium for session count and space.
        """

    @abc.abstractmethod
    def read_path(self, path: str) -> bytes:
        """
        Input:  path — relative path within the disc/ISO filesystem
        Output: bytes — raw file content
        Details:
            Read a file from the target medium into memory.
        """


class DiscBackend(BurnBackend):
    """
    Input:  device — block device path (e.g. /dev/sr0)
    Output: N/A (class)
    Details:
        Wraps growisofs for writes and dvd+rw-mediainfo for capacity queries.
        read_path() requires the disc to be mounted; checks /proc/mounts.
    """

    def __init__(self, device: str) -> None:
        self.device = device

    def init(self, staging: Path, label: str, expected_session_count: int = 0) -> None:
        """
        Input:  staging, label, expected_session_count
        Output: None
        Details:
            Burns session 0 with growisofs -Z (new disc).
        """
        self._guard(expected_session_count)
        cmd = [
            "growisofs", "-Z", self.device,
            "-R", "-T",
            "-V", label,
            "-use-the-force-luke=notray",
            str(staging),
        ]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"growisofs -Z failed with exit code {result.returncode}")

    def append(self, staging: Path, label: str, expected_session_count: int) -> None:
        """
        Input:  staging, label, expected_session_count
        Output: None
        Details:
            Appends a session with growisofs -M (existing disc).
        """
        self._guard(expected_session_count)
        cmd = [
            "growisofs", "-M", self.device,
            "-R", "-T",
            "-V", label,
            "-use-the-force-luke=notray",
            str(staging),
        ]
        result = subprocess.run(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"growisofs -M failed with exit code {result.returncode}")

    def mediainfo(self, retries: int = 6, retry_delay: float = 5.0) -> DiscInfo:
        """
        Input:  retries     — number of extra attempts if the drive reports no media
                retry_delay — seconds to wait between attempts
        Output: DiscInfo
        Details:
            Runs dvd+rw-mediainfo and parses session count and capacity.
            Retries when the drive reports "no media" — this happens immediately
            after a burn while the drive is reloading the tray and the disc is
            still spinning up (typically takes 5–30 seconds).
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

    def read_path(self, path: str) -> bytes:
        """
        Input:  path — relative path on the disc filesystem
        Output: bytes
        Details:
            Locates the disc mount point via /proc/mounts and reads the file.
            If the disc is not mounted, attempts to mount it via udisksctl
            (available on all systemd/udisks2 desktops without root).
            Raises ValueError if path is not a known-safe blob or manifest path.
            Raises RuntimeError if the disc cannot be mounted.
        """
        from oddarchiver.manifest import validate_disc_read_path
        validate_disc_read_path(path)
        mount_point = _find_mount(self.device) or self._auto_mount()
        return (mount_point / path).read_bytes()

    def _auto_mount(self) -> Path:
        """
        Input:  None
        Output: Path — mount point after mounting
        Details:
            Calls 'udisksctl mount -b DEVICE' to mount without root.
            Parses the mount point from udisksctl's stdout.
            Falls back to re-checking /proc/mounts in case another process
            raced to mount it.  Raises RuntimeError if all attempts fail.
        """
        result = subprocess.run(
            ["udisksctl", "mount", "-b", self.device],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            # "Mounted /dev/sr0 at /run/media/user/LABEL."
            m = re.search(r"\bat\s+(\S+?)\.?\s*$", result.stdout.strip())
            if m:
                return Path(m.group(1))
        # Race: another process may have mounted it between our check and now.
        mount_point = _find_mount(self.device)
        if mount_point:
            return mount_point
        err = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Device {self.device} is not mounted and auto-mount failed: {err}"
        )

    def _guard(self, expected: int) -> None:
        """
        Input:  expected — session count recorded at start of run
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
    Input:  iso_path  — filesystem path for the ISO file
            disc_size — simulated capacity in bytes (default 23 GiB for 25 GB BD-R)
    Output: N/A (class)
    Details:
        Wraps genisoimage to build UDF ISOs for --test-iso mode and CI.
        Sessions accumulate in a sibling directory <iso>.d/ and the ISO is
        rebuilt from that tree on every write. read_path() reads from the
        sibling directory directly (no loop mount required).
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
            Creates session_000 inside sessions_root and builds a new UDF ISO.
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
        Input:  prefill_bytes — bytes to simulate as already used
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
        Input:  path — relative path within the disc filesystem
        Output: bytes
        Details:
            Reads from the sessions_root directory tree; no loop mount required.
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
        Input:  expected — session count recorded at start of run
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
        Input:  label — UDF volume label (truncated to 32 chars per UDF spec)
        Output: None
        Details:
            Calls genisoimage to build a UDF ISO from sessions_root.
            Writes to a .tmp file then renames atomically to avoid partial ISO.
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
        Output: int — prefill_bytes from metadata file, or 0
        """
        if not self._meta_path.exists():
            return 0
        try:
            return json.loads(self._meta_path.read_text()).get("prefill_bytes", 0)
        except (json.JSONDecodeError, OSError):
            return 0

    def _read_label(self, sessions: list[Path]) -> str:
        """
        Input:  sessions — sorted list of session_NNN directories
        Output: str — label from the first readable manifest, or ""
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

def parse_disc_size(size_str: str) -> int:
    """
    Input:  size_str — human-readable size e.g. "25gb", "100GB", "93GiB"
    Output: int — bytes
    Details:
        Supports decimal (kb/mb/gb/tb) and binary (kib/mib/gib/tib) suffixes.
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
    Input:  staging   — source directory containing session_NNN/
            dest_root — destination root to merge into
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
    Input:  stdout — text output from dvd+rw-mediainfo
    Output: DiscInfo
    Details:
        Extracts session count, remaining blocks (2KB units), total capacity,
        and label. Remaining and capacity blocks are multiplied by 2048.
        Blank BD-R discs report "Number of Sessions: 1" with an implicit empty
        session; these are treated as 0 written sessions.
    """
    session_match = re.search(r"Sessions:\s+(\d+)", stdout)
    # dvd+rw-mediainfo uses *2KB for DVD and *2048 for BD; accept both.
    _BLOCKS_RE = r"(\d+)\*(?:2KB|2048)"
    remaining_match = re.search(r"Remaining:\s+" + _BLOCKS_RE, stdout)
    # Free Blocks: appears once per track; the last entry is the writable track.
    free_blocks_all = re.findall(r"Free Blocks:\s+" + _BLOCKS_RE, stdout)
    capacity_match = re.search(r"READ CAPACITY:\s+" + _BLOCKS_RE, stdout)
    label_match = re.search(r"Volume id:\s+(\S+)", stdout, re.IGNORECASE)
    disc_status_match = re.search(r"Disc status:\s+(\S+)", stdout, re.IGNORECASE)
    last_session_match = re.search(r"State of Last Session:\s+(\S+)", stdout, re.IGNORECASE)

    session_count = int(session_match.group(1)) if session_match else 0

    # Prefer the explicit Remaining: field; fall back to the last Free Blocks:
    # entry (the invisible/writable track on BD-R SRM, which holds the actual
    # remaining capacity — earlier tracks show 0 and must be ignored).
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


def _find_mount(device: str) -> Path | None:
    """
    Input:  device — block device path e.g. /dev/sr0
    Output: Path to mount point, or None if not mounted
    Details:
        Scans /proc/mounts for a line whose first field matches device.
    """
    try:
        mounts = Path("/proc/mounts").read_text()
    except OSError:
        return None
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == device:
            return Path(parts[1])
    return None
