"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Staging directory construction. Orchestrates scan, diff, content-addressed
    dedup, delta/full encoding, greedy volume packing, and SIGINT-safe cleanup.
"""
# Imports
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import secrets
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING


from oddarchiver.crypto import STREAM_CHUNK_SIZE, NullCrypto, blob_aad, is_bound_ciphertext, new_blob_aad
from oddarchiver.delta import apply_delta, process_bytes_files
from oddarchiver.filehash import hash_file
from oddarchiver.compress import (
    CODEC_NONE, CODEC_LZMA, DEFAULT_CODEC, PROBE_MIN, compress_bytes,
    compress_file, decode_payload, encode_bytes, probe_file_incompressible,
)
from oddarchiver.catalog import safe_display
from oddarchiver.manifest import (
    MANIFEST_VERSION, Manifest, ManifestEntry, SplitPrefix, write_manifest,
)
from oddarchiver.volume import (
    PathVersion, VolumeBaseline, VolumeMarker, VolumeRecord,
    build_volume_baseline, parse_blob_session,
)

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.disc import DiscInfo
    from oddarchiver.cache import CacheManager
    from oddarchiver.crypto import CryptoBackend

# Globals
SPACE_SAFETY_MARGIN = 0.95
# ISO9660 wraps a file size at 4 GiB. Stay below that after encryption overhead.
BLOB_SIZE_CAP = 3 * 2**30

# ISO9660 and UDF allocate file data in 2 KiB blocks.
ISO_BLOCK_BYTES = 2048
# genisoimage writes about 885 KiB of UDF scaffolding before any file data.
# The rounded 1 MiB constant leaves room for volume.json and enc_mode.json.
ISO_SCAFFOLD_BYTES = 1024 * 1024
# UDF and Rock Ridge directory records cost about 2.1 KiB per file
# (measured: iso size minus data blocks and manifest entries).
ISO_FILE_METADATA_BYTES = 2560
# volume.json plus enc_mode.json plus the manifest header of one volume.
VOLUME_CONTROL_BYTES = 1024
# Volume records and control files of the volumes that this pack can close.
VOLUME_RECORD_RESERVE_COUNT = 4

# growisofs -M appends a new session by handing genisoimage/mkisofs the
# previous session as a merge source (-C last,next -M /dev/fd/N). An
# ISO9660 multisession append rebuilds the entire cumulative directory
# tree (path table, directory records, and one full UDF File Entry
# logical block per file) for every file already on the disc, not only
# the files added now. The file data extents are different: the
# builder reuses them by reference and never recopies them. A packing budget
# that only sums new/changed file bytes undercounts this. The shortfall
# grows with the total count of prior files on the disc. Direct reproduction
# confirmed and calibrated this: appending 3085 new files onto a real
# UDF+RockRidge image already holding 8000 files needed 19.9 MB more
# than a fresh, non-merged build of the same 3085 files alone (about
# 2.5 KB of extra overhead per prior file). This constant is that
# measurement rounded up to 8192 bytes/file. The margin covers longer
# filenames, deeper paths, and other genisoimage/mkisofs versions.
MERGE_OVERHEAD_PER_PRIOR_FILE_BYTES = 8192
logger = logging.getLogger(__name__)
_log = logger


@dataclass(frozen=True)
class FillTarget:
    """Measured appendable volume used as a fill packing candidate."""

    volume_index: int
    disc_info: DiscInfo
    prior_file_count: int




@dataclass(frozen=True)
class PlannedVolume:
    """One packed session from size-only dry-run planning."""

    session_n: int
    is_new_volume: bool
    file_count: int
    planned_bytes: int
    split_notes: tuple[tuple[str, int], ...]

@dataclass
class _PreparedBlob:
    """On-disc blob location and manifest fields for one staged file.

    A dedup reference carries encoded_size=0 and either dedup_blob_path
    (an already-burned archive blob) or dedup_origin_rel_path (another
    blob in this same build_staging run). _write_chunk skips the
    physical write for those items and still emits a ManifestEntry.
    """
    rel_path: str
    kind: str
    blob: bytes
    result_checksum: str
    full_size_bytes: int
    encoded_size: int
    source_checksum: str = ""
    delta_size_bytes: int = 0
    streamed: bool = False
    cache_full_path: Path | None = None
    blob_id: str = ""
    blob_origin_session: int | None = None
    blob_path: Path | None = None
    volume_chunk: int | None = None
    volume_chunk_count: int | None = None
    volume_chunk_size: int | None = None
    dedup_blob_path: str = ""
    dedup_origin_rel_path: str = ""
    codec: str = CODEC_NONE
    original_size: int | None = None
    # On-disc bytes beyond encoded_size: block rounding, UDF/Rock Ridge
    # records, and this file's manifest entry. Hand-built packing tests
    # leave it 0 and keep a blob-only budget.
    on_disc_overhead: int = 0


@dataclass(frozen=True)
class _DedupTarget:
    """Existing full blob that a later path with the same checksum can reuse."""
    blob_path: str
    origin_rel_path: str
    blob_origin_session: int | None = None
    codec: str = CODEC_NONE
    original_size: int | None = None


class _CountingDigest:
    """sha256 digest that also counts the bytes it receives.

    CryptoBackend.encrypt_file calls update() with each plaintext chunk
    that it reads. Staging then knows the checksum and the size of the
    exact bytes that it encoded, without a second read.
    """

    def __init__(self) -> None:
        self._sha = hashlib.sha256()
        self.size = 0

    def update(self, chunk: bytes) -> None:
        """
        Input:  chunk - plaintext bytes in read order
        Output: None
        """
        self._sha.update(chunk)
        self.size += len(chunk)

    def hexdigest(self) -> str:
        """
        Input:  None
        Output: hex sha256 of every chunk so far
        """
        return self._sha.hexdigest()


# Functions


def _default_staging_root() -> Path:
    """Per-user staging root with 0o700 permissions.

    Prefers $XDG_RUNTIME_DIR/oddarchiver (tmpfs, cleaned on logout).
    Falls back to ~/.local/state/oddarchiver/staging.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        base = Path(xdg) / "oddarchiver"
    else:
        base = Path.home() / ".local" / "state" / "oddarchiver" / "staging"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        base.chmod(0o700)
    except OSError as exc:
        _log.warning("Could not tighten staging root mode on %s: %s", base, exc)
    return base


def _blob_id(session_n: int, rel_path: str) -> str:
    """
    Input: session index and source path
    Output: independent random 64-character hexadecimal blob name
    Details: The name does not reveal source paths.
    """
    return secrets.token_hex(32)


def _print_bar(current: int, total: int, suffix: str = "", bar_width: int = 35) -> None:
    """
    Input:  current   - items completed
            total     - total items
            suffix    - short label appended after the counter (truncated to 40 chars)
            bar_width - number of characters in the bar itself
    Output: None (writes to stdout, no newline. The caller prints a newline when done)
    Details:
        Uses carriage-return overwrite so successive calls update in-place.
    """
    pct = current / total if total > 0 else 1.0
    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)
    label = safe_display(suffix)[:40].ljust(40)
    print(f"\r  [{bar}] {current}/{total}  {label}", end="", flush=True)


def _sidecar_dir(sidecar: Path) -> Path:
    """
    Input: sidecar - staging sidecar directory
    Output: Checked sidecar directory
    Details: Reject links and directories owned by another user.
    """
    if not sidecar.exists() and not sidecar.is_symlink():
        sidecar.mkdir(mode=0o700)
    info = sidecar.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Unsafe delta cache sidecar directory: {sidecar}")
    sidecar.chmod(0o700)
    return sidecar


_sigint_received = False


def _handle_sigint(_signum: int, _frame: object) -> None:
    """
    Input:  signum and frame - standard signal handler args
    Output: None
    Details:
        Sets a flag. Loops check the flag. Cleanup is not done here.
    """
    global _sigint_received
    _sigint_received = True




def _staging_bytes(staging: Path) -> int:
    """
    Input:  staging - directory to measure
    Output: total bytes used on disk (as reported by du -sb)
    Details:
        Uses du -sb for accurate disk usage including directory overhead.
    """
    proc = subprocess.run(
        ["du", "-sb", str(staging)],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.split()[0])


def _check_staging_lock(staging: Path, session_n: int) -> None:
    """
    Input:  staging    - deterministic staging directory that already exists
            session_n  - session number, used only for the error message
    Output: None
    Details:
        Guards against the exact collision that caused a real disc
        corruption: two processes race to (re)build the same
        deterministically-named staging directory, and one silently
        deletes in-flight files of the other mid-burn. Reads a PID from
        staging/.lock (build_staging writes it on every successful
        start) and probes it with os.kill(pid, 0):

          - No lockfile, or unreadable/non-integer contents. This is a
            leftover from an old oddarchiver version or a crash before
            build_staging wrote the lock. It is stale. The caller may remove
            it.
          - The lockfile names a PID that no longer exists
            (ProcessLookupError). It is stale. The caller may remove
            it.
          - The lockfile names a PID that still exists (kill succeeds,
            or raises PermissionError because it belongs to another
            user). That build is genuinely in flight. Raises
            RuntimeError instead of letting the caller delete live
            data out from under it.
    """
    lock_path = staging / ".lock"
    try:
        pid = int(lock_path.read_text().strip())
    except (OSError, ValueError):
        _log.warning("Staging lock is absent or invalid at %s", lock_path)
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _log.warning("Staging lock names a stopped process: %d", pid)
        return
    except OSError as exc:
        _log.warning("Could not probe staging lock pid %d for %s: %s", pid, staging, exc)
    raise RuntimeError(
        f"Staging directory for session {session_n:03d} ({staging}) is in use "
        f"by pid {pid}; refusing to remove it. If that process is no longer "
        f"running, remove {staging} manually and retry."
    )

def _lock_pid(fd: int) -> str:
    """Input: lock file descriptor. Output: PID text, or empty after a read failure."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 32).decode("ascii", errors="replace").strip()
    except OSError as exc:
        _log.warning("Cannot read archive sync lock pid: %s", exc)
        return ""


@contextlib.contextmanager
def archive_sync_lock(archive_id: str):
    """
    Input:  archive_id - ID from volume.json
    Output: context manager that holds archive-{id}.sync.lock
    Details:
        Takes an exclusive non-blocking flock. A second sync on the same
        archive raises RuntimeError and names the lock path.
    """
    from oddarchiver.catalog import archive_sync_lock_path

    path = archive_sync_lock_path(archive_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = _lock_pid(fd)
            detail = f", pid {pid}" if pid else ""
            raise RuntimeError(
                f"Archive {archive_id} already has a sync in progress "
                f"(lock {path}{detail}). Wait for that process to finish."
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.fsync(fd)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            _log.warning("Cannot unlock archive sync lock %s", path)
        os.close(fd)



def _full_medium_bytes(backend: "BurnBackend") -> int:
    """
    Input:  backend - burn backend for which the caller needs fresh-medium capacity
    Output: int - bytes of a blank medium
    Details:
        ISOBackend stores the configured --disc-size on .disc_size. The
        code uses that value when it is a positive int. DiscBackend uses
        the capacity of the loaded medium (used plus remaining bytes), so
        each later disc of a set matches the media type in the drive. A
        DVD set then splits at DVD size, not at BDXL size. Anything else
        (mocks without an int disc_size) falls back to BDXL_DISC_BYTES.
    """
    from oddarchiver.disc import BDXL_DISC_BYTES, DiscBackend
    disc_size = getattr(backend, "disc_size", None)
    if isinstance(disc_size, int) and disc_size > 0:
        return disc_size
    if isinstance(backend, DiscBackend):
        info = backend.mediainfo()
        capacity = info.used_bytes + info.remaining_bytes
        if capacity > 0:
            return capacity
        _log.warning(
            "Drive %s reports no capacity; using the BDXL size for later discs",
            backend.device,
        )
    return BDXL_DISC_BYTES


def _volume_budget(medium_bytes: int) -> int:
    """
    Input:  medium_bytes - remaining or full-medium capacity in bytes
    Output: int - packable budget after SPACE_SAFETY_MARGIN
    """
    return int(medium_bytes * SPACE_SAFETY_MARGIN)


_STREAM_HEADER_MAX = 80  # Keyfile header plus the format 3 magic.
_STREAM_AEAD_OVERHEAD = 33  # frame(5) + nonce(12) + tag(16).


def _encoded_size_upper_bound(plaintext_size: int, crypto: object) -> int:
    """
    Input:  plaintext_size - source file size in bytes
            crypto         - CryptoBackend that will encode the file
    Output: int - encoded size, or a conservative upper bound
    Details:
        NullCrypto copies bytes, so the bound is exact. Streamed
        PassphraseCrypto/KeyfileCrypto add a per-blob header and
        per-AEAD-chunk nonce+tag. The bound uses the larger header
        so a file is split before encryption rather than packed and
        rejected after.
    """
    if isinstance(crypto, NullCrypto):
        return plaintext_size
    nchunks = (
        1
        if plaintext_size == 0
        else (plaintext_size + STREAM_CHUNK_SIZE - 1) // STREAM_CHUNK_SIZE
    )
    return plaintext_size + _STREAM_HEADER_MAX + (nchunks + 1) * _STREAM_AEAD_OVERHEAD


def _volume_split_lengths(
    plaintext_size: int,
    first_chunk_budget: int,
    full_budget: int,
) -> list[int]:
    """
    Input:  plaintext_size     - encoded payload size in bytes
            first_chunk_budget - budget active for chunk 0
            full_budget        - budget for every later chunk
    Output: list of encoded lengths, one per volume chunk
    Details:
        Chunk 0 is min(remaining, first_chunk_budget, BLOB_SIZE_CAP)
        when that budget is positive, otherwise min(remaining,
        full_budget, BLOB_SIZE_CAP). Later chunks use full_budget
        capped the same way. The last chunk takes the remainder.
        A zero full_budget cannot split a file.
    """
    if full_budget <= 0:
        raise RuntimeError(
            f"Cannot split a file: full volume budget is {full_budget} bytes"
        )
    budget = first_chunk_budget if first_chunk_budget > 0 else full_budget
    lengths: list[int] = []
    remaining = plaintext_size
    while remaining > 0:
        n = min(remaining, budget, BLOB_SIZE_CAP)
        lengths.append(n)
        remaining -= n
        budget = full_budget
    return lengths



def _blob_too_large_error(rel_path: str, size: int, budget: int) -> RuntimeError:
    """
    Input:  rel_path - source-relative path of the oversized file
            size     - encoded blob size in bytes
            budget   - packable budget of one volume in bytes
    Output: RuntimeError naming the file
    """
    return RuntimeError(
        f"Encoded blob for {rel_path!r} ({size} bytes) exceeds one volume's "
        f"budget ({budget} bytes); a single file cannot span two discs."
    )


def _ensure_staging_dir(root: Path, session_n: int) -> Path:
    """
    Input:  root      - parent of oddarchiver_staging_NNN directories
            session_n - session number used in the directory name
    Output: Path - empty staging directory with a live PID lockfile
    Details:
        Removes a stale leftover after _check_staging_lock() agrees it is
        safe. Raises RuntimeError if another process holds the lock.
    """
    staging = root / f"oddarchiver_staging_{session_n:03d}"
    if staging.exists():
        _check_staging_lock(staging, session_n)
        _log.warning(
            "Stale staging dir found for session %03d; removing and rebuilding.",
            session_n,
        )
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / ".lock").write_text(str(os.getpid()))
    return staging


def _log_volume_pack_stop(
    packed_count: int,
    packed_bytes: int,
    budget: int,
    reason: str,
) -> None:
    """
    Input:  packed_count - blobs placed in this volume bin
            packed_bytes - sum of those blobs' encoded_size
            budget       - packable budget for this bin
            reason       - why this bin closed
    Output: None
    Details:
        One INFO line so a volume rollover is diagnosable from the log
        without a debugger. leftover is budget - packed_bytes, floored
        at 0 so a skip-bin with a non-positive budget does not print a
        negative leftover.
    """
    leftover = max(0, budget - packed_bytes)
    _log.info(
        "Volume pack stop: packed %d blob(s) %d bytes into budget %d "
        "(%d bytes leftover); %s",
        packed_count, packed_bytes, budget, leftover, reason,
    )



def _index_manifest_full_blobs(
    index: dict[str, _DedupTarget], manifest: Manifest, first_session: int,
) -> None:
    """
    Input:  index - checksum index to update
            manifest - entries to inspect
            first_session - earliest eligible blob session
    Output: None
    """
    for entry in manifest.entries:
        if entry.volume_chunk is not None:
            continue
        if entry.type != "full" or not entry.file:
            continue
        if parse_blob_session(entry.file) < first_session:
            continue
        if entry.result_checksum in index:
            continue
        index[entry.result_checksum] = _DedupTarget(
            blob_path=entry.file,
            origin_rel_path=entry.path,
            blob_origin_session=entry.blob_origin_session,
            codec=entry.codec,
            original_size=entry.original_size,
        )


def _build_checksum_index(
    disc_state: dict[str, str],
    disc_origin: dict[str, int],
    manifests: "list[Manifest] | None",
    first_session: int = 0,
) -> dict[str, _DedupTarget]:
    """
    Input:  disc_state  - {path: result_checksum} currently on disc
            disc_origin - {path: session} that last wrote each path
            manifests   - optional loaded Manifest list (CLI sync passes this)
            first_session - first session stored on the loaded disc
    Output: {result_checksum: _DedupTarget} for reusable full blobs
    Details:
        When manifests is provided, index every type=full entry that is
        not a volume-split chunk, using the entry's real file path. Oldest
        session wins. That covers current disc_state paths, chained dedup
        references (file already points at the physical blob), and blobs
        left behind by a later delete.
        Without manifests, no physical blob path can be resolved.
        The index stays empty instead of guessing a name.
        Delta entries and volume-split chunks are not targets: a dedup
        reference is always type=full pointing at one complete blob.
    """
    index: dict[str, _DedupTarget] = {}
    if manifests:
        for manifest in manifests:
            if manifest.suspect or manifest.session < first_session:
                continue
            _index_manifest_full_blobs(index, manifest, first_session)
        return index
    return index


def _prepare_dedup_blob(
    rel_path: str,
    result_checksum: str,
    full_size_bytes: int,
    *,
    blob_path: str = "",
    origin_rel_path: str = "",
    blob_origin_session: int | None = None,
    codec: str = CODEC_NONE,
    original_size: int | None = None,
) -> _PreparedBlob:
    """
    Input:  rel_path, result_checksum - new path identity
            full_size_bytes  - size of the content that staging read
            blob_path        - archive-relative full blob already on disc
            origin_rel_path  - this-run path whose blob will be shared
    Output: _PreparedBlob with encoded_size=0 and no ciphertext
    Details:
        Does not encrypt. result_checksum must be the checksum of the
        bytes that staging read, not the scan checksum. Exactly one of
        blob_path or origin_rel_path must be set.
    """
    if not blob_path and not origin_rel_path:
        raise RuntimeError(
            f"dedup blob for {rel_path!r} needs blob_path or origin_rel_path"
        )
    return _PreparedBlob(
        rel_path=rel_path,
        kind="full",
        blob=b"",
        result_checksum=result_checksum,
        full_size_bytes=full_size_bytes,
        encoded_size=0,
        dedup_blob_path=blob_path,
        dedup_origin_rel_path=origin_rel_path,
        blob_origin_session=blob_origin_session,
        codec=codec,
        original_size=original_size,
    )


def _report_read_checksum(
    rel_path: str,
    scan_checksum: str,
    read_checksum: str,
    current_state: dict[str, str],
) -> None:
    """
    Input:  rel_path      - source-relative path
            scan_checksum - checksum that the caller expected for the file
            read_checksum - sha256 of the bytes that staging read
            current_state - {path: sha256} that the caller uses for the
                            manifest, the baseline, and later passes
    Output: None
    Details:
        A file can change between the scan and the read. The archive then
        holds the bytes that staging read, under their own checksum. This
        function logs a warning, prints one line on stderr, and sets
        current_state[rel_path] to read_checksum. The next sync sees the
        newer source content as a change and archives it.
    """
    if read_checksum == scan_checksum:
        return
    _log.warning(
        "source changed while it was read: %s (scan %s, read %s)",
        rel_path, scan_checksum, read_checksum,
    )
    print(
        f"Warning: {safe_display(rel_path)} changed while it was read. "
        "The archive holds the version that was read.",
        file=sys.stderr,
        flush=True,
    )
    current_state[rel_path] = read_checksum


def _dedup_ref(
    rel_path: str,
    read_checksum: str,
    full_size_bytes: int,
    checksum_index: dict[str, _DedupTarget],
    this_run_origins: dict[str, str],
) -> _PreparedBlob | None:
    """
    Input:  rel_path         - source-relative path
            read_checksum    - sha256 of the bytes that staging read
            full_size_bytes  - size of those bytes
            checksum_index   - {checksum: _DedupTarget} for blobs on disc
            this_run_origins - {checksum: rel_path} for full blobs of this run
    Output: A dedup reference, or None when no blob holds the same content
    Details:
        The key is the checksum of the bytes that staging read. A file that
        changed after the scan thus never shares a blob that holds its
        scan content.
    """
    hit = checksum_index.get(read_checksum)
    if hit is not None:
        _log.info("dedup %s -> %s", rel_path, hit.blob_path)
        return _prepare_dedup_blob(
            rel_path, read_checksum, full_size_bytes, blob_path=hit.blob_path,
            blob_origin_session=hit.blob_origin_session,
            codec=hit.codec, original_size=hit.original_size,
        )
    origin_rel = this_run_origins.get(read_checksum)
    if origin_rel is not None and origin_rel != rel_path:
        _log.info("dedup %s -> this-run %s", rel_path, origin_rel)
        return _prepare_dedup_blob(
            rel_path, read_checksum, full_size_bytes, origin_rel_path=origin_rel,
        )
    return None


def _verified_dedup_ref(
    rel_path: str,
    abs_path: Path,
    scan_checksum: str,
    checksum_index: dict[str, _DedupTarget],
    this_run_origins: dict[str, str],
    current_state: dict[str, str],
) -> _PreparedBlob | None:
    """
    Input:  rel_path, abs_path - source identity of a large file
            scan_checksum      - checksum from the scan
            checksum_index, this_run_origins - dedup targets
            current_state      - {path: sha256}, updated on a source change
    Output: A dedup reference, or None when the caller must encode the file
    Details:
        Used for files above STREAM_CHUNK_SIZE only. When the scan checksum
        names a dedup target, this function hashes the file without
        encoding it and looks up the dedup target again with that hash.
        On a miss the caller encodes the file. That encode hashes its
        own bytes, so the archived checksum is always the checksum of
        the archived bytes.
    """
    if scan_checksum not in checksum_index and scan_checksum not in this_run_origins:
        return None
    digest = _CountingDigest()
    hash_file(abs_path, STREAM_CHUNK_SIZE, digest)
    read_checksum = digest.hexdigest()
    ref = _dedup_ref(
        rel_path, read_checksum, digest.size, checksum_index, this_run_origins,
    )
    if ref is not None:
        _report_read_checksum(rel_path, scan_checksum, read_checksum, current_state)
    return ref


def _require_disc_origin(
    rel_path: str, changed: dict[str, str], disc_origin: dict[str, int],
) -> None:
    """
    Input:  rel_path    - source-relative path that staging will encode
            changed     - {path: scan checksum} for changed files
            disc_origin - {path: session} for every path on disc
    Output: None
    Details:
        Raises RuntimeError when a changed file has no origin session.
        A dedup reference does not need one, so the caller checks only
        after the dedup lookup.
    """
    if rel_path in changed and rel_path not in disc_origin:
        raise RuntimeError(
            f"No origin session recorded for changed file {rel_path!r}; "
            "disc_origin is out of sync with disc_state."
        )


def _delta_rebuilds(
    rel_path: str, old_bytes: bytes, delta: bytes, read_checksum: str,
) -> bool:
    """
    Input:  rel_path      - source-relative path, for the log
            old_bytes     - plaintext of the prior version
            delta         - xdelta3 delta from old_bytes to the bytes read
            read_checksum - sha256 of the bytes that staging read
    Output: True when apply_delta(old_bytes, delta) gives read_checksum
    Details:
        A False result is an encoder fault. The caller then stages a full
        blob of the bytes read. This function logs a warning for it.
    """
    try:
        rebuilt = apply_delta(old_bytes, delta)
    except RuntimeError:
        _log.warning(
            "delta self-check could not apply the delta for %s. "
            "Staging a full blob",
            rel_path, exc_info=True,
        )
        return False
    if hashlib.sha256(rebuilt).hexdigest() != read_checksum:
        _log.warning(
            "delta result checksum did not match for %s. Staging a full blob",
            rel_path,
        )
        return False
    return True


def _place_this_run_ref(
    chunks: list[list[_PreparedBlob]], ref: _PreparedBlob,
) -> None:
    """
    Input:  chunks - bins of real blobs
            ref - reference to an origin in one of the bins
    Output: None
    """
    placed = False
    for bin_items in chunks:
        if any(item.rel_path == ref.dedup_origin_rel_path for item in bin_items):
            bin_items.append(ref)
            placed = True
            break
    if not placed:
        raise RuntimeError(
            f"dedup origin {ref.dedup_origin_rel_path!r} for "
            f"{ref.rel_path!r} was not packed"
        )


def _attach_dedup_refs(
    chunks: list[list[_PreparedBlob]],
    prior_refs: list[_PreparedBlob],
    this_run_refs: list[_PreparedBlob],
) -> list[list[_PreparedBlob]]:
    """
    Input:  chunks        - bins of real (non-dedup) blobs
            prior_refs    - refs to already-burned blobs (dedup_blob_path set)
            this_run_refs - refs to a blob packed in this run
    Output: the same bins with refs attached
    Details:
        A this-run ref costs zero bytes and must sit in the same bin as
        its origin so _write_chunk can resolve the origin's final path.
        A prior-session ref can go in any bin; it rides with the first
        non-empty bin so a full current medium does not get a
        manifest-only append of only references. A move-only run with
        no real blobs keeps them in bin 0.
    """
    if not chunks:
        chunks = [[]]
    for ref in sorted(this_run_refs, key=lambda b: b.rel_path):
        _place_this_run_ref(chunks, ref)
    if prior_refs:
        target = 0
        for i, bin_items in enumerate(chunks):
            if bin_items:
                target = i
                break
        chunks[target].extend(sorted(prior_refs, key=lambda b: b.rel_path))
    return chunks


def _ordered_split_chunks(split_items: list[_PreparedBlob]) -> list[_PreparedBlob]:
    """
    Input:  split_items - volume-split blobs in first-seen order
    Output: the same blobs grouped by rel_path, then by volume_chunk
    """
    groups: dict[str, list[_PreparedBlob]] = {}
    group_order: list[str] = []
    for item in split_items:
        if item.rel_path not in groups:
            group_order.append(item.rel_path)
            groups[item.rel_path] = []
        groups[item.rel_path].append(item)
    ordered: list[_PreparedBlob] = []
    for rel_path in group_order:
        group = groups[rel_path]
        group.sort(key=lambda b: b.volume_chunk or 0)
        ordered.extend(group)
    return ordered


def _blob_pack_cost(item: _PreparedBlob) -> int:
    """Input: one prepared blob. Output: encoded_size plus on-disc overhead."""
    return item.encoded_size + item.on_disc_overhead


def _split_chunk_unfittable(item: _PreparedBlob, full_budget: int) -> bool:
    """
    Input:  item - one volume-split chunk
            full_budget - packable budget of a fresh medium
    Output: True when the chunk cannot occupy an empty full-budget bin
    """
    cost = _blob_pack_cost(item)
    return cost >= full_budget and cost > BLOB_SIZE_CAP


def _bin_budget(index: int, first_budget: int, full_budget: int) -> int:
    """Input: bin index and budgets. Output: budget for that bin."""
    return first_budget if index == 0 else full_budget


def _try_append_split_chunk(
    chunks: list[list[_PreparedBlob]], item: _PreparedBlob,
    first_budget: int, full_budget: int,
) -> bool:
    """
    Input:  current bins, one split chunk, and the two budgets
    Output: True when the chunk was placed in the last bin
    """
    if not chunks:
        return False
    budget = _bin_budget(len(chunks) - 1, first_budget, full_budget)
    used = sum(_blob_pack_cost(blob) for blob in chunks[-1])
    cost = _blob_pack_cost(item)
    if used + cost < budget:
        chunks[-1].append(item)
        return True
    if chunks[-1]:
        _log_volume_pack_stop(
            len(chunks[-1]), used, budget,
            f"next volume-split chunk {item.rel_path!r} does not fit",
        )
        return False
    if budget <= 0 or cost < full_budget:
        _log_volume_pack_stop(
            0, 0, budget,
            "current medium has no remaining packable space",
        )
        return False
    if _split_chunk_unfittable(item, full_budget):
        return False
    chunks[-1].append(item)
    return True


def _place_split_chunk(
    chunks: list[list[_PreparedBlob]], item: _PreparedBlob,
    first_budget: int, full_budget: int,
) -> None:
    """
    Input:  bins, one split chunk, and the two budgets
    Output: None. Appends the chunk to the last bin or opens a new bin.
    """
    cost = _blob_pack_cost(item)
    if _try_append_split_chunk(chunks, item, first_budget, full_budget):
        return
    if not chunks and first_budget > 0:
        # Only chunk 0 was sized to the current medium, so only chunk 0
        # may land in bin 0 when its cost reaches the first budget. A
        # resumed tail chunk belongs on a fresh medium instead.
        sized_for_current = (
            (item.volume_chunk or 0) == 0
            and cost >= full_budget
            and not _split_chunk_unfittable(item, full_budget)
        )
        if cost < first_budget or sized_for_current:
            chunks.append([item])
            return
        _log_volume_pack_stop(
            0, 0, first_budget,
            "current medium has no remaining packable space",
        )
        chunks.append([])
    if _split_chunk_unfittable(item, full_budget):
        raise _blob_too_large_error(item.rel_path, cost, full_budget)
    chunks.append([item])


def _pack_split_bins(
    split_items: list[_PreparedBlob], first_budget: int, full_budget: int,
) -> list[list[_PreparedBlob]]:
    """
    Input:  split_items - volume-split blobs
            first_budget - budget on the current medium
            full_budget - budget on a fresh medium
    Output: bins holding every split chunk in order
    """
    ordered = _ordered_split_chunks(split_items)
    chunks: list[list[_PreparedBlob]] = []
    if not ordered:
        return chunks
    if first_budget <= 0:
        _log_volume_pack_stop(
            0, 0, first_budget,
            "current medium has no remaining packable space",
        )
        chunks.append([])
    for item in ordered:
        _place_split_chunk(chunks, item, first_budget, full_budget)
    return chunks


def _fill_existing_bins(
    chunks: list[list[_PreparedBlob]], ordinary: list[_PreparedBlob],
    first_budget: int, full_budget: int,
) -> list[_PreparedBlob]:
    """
    Input:  chunks - bins that already hold split chunks
            ordinary - non-split blobs
            first_budget - budget for bin 0
            full_budget - budget for later bins
    Output: ordinary blobs that did not fit an existing bin
    """
    remaining = sorted(ordinary, key=lambda b: (-b.encoded_size, b.rel_path))
    for i, bin_items in enumerate(chunks):
        remaining = _fill_one_bin(
            i, bin_items, remaining, first_budget, full_budget,
        )
    return remaining


def _fill_one_bin(
    index: int, bin_items: list[_PreparedBlob],
    remaining: list[_PreparedBlob], first_budget: int, full_budget: int,
) -> list[_PreparedBlob]:
    """
    Input:  one bin and leftover ordinary blobs
    Output: ordinary blobs that did not fit this bin
    """
    budget = _bin_budget(index, first_budget, full_budget)
    used = sum(_blob_pack_cost(item) for item in bin_items)
    leftover: list[_PreparedBlob] = []
    for cand in remaining:
        size = _blob_pack_cost(cand)
        if used + size < budget:
            bin_items.append(cand)
            used += size
        else:
            leftover.append(cand)
    _log_volume_pack_stop(
        len(bin_items), used, budget,
        f"filled existing bin {index} with leftover ordinary blobs",
    )
    return leftover


def _pack_ordinary_bins(
    chunks: list[list[_PreparedBlob]], ordinary: list[_PreparedBlob],
    ordinary_budget: int, full_budget: int,
) -> None:
    """
    Input:  chunks - bins to update
            ordinary - non-split blobs to pack
            ordinary_budget - budget for the first new ordinary bin
            full_budget - budget for each later bin
    Output: None
    """
    remaining = sorted(ordinary, key=lambda b: (-b.encoded_size, b.rel_path))
    budget = ordinary_budget
    while remaining:
        bin_items: list[_PreparedBlob] = []
        deferred: list[_PreparedBlob] = []
        used = 0
        for item in remaining:
            size = _blob_pack_cost(item)
            if used + size < budget:
                bin_items.append(item)
                used += size
            else:
                deferred.append(item)
        if not bin_items:
            nxt = remaining[0]
            _log_volume_pack_stop(
                0, 0, budget,
                f"next file {nxt.rel_path!r} is {nxt.encoded_size} bytes "
                f"and does not fit ({len(remaining)} blob(s) deferred)",
            )
            chunks.append([])
            budget = full_budget
            continue
        leftover_files = deferred
        if leftover_files:
            smallest = min(
                leftover_files, key=lambda b: (b.encoded_size, b.rel_path),
            )
            reason = (
                f"next file {smallest.rel_path!r} is "
                f"{smallest.encoded_size} bytes and does not fit "
                f"({len(leftover_files)} blob(s) deferred)"
            )
        else:
            reason = "no more source files"
        _log_volume_pack_stop(len(bin_items), used, budget, reason)
        chunks.append(bin_items)
        remaining = leftover_files
        budget = full_budget


def _pack_with_ordinary_first(
    split_items: list[_PreparedBlob], ordinary: list[_PreparedBlob],
    first_budget: int, full_budget: int,
) -> list[list[_PreparedBlob]]:
    """
    Input:  split chunks, whole blobs, and the two bin budgets
    Output: bins with whole blobs packed first, then split chunks
    """
    chunks: list[list[_PreparedBlob]] = []
    if first_budget <= 0:
        _log_volume_pack_stop(
            0, 0, first_budget,
            "current medium has no remaining packable space",
        )
        chunks.append([])
        ordinary_budget = full_budget
    else:
        ordinary_budget = first_budget
    if ordinary:
        _pack_ordinary_bins(chunks, ordinary, ordinary_budget, full_budget)
    for item in _ordered_split_chunks(split_items):
        _place_split_chunk(chunks, item, first_budget, full_budget)
    return chunks if chunks else [[]]


def _reject_unfittable_ordinary(
    ordinary: list[_PreparedBlob], full_budget: int,
) -> None:
    """Input: whole blobs and full-medium budget. Output: None. Raises if a blob cannot fit."""
    for item in ordinary:
        size = _blob_pack_cost(item)
        if size >= full_budget:
            raise _blob_too_large_error(item.rel_path, size, full_budget)


def _pack_with_splits_first(
    split_items: list[_PreparedBlob], ordinary: list[_PreparedBlob],
    first_budget: int, full_budget: int,
) -> list[list[_PreparedBlob]]:
    """
    Input: split chunks, whole blobs, and the two bin budgets
    Output: bins with split chunks packed first, then whole blobs
    """
    chunks = _pack_split_bins(split_items, first_budget, full_budget)
    if split_items:
        ordinary = _fill_existing_bins(
            chunks, ordinary, first_budget, full_budget,
        )
        ordinary_budget = full_budget
    else:
        ordinary_budget = first_budget
    if not ordinary:
        return chunks if chunks else [[]]
    _pack_ordinary_bins(chunks, ordinary, ordinary_budget, full_budget)
    return chunks


def _partition_pack_blobs(
    blobs: list[_PreparedBlob],
) -> tuple[list[_PreparedBlob], list[_PreparedBlob], list[_PreparedBlob]]:
    """
    Input: prepared blobs
    Output: prior-session refs, this-run refs, and real blobs
    """
    prior_refs = [b for b in blobs if b.dedup_blob_path]
    this_run_refs = [
        b for b in blobs
        if b.dedup_origin_rel_path and not b.dedup_blob_path
    ]
    real = [
        b for b in blobs
        if not b.dedup_blob_path and not b.dedup_origin_rel_path
    ]
    return prior_refs, this_run_refs, real


def _pack_blobs(
    blobs: list[_PreparedBlob],
    first_budget: int,
    full_budget: int,
    ordinary_first: bool = False,
) -> list[list[_PreparedBlob]]:
    """
    Input:  blobs        - encoded blobs (ordinary and optional volume-split)
            first_budget - remaining_bytes * SPACE_SAFETY_MARGIN on the current medium
            full_budget  - fresh-medium bytes * SPACE_SAFETY_MARGIN
    Output: list of bins. Each bin holds the blobs for one session directory
    Details:
        Volume-split blobs (volume_chunk is not None) pack in first-seen
        rel_path order, then by ascending volume_chunk. A chunk shares
        the current bin when encoded_size plus on_disc_overhead keeps
        the bin under its budget (strict <). Bin 0 uses first_budget.
        Later bins use full_budget. A chunk that does not fit closes
        the bin and opens a new one. A chunk whose cost reaches
        full_budget and exceeds BLOB_SIZE_CAP raises RuntimeError.
        Chunk N cannot land in an earlier bin than chunk N-1.
        After every split chunk is placed, ordinary blobs fill leftover
        space in each bin, greedy largest-first. Ties use rel_path.
        Remaining ordinary blobs open new bins. ordinary_first packs
        whole blobs first so a gap-fill split can share bin 0. If
        first_budget is 0 or negative, the code keeps an empty first
        bin and skips the current medium. Leftover ordinary blobs then
        pack with full_budget after the split bins.
        Each item consumes its encoded_size plus its on_disc_overhead.
        A non-split blob whose on-disc cost reaches full_budget raises
        RuntimeError naming that file. The code keeps an empty first bin
        when nothing fits the remaining space of the current medium.
        Each closed bin logs packed bytes, leftover budget, and why packing
        stopped.
        Dedup references have encoded_size=0. This-run refs attach to the
        origin's bin after real blobs pack. Prior-session refs attach to
        the first non-empty bin.
    """
    if not blobs:
        return [[]]

    prior_refs, this_run_refs, real = _partition_pack_blobs(blobs)
    if not real:
        return _attach_dedup_refs([[]], prior_refs, this_run_refs)

    split_items = [b for b in real if b.volume_chunk is not None]
    ordinary = [b for b in real if b.volume_chunk is None]
    _reject_unfittable_ordinary(ordinary, full_budget)
    packer = (
        _pack_with_ordinary_first if ordinary_first else _pack_with_splits_first
    )
    chunks = packer(split_items, ordinary, first_budget, full_budget)
    return _attach_dedup_refs(chunks, prior_refs, this_run_refs)


def _plan_ordinary_blob(
    rel_path: str, encoded_size: int, full_size: int, kind: str,
) -> _PreparedBlob:
    """Input: path and sizes. Output: a packable blob with no payload bytes."""
    return _PreparedBlob(
        rel_path=rel_path,
        kind=kind,
        blob=b"",
        result_checksum="",
        full_size_bytes=full_size,
        encoded_size=encoded_size,
        original_size=full_size,
    )


def _plan_split_blobs(
    rel_path: str,
    plaintext_size: int,
    chunk0: int,
    full_budget: int,
    crypto: object,
) -> list[_PreparedBlob]:
    """Input: path, size, and split budgets. Output: volume-split size-only blobs."""
    encoded = _encoded_size_upper_bound(plaintext_size, crypto)
    lengths = _volume_split_lengths(encoded, chunk0, full_budget)
    count = len(lengths)
    blobs: list[_PreparedBlob] = []
    for index, length in enumerate(lengths):
        blobs.append(_PreparedBlob(
            rel_path=rel_path,
            kind="full",
            blob=b"",
            result_checksum="",
            full_size_bytes=plaintext_size,
            encoded_size=length,
            volume_chunk=index,
            volume_chunk_count=count,
            volume_chunk_size=length,
            original_size=plaintext_size,
        ))
    return blobs


def _plan_prepared_from_classify(
    source: Path,
    oversized_jobs: list[tuple[str, int, int]],
    new_paths: list[str],
    changed_paths: list[str],
    encoded_sizes: dict[str, int],
    crypto: object,
    full_budget: int,
) -> list[_PreparedBlob]:
    """Input: classified paths and sizes. Output: size-only prepared blobs."""
    prepared: list[_PreparedBlob] = []
    for rel_path, size, chunk0 in oversized_jobs:
        prepared.extend(
            _plan_split_blobs(rel_path, size, chunk0, full_budget, crypto),
        )
    for rel_path in new_paths:
        full_size = (source / rel_path).stat().st_size
        encoded = encoded_sizes.get(
            rel_path, _encoded_size_upper_bound(full_size, crypto),
        )
        prepared.append(
            _plan_ordinary_blob(rel_path, encoded, full_size, "full"),
        )
    for rel_path in changed_paths:
        full_size = (source / rel_path).stat().st_size
        encoded = encoded_sizes.get(
            rel_path, _encoded_size_upper_bound(full_size, crypto),
        )
        prepared.append(
            _plan_ordinary_blob(rel_path, encoded, full_size, "full"),
        )
    return prepared


def _bins_to_planned_volumes(
    bins: list[list[_PreparedBlob]],
    session_n: int,
    first_is_new_volume: bool,
) -> list[PlannedVolume]:
    """Input: packed bins and session numbering. Output: planned volumes."""
    volumes: list[PlannedVolume] = []
    for i, items in enumerate(bins):
        if not items:
            continue
        notes: dict[str, int] = {}
        for item in items:
            if item.volume_chunk is not None and item.volume_chunk_count:
                notes[item.rel_path] = item.volume_chunk_count
        files = {item.rel_path for item in items}
        is_new = True if i > 0 else first_is_new_volume
        volumes.append(PlannedVolume(
            session_n=session_n + i,
            is_new_volume=is_new,
            file_count=len(files),
            planned_bytes=_real_pack_cost(items),
            split_notes=tuple(sorted(notes.items())),
        ))
    return volumes


def plan_session_volumes(
    source: Path,
    new_files: set[str],
    changed: set[str],
    encoded_sizes: dict[str, int],
    disc_state: dict[str, str],
    current_state: dict[str, str],
    disc_info: DiscInfo,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    session_n: int,
    split_large: bool,
    manifests: list[Manifest] | None = None,
    volume_baseline: VolumeBaseline | None = None,
    volume_marker: VolumeMarker | None = None,
    volume_label: str = "",
    deleted: list[str] | None = None,
    bundle_bytes: int = 0,
) -> list[PlannedVolume]:
    """
    Input: source paths, encoded sizes, disc capacity, and pack flags
    Output: packed volumes using the same packer as a real burn
    Details:
        Uses file sizes and encoded-size bounds only. Does not encrypt,
        copy, or write blob files.
    """
    deleted_paths = [] if deleted is None else deleted
    changed_map = {path: current_state[path] for path in changed}
    first_budget, _merge, _effective = _first_pack_budget(
        disc_info, session_n, disc_state, bundle_bytes,
    )
    _full_medium, full_budget, image_costs = _full_pack_budget(
        backend, crypto, volume_baseline, manifests, volume_marker,
        volume_label, new_files, changed_map, deleted_paths, current_state,
    )
    oversized_jobs, new_paths, changed_paths, ordinary_first = (
        _classify_staging_paths(
            source, new_files, changed_map, crypto, image_costs, full_budget,
            first_budget, split_large,
        )
    )
    prepared = _plan_prepared_from_classify(
        source, oversized_jobs, new_paths, changed_paths, encoded_sizes,
        crypto, full_budget,
    )
    _stamp_on_disc_overhead(prepared, crypto, image_costs)
    bins = _pack_blobs(
        prepared, first_budget, full_budget, ordinary_first=ordinary_first,
    )
    bins, first_is_new = _skip_empty_current_bin(bins, session_n, False)
    return _bins_to_planned_volumes(bins, session_n, first_is_new)




def _staging_budget_error(rel_path: str, size: int, budget: float) -> RuntimeError:
    """
    Input:  rel_path - source-relative path of the oversized file
            size     - source file size in bytes
            budget   - local staging budget in bytes (fixed across groups)
    Output: RuntimeError naming the file
    """
    return RuntimeError(
        f"{rel_path!r} ({size} bytes) exceeds the entire local staging "
        f"budget ({int(budget)} bytes) on its own. Unlike disc-capacity "
        "chunking, the staging budget cannot grow between groups -- free "
        "space on the staging filesystem (or point XDG_RUNTIME_DIR at a "
        "larger one) and retry."
    )


def _staging_budget_bytes(root: Path) -> float:
    """
    Input:  root - staging root directory
    Output: float - free bytes on its filesystem * SPACE_SAFETY_MARGIN
    """
    return shutil.disk_usage(root).free * SPACE_SAFETY_MARGIN


def partition_by_staging_budget(
    paths: set[str],
    source: Path,
    staging_root: Path | None = None,
) -> list[list[str]]:
    """
    Input:  paths        - source-relative new/changed paths to partition
            source       - archive source directory
            staging_root - override staging root (default: _default_staging_root())
    Output: list of groups. Each group is a sorted list of source-relative paths
    Details:
        Greedily bin-packs paths by (source / p).stat().st_size into
        groups whose running total stays under
        shutil.disk_usage(staging_root or _default_staging_root()).free
        * SPACE_SAFETY_MARGIN. The sort is source-relative path order,
        for determinism. An empty input returns [[]]. A changeset that
        fits in one group returns [sorted(paths)]. That is
        indistinguishable from the ungrouped path this replaces: no
        split, one session, unchanged output.
        The same fixed staging budget applies to every group. This
        differs from the disc-capacity chunks of _pack_blobs, where a
        fresh medium grants fresh headroom on every later chunk. A
        single path whose own size exceeds the whole budget therefore
        raises RuntimeError naming that file. The code never routes
        that path to a group of its own.
    """
    if not paths:
        return [[]]

    root = staging_root if staging_root is not None else _default_staging_root()
    budget = _staging_budget_bytes(root)

    ordered = sorted(paths)
    sizes = {p: (source / p).stat().st_size for p in ordered}

    groups: list[list[str]] = [[]]
    used = 0
    for p in ordered:
        size = sizes[p]
        if size >= budget:
            raise _staging_budget_error(p, size, budget)
        if used + size >= budget and groups[-1]:
            groups.append([])
            used = 0
        groups[-1].append(p)
        used += size
    return groups


def find_unfittable_paths(
    paths: set[str],
    source: Path,
    current_state: dict[str, str],
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    disc_state: dict[str, str],
    disc_origin: dict[str, int],
    manifests: "list[Manifest] | None" = None,
    staging_root: Path | None = None,
) -> dict[str, str]:
    """
    Input:  paths         - source-relative new/changed paths about to be staged
            source        - archive source directory
            current_state - {path: sha256} for the whole source
            backend       - BurnBackend, for the full-medium capacity
            crypto        - CryptoBackend, for the encoded-size bound
            disc_state    - real {path: checksum} already on the archive
            disc_origin   - {path: session} for disc_state
            manifests     - optional loaded Manifest list, for dedup lookup
            staging_root  - override staging root (default: _default_staging_root())
    Output: {path: reason} for every path --skip-large must leave out
    Details:
        A path is unfittable when its source size reaches the whole
        local staging budget (partition_by_staging_budget would raise).
        A path that is larger than one disc is not unfittable: staging
        splits it into volume chunks. A path whose checksum already has
        a reusable full blob on the archive is never listed: it costs
        zero bytes as a dedup reference.
    """
    root = staging_root if staging_root is not None else _default_staging_root()
    staging_budget = _staging_budget_bytes(root)
    checksum_index = _build_checksum_index(disc_state, disc_origin, manifests)

    skipped: dict[str, str] = {}
    for rel_path in sorted(paths):
        if current_state[rel_path] in checksum_index:
            continue
        size = (source / rel_path).stat().st_size
        if size >= staging_budget:
            skipped[rel_path] = (
                f"{size} bytes exceeds the local staging budget "
                f"({int(staging_budget)} bytes)"
            )
    return skipped


def _write_dedup_entry(
    item: _PreparedBlob,
    written_blob_paths: dict[str, str],
    written_blob_origins: dict[str, int | None],
) -> ManifestEntry:
    """
    Input:  item - reference to a full blob
            written_blob_paths - paths of blobs written in this run
            written_blob_origins - origin sessions of those blobs
    Output: manifest entry for the reference
    """
    origin_session = (
        item.blob_origin_session if item.dedup_blob_path
        else written_blob_origins[item.dedup_origin_rel_path]
    )
    if item.dedup_blob_path:
        blob_file = item.dedup_blob_path
    else:
        try:
            blob_file = written_blob_paths[item.dedup_origin_rel_path]
        except KeyError:
            _log.exception(
                "dedup origin %r for %r was not written",
                item.dedup_origin_rel_path, item.rel_path,
            )
            raise RuntimeError(
                f"dedup origin {item.dedup_origin_rel_path!r} for "
                f"{item.rel_path!r} was not written"
            )
    entry = ManifestEntry(
        path=item.rel_path,
        type="full",
        result_checksum=item.result_checksum,
        full_size_bytes=item.full_size_bytes,
        source_checksum=item.source_checksum,
        file=blob_file,
        blob_origin_session=origin_session,
        codec=item.codec,
        original_size=item.original_size,
    )
    written_blob_paths[item.rel_path] = blob_file
    written_blob_origins[item.rel_path] = origin_session
    return entry


def _write_real_blob_entry(
    staging: Path,
    session_n: int,
    session_name: str,
    full_dir: Path,
    deltas_dir: Path,
    item: _PreparedBlob,
    written_blob_paths: dict[str, str],
    written_blob_origins: dict[str, int | None],
) -> ManifestEntry:
    """
    Input:  staging - staging directory root
            session_n, session_name - session number and directory name
            full_dir, deltas_dir - blob destination directories
            item - blob to write
            written_blob_paths - paths of full blobs written in this run
            written_blob_origins - origin sessions of those blobs
    Output: manifest entry for the written blob
    """
    blob_id = item.blob_id or _blob_id(session_n, item.rel_path)
    if item.kind == "delta":
        if item.cache_full_path is not None:
            sidecar = _sidecar_dir(staging.with_name(staging.name + ".cache_full"))
            dest_full = sidecar / item.result_checksum
            if dest_full.exists():
                item.cache_full_path.unlink(missing_ok=True)
            else:
                shutil.move(str(item.cache_full_path), str(dest_full))
            item.cache_full_path = dest_full
        dest = deltas_dir / blob_id
    else:
        dest = full_dir / blob_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    if item.streamed and item.blob_path is None:
        raise RuntimeError(
            f"streamed blob {item.rel_path!r} has no blob_path"
        )
    if item.blob_path is not None:
        shutil.move(str(item.blob_path), str(dest))
    else:
        dest.write_bytes(item.blob)
    file_field = f"{session_name}/full/{blob_id}" if item.kind == "full" else ""
    delta_field = f"{session_name}/deltas/{blob_id}" if item.kind == "delta" else ""
    entry = ManifestEntry(
        path=item.rel_path,
        type=item.kind,
        result_checksum=item.result_checksum,
        full_size_bytes=item.full_size_bytes,
        source_checksum=item.source_checksum,
        delta_file=delta_field,
        delta_size_bytes=item.delta_size_bytes,
        file=file_field,
        streamed=item.streamed,
        volume_chunk=item.volume_chunk,
        volume_chunk_count=item.volume_chunk_count,
        volume_chunk_size=item.volume_chunk_size,
        blob_origin_session=item.blob_origin_session,
        codec=item.codec,
        original_size=item.original_size,
    )
    if file_field:
        written_blob_paths[item.rel_path] = file_field
        written_blob_origins[item.rel_path] = item.blob_origin_session
    return entry


def _write_chunk(
    staging: Path,
    session_n: int,
    blobs: list[_PreparedBlob],
    deleted: list[str],
    source: Path,
    crypto: "CryptoBackend",
    written_blob_paths: dict[str, str] | None = None,
    written_blob_origins: dict[str, int | None] | None = None,
) -> None:
    """
    Input:  staging   - staging directory root for this chunk
            session_n - session number written into session_NNN/
            blobs     - encoded blobs that belong in this session
            deleted   - paths removed in this session (empty on later volumes)
            source    - archive source directory, stored on the manifest
            crypto    - backend used to encrypt the session manifest
            written_blob_paths - optional {rel_path: archive blob path} filled
                              as real blobs are written, reused across chunks
    Output: None
    Details:
        Writes full/ and deltas/ blobs, then writes the session manifest
        with the selected crypto backend. A dedup reference skips the
        physical write and points the file at the existing blob path.
        Checks the SIGINT flag between files.
    """
    if written_blob_paths is None:
        written_blob_paths = {}
    if written_blob_origins is None:
        written_blob_origins = {}
    session_name = f"session_{session_n:03d}"
    session_dir = staging / session_name
    full_dir = session_dir / "full"
    deltas_dir = session_dir / "deltas"
    full_dir.mkdir(parents=True, exist_ok=True)
    deltas_dir.mkdir(parents=True, exist_ok=True)

    entries: list[ManifestEntry] = []
    for item in blobs:
        if _sigint_received:
            raise KeyboardInterrupt
        if item.dedup_blob_path or item.dedup_origin_rel_path:
            entries.append(_write_dedup_entry(item, written_blob_paths, written_blob_origins))
            continue
        entries.append(_write_real_blob_entry(
            staging, session_n, session_name, full_dir, deltas_dir,
            item, written_blob_paths, written_blob_origins,
        ))

    if _sigint_received:
        raise KeyboardInterrupt

    _inherit_dedup_codec(blobs, entries)

    manifest = Manifest(
        version=MANIFEST_VERSION,
        session=session_n,
        timestamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source=str(source),
        label="",
        based_on_session=(session_n - 1) if session_n > 0 else None,
        encryption={},
        entries=entries,
        deleted=deleted,
        manifest_checksum="",
    )
    write_manifest(session_dir, manifest, crypto=crypto)

def _inherit_dedup_codec(
    blobs: list[_PreparedBlob], entries: list[ManifestEntry],
) -> None:
    """
    Input:  blobs - prepared blobs in write order; entries - their entries
    Output: None; dedup entries copy the codec of the blob they reference
    Details:
        A dedup entry shares another path's stored bytes, so it must
        declare that blob's codec to decode on restore. A same-run origin
        may appear after the reference, so this runs after all entries
        exist; archive origins already carry the codec on the target.
    """
    by_path = {blob.rel_path: blob for blob in blobs}
    by_file = {entry.file: entry for entry in entries if entry.file}
    for blob, entry in zip(blobs, entries):
        if not (blob.dedup_origin_rel_path or blob.dedup_blob_path):
            continue
        origin = by_path.get(blob.dedup_origin_rel_path) if blob.dedup_origin_rel_path else None
        if origin is not None:
            entry.codec = origin.codec
            entry.original_size = origin.original_size
            continue
        shared = by_file.get(entry.file)
        if shared is not None and shared is not entry:
            entry.codec = shared.codec
            entry.original_size = shared.original_size


def _copy_file_range(src: Path, dest: Path, offset: int, length: int) -> None:
    """Input: source file, dest, byte offset, length. Output: dest holds that range."""
    with src.open("rb") as inf, dest.open("wb") as outf:
        if offset:
            inf.seek(offset)
        remaining = length
        while remaining > 0:
            chunk = inf.read(min(STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            outf.write(chunk)
            remaining -= len(chunk)


def _hash_into(path: Path, digest: object) -> int:
    """
    Input:  path - file to read; digest - object with update(bytes)
    Output: byte count that the digest consumed
    Details:
        Used when a blob is stored raw: the encode pass reads the source
        once, so the same read hashes it.
    """
    total = 0
    with Path(path).open("rb") as inf:
        while True:
            chunk = inf.read(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return total


def _encrypt_payload_file(
    payload: Path,
    dest: Path,
    crypto: "CryptoBackend | None",
    session_n: int,
    kind: str,
    blob_id: str,
    digest: object | None = None,
) -> int:
    """Input: payload path and encode destination. Output: encoded size."""
    if crypto is None:
        shutil.copyfile(payload, dest)
        if digest is not None:
            with payload.open("rb") as inf:
                while True:
                    chunk = inf.read(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
    elif isinstance(crypto, NullCrypto):
        crypto.encrypt_file(payload, dest, digest=digest)
    else:
        crypto.encrypt_file(
            payload, dest, digest=digest,
            aad=new_blob_aad(session_n, kind, blob_id),
        )
    return dest.stat().st_size


def _prepare_encoded_blob(
    rel_path: str, kind: str, plaintext: bytes, scratch_dir: Path,
    session_n: int, result_checksum: str, full_size_bytes: int,
    source_checksum: str = "", delta_size_bytes: int = 0,
    cache_full_path: Path | None = None,
    crypto: "CryptoBackend | None" = None,
) -> _PreparedBlob:
    """
    Input: rel_path, kind, plaintext, scratch_dir, session_n, checksums and sizes
    Output: A prepared blob that keeps ciphertext on disk
    Details: Compress, then encrypt, and keep only the path and size.
    """
    blob_id = _blob_id(session_n, rel_path)
    dest = scratch_dir / blob_id
    codec, payload = encode_bytes(plaintext)
    if crypto is None:
        encrypted = payload
    elif isinstance(crypto, NullCrypto):
        encrypted = crypto.encrypt(payload)
    else:
        encrypted = crypto.encrypt(
            payload, aad=new_blob_aad(session_n, kind, blob_id),
        )
    dest.write_bytes(encrypted)
    encoded_size = len(encrypted)
    return _PreparedBlob(
        rel_path=rel_path, kind=kind, blob=b"",
        result_checksum=result_checksum, full_size_bytes=full_size_bytes,
        encoded_size=encoded_size, source_checksum=source_checksum,
        delta_size_bytes=delta_size_bytes, blob_path=dest,
        cache_full_path=cache_full_path, blob_id=blob_id,
        blob_origin_session=session_n, codec=codec,
        original_size=len(plaintext),
    )



def remove_tree(path: Path, label: str = "") -> bool:
    """
    Input: path  - temporary directory that can hold decrypted plaintext
            label - name shown to the user on failure
    Output: True when the directory is gone
    Details:
        Reports removal failure with a log warning and a user note.
        The caller keeps its original error. A failed removal must stay
        visible because the directory can hold decrypted plaintext.
    """
    try:
        if not path.exists():
            return True
        shutil.rmtree(path)
    except OSError as exc:
        name = label or str(path)
        _log.warning("Cannot remove %s at %s: %s", name, path, exc)
        print(
            f"warning: Cannot remove {name} at {path}. "
            "It can hold decrypted plaintext. Remove it by hand.",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def _manifest_entry_size(rel_path: str, crypto: "CryptoBackend | None" = None) -> int:
    """
    Input: rel_path - source-relative path of one file entry
            crypto   - CryptoBackend that writes the session manifest
    Output: Encoded manifest bytes reserved for one file entry
    Details:
        Measures the JSON delta of one ManifestEntry with the real
        path, then bounds the encryption overhead. write_manifest
        serializes with indent=2, so the measurement does too.
    """
    from oddarchiver.crypto import NullCrypto

    sample = ManifestEntry(
        path=rel_path,
        type="delta",
        result_checksum="0" * 64,
        full_size_bytes=2**53 - 1,
        source_checksum="0" * 64,
        delta_file="session_999/deltas/" + ("0" * 64),
        delta_size_bytes=2**53 - 1,
        file="session_999/full/" + ("0" * 64),
        encrypted_dek="0" * 256,
        streamed=True,
        volume_chunk=255,
        volume_chunk_count=255,
        codec="lzma",
        original_size=2**53 - 1,
    )
    sample_entry = dataclasses.asdict(sample)
    # write_manifest drops an unset chunk size; keep the measurement as is.
    sample_entry.pop("volume_chunk_size", None)
    one = json.dumps({"entries": [sample_entry]}, indent=2).encode("utf-8")
    zero = json.dumps({"entries": []}, indent=2).encode("utf-8")
    plain = len(one) - len(zero)
    if crypto is None or isinstance(crypto, NullCrypto):
        return plain
    return _encoded_size_upper_bound(plain, crypto)


def _baseline_plain_size(baseline: object) -> int:
    """
    Input: baseline - VolumeBaseline-like object
    Output: Plaintext JSON byte size of that baseline
    Details:
        Matches write_volume_baseline serialization. The caller
        serializes the seed once and reuses the byte count.
    """
    return len(json.dumps(dataclasses.asdict(baseline), indent=2).encode("utf-8"))


def _event_increment_size(rel_path: str, checksum: str) -> int:
    """
    Input: rel_path - pending path event
            checksum - result_checksum of that path, or ""
    Output: Added plaintext baseline bytes for one pending path event
    Details:
        Adds one PathVersion plus its state and origin entries. The
        JSON framing is measured once from a sample with empty strings.
        The path adds its escaped length three times (history key,
        state key, origin key). The checksum adds it twice (state
        value and result_checksum).
    """
    version = PathVersion(
        session=2**31 - 1,
        volume_index=2**31 - 1,
        kind="delta",
        result_checksum="",
        full_size_bytes=2**53 - 1,
        timestamp="2026-10-15T00:00:00.000000+00:00",
        blob_session=2**31 - 1,
        volume_chunk=255,
        volume_chunk_count=255,
    )
    empty = VolumeBaseline(
        version=2, volumes=[], history={}, state={}, origin={},
    )
    one = VolumeBaseline(
        version=2,
        volumes=[],
        history={"": [version]},
        state={"": ""},
        origin={"": 2**31 - 1},
    )
    frame = _baseline_plain_size(one) - _baseline_plain_size(empty)
    path_bytes = len(json.dumps(rel_path, ensure_ascii=True).encode("utf-8"))
    checksum_bytes = len(json.dumps(checksum, ensure_ascii=True).encode("utf-8"))
    return frame + 3 * path_bytes + 2 * checksum_bytes


def _volume_record_size(label: str) -> int:
    """
    Input: label - archive label of one volume record
    Output: Added plaintext baseline bytes for one volume record
    Details:
        Measures one VolumeRecord against an empty baseline. The
        sample keeps unknown session bounds, the largest record form.
    """
    empty = VolumeBaseline(
        version=2, volumes=[], history={}, state={}, origin={},
    )
    one = VolumeBaseline(
        version=2,
        volumes=[VolumeRecord(
            volume_index=2**31 - 1,
            label=label,
            first_session=2**31 - 1,
            last_session=2**31 - 1,
        )],
        history={}, state={}, origin={},
    )
    return _baseline_plain_size(one) - _baseline_plain_size(empty)


def _baseline_reservation(
    crypto: "CryptoBackend", baseline: VolumeBaseline | None,
    manifests: list[Manifest] | None, marker: VolumeMarker | None,
    label: str, events: list[str], checksums: dict[str, str],
) -> int:
    """
    Input: Existing baseline, closing disc manifests, label, and pending paths
    Output: Upper bound for the next disc's encoded baseline
    Details:
        Serializes the closing disc baseline once and adds one measured
        event per pending path. The sample PathVersion plus its state
        and origin entries give the per-event size. The volume records
        and control files of the volumes that this pack can close are
        reserved as a fixed measured allowance. The bound includes the
        encryption header and tags. A history whose baseline cannot fit
        needs a larger disc.
    """
    seed = build_volume_baseline(baseline, manifests or [], marker, label)
    plain = _baseline_plain_size(seed)
    for path in events:
        plain += _event_increment_size(path, checksums.get(path, ""))
    per_volume = _volume_record_size(label) + VOLUME_CONTROL_BYTES
    plain += per_volume * VOLUME_RECORD_RESERVE_COUNT
    return _encoded_size_upper_bound(plain, crypto)


def _iso_scaffold_bytes(medium_bytes: int) -> int:
    """
    Input: medium_bytes - full medium capacity
    Output: Bytes reserved for ISO control structures on a fresh image
    Details:
        genisoimage writes about 885 KiB of UDF and Rock Ridge
        scaffolding before it places any file data. The constant is
        that measurement rounded up to 1 MiB. A medium smaller than
        that has no room for a real image, so the byte budget stays
        unchanged for synthetic packing budgets.
    """
    return ISO_SCAFFOLD_BYTES if medium_bytes > ISO_SCAFFOLD_BYTES else 0


def _on_disk_cost(
    rel_path: str, encoded_size: int, crypto: "CryptoBackend | None" = None,
    image_costs: bool = True,
) -> int:
    """
    Input: rel_path     - source-relative path of one packed file
            encoded_size - ciphertext bytes of that file
            crypto       - CryptoBackend that writes the session manifest
            image_costs  - True to add ISO block rounding and UDF records
    Output: On-disc bytes that this file consumes
    Details:
        File data rounds up to an ISO9660 block. UDF and Rock Ridge
        add directory records per file. The session manifest grows one
        entry per file. A dedup reference writes no data block, so
        callers use only the manifest entry for it. A medium too small
        for the ISO scaffolding cannot hold a real image. For that
        synthetic medium, image_costs is False and the cost is the
        data plus the manifest entry.
    """
    entry = _manifest_entry_size(rel_path, crypto)
    if not image_costs:
        return encoded_size + entry
    waste = (-encoded_size) % ISO_BLOCK_BYTES
    return encoded_size + waste + ISO_FILE_METADATA_BYTES + entry


def _prepared_blob_overhead(
    item: "_PreparedBlob", crypto: "CryptoBackend | None" = None,
    image_costs: bool = True,
) -> int:
    """
    Input: item        - one prepared blob
            crypto      - CryptoBackend that writes the session manifest
            image_costs - True to add ISO block rounding and UDF records
    Output: On-disc bytes beyond item.encoded_size
    Details:
        A dedup reference points at an existing blob and writes no
        data block. It still takes one manifest entry.
    """
    if item.dedup_blob_path or item.dedup_origin_rel_path:
        return _manifest_entry_size(item.rel_path, crypto)
    cost = _on_disk_cost(item.rel_path, item.encoded_size, crypto, image_costs)
    return cost - item.encoded_size


def _stamp_on_disc_overhead(
    items: list["_PreparedBlob"], crypto: "CryptoBackend | None" = None,
    image_costs: bool = True,
) -> None:
    """
    Input: items       - prepared blobs about to pack
            crypto      - CryptoBackend that writes the session manifest
            image_costs - True to add ISO block rounding and UDF records
    Output: None (sets on_disc_overhead on each item)
    Details:
        The packer charges encoded_size plus this overhead so the
        budget covers block rounding, UDF records, and manifest growth.
    """
    for item in items:
        item.on_disc_overhead = _prepared_blob_overhead(item, crypto, image_costs)


def _prepare_streamed_blob(
    rel_path: str,
    abs_path: Path,
    crypto: "CryptoBackend",
    scratch_dir: Path,
    session_n: int,
    source_checksum: str = "",
) -> _PreparedBlob:
    """
    Input:  rel_path, abs_path - source identity
            crypto, scratch_dir, session_n - encode destination
            source_checksum - manifest field for a changed file
    Output: _PreparedBlob with streamed=True and blob_path set
    Details:
        Compresses abs_path, then encrypts the payload via encrypt_file.
        A digest receives the plaintext during compress. result_checksum
        and full_size_bytes describe those plaintext bytes. blob stays
        empty. encoded_size is the on-disk ciphertext size.
    """
    blob_id = _blob_id(session_n, rel_path)
    payload_path = scratch_dir / f"{blob_id}.payload"
    scratch_path = scratch_dir / blob_id
    digest = _CountingDigest()
    if DEFAULT_CODEC == CODEC_NONE or (
        abs_path.stat().st_size >= PROBE_MIN
        and probe_file_incompressible(abs_path)
    ):
        codec = CODEC_NONE
        encoded_size = _encrypt_payload_file(
            abs_path, scratch_path, crypto, session_n, "full", blob_id,
            digest=digest,
        )
    else:
        codec, _payload_size = compress_file(abs_path, payload_path, digest=digest)
        try:
            encoded_size = _encrypt_payload_file(
                payload_path, scratch_path, crypto, session_n, "full", blob_id,
            )
        finally:
            payload_path.unlink(missing_ok=True)
    return _PreparedBlob(
        rel_path=rel_path,
        kind="full",
        blob=b"",
        result_checksum=digest.hexdigest(),
        full_size_bytes=digest.size,
        encoded_size=encoded_size,
        source_checksum=source_checksum,
        streamed=True,
        blob_path=scratch_path,
        blob_id=blob_id,
        blob_origin_session=session_n,
        codec=codec,
        original_size=digest.size,
    )

def _build_split_payload(
    abs_path: Path, scratch_dir: Path, base_id: str,
) -> tuple[Path, Path | None, str, int, str, int]:
    """
    Input:  abs_path - source file
            scratch_dir, base_id - payload location
    Output: encrypt source, payload temp path to remove or None,
            codec, encoded payload size, read checksum, and source size
    Details:
        An incompressible file is hashed in place and encrypted straight
        from the source file. Otherwise the whole file is compressed to
        one payload file that the chunk encoder slices. The digest covers
        the source bytes, so its checksum is the scan checksum and its
        size is the source size. The encoded payload size drives the
        chunk boundaries.
    """
    payload_path = scratch_dir / f"{base_id}.payload"
    digest = _CountingDigest()
    incompressible = DEFAULT_CODEC == CODEC_NONE or (
        abs_path.stat().st_size >= PROBE_MIN
        and probe_file_incompressible(abs_path)
    )
    if incompressible:
        payload_size = _hash_into(abs_path, digest)
        return (
            abs_path, None, CODEC_NONE, payload_size,
            digest.hexdigest(), digest.size,
        )
    codec, payload_size = compress_file(abs_path, payload_path, digest=digest)
    return (
        payload_path, payload_path, codec, payload_size,
        digest.hexdigest(), digest.size,
    )


def _encode_split_chunks(
    rel_path: str,
    encrypt_source: Path,
    base_id: str,
    crypto: "CryptoBackend",
    scratch_dir: Path,
    session_n: int,
    source_checksum: str,
    lengths: list[int],
    start_chunk: int,
    chunk_count: int,
    offset: int,
    read_checksum: str,
    codec: str,
    original_size: int,
) -> list[_PreparedBlob]:
    """
    Input:  rel_path, encrypt_source, base_id - source and payload identity
            crypto, scratch_dir, session_n - encode destination
            source_checksum - whole-file manifest field for a changed file
            lengths - payload slice length per chunk, in order
            start_chunk, chunk_count, offset - chunk numbering and
                                                payload offset of chunk 0
            read_checksum, codec, original_size - whole-file manifest fields
    Output: one streamed _PreparedBlob per slice
    Details:
        Encrypts each slice separately with the per-chunk AAD. Restore
        decrypts each chunk and appends the plaintext slices, so the
        format matches earlier archives whatever the start chunk is.
    """
    blobs: list[_PreparedBlob] = []
    for index in range(start_chunk, start_chunk + len(lengths)):
        if _sigint_received:
            raise KeyboardInterrupt
        length = lengths[index - start_chunk]
        blob_id = f"{base_id}_{index}"
        scratch_path = scratch_dir / blob_id
        if isinstance(crypto, NullCrypto):
            crypto.encrypt_file(
                encrypt_source, scratch_path, offset=offset, length=length,
            )
        else:
            crypto.encrypt_file(
                encrypt_source, scratch_path, offset=offset, length=length,
                aad=new_blob_aad(session_n, "full", blob_id),
            )
        blobs.append(_PreparedBlob(
            rel_path=rel_path,
            kind="full",
            blob=b"",
            result_checksum=read_checksum,
            full_size_bytes=original_size,
            encoded_size=scratch_path.stat().st_size,
            source_checksum=source_checksum,
            streamed=True,
            blob_path=scratch_path,
            volume_chunk=index,
            volume_chunk_count=chunk_count,
            volume_chunk_size=length,
            blob_id=blob_id,
            blob_origin_session=session_n,
            codec=codec,
            original_size=original_size,
        ))
        offset += length
    return blobs


def _prepare_volume_split_blobs(
    rel_path: str,
    abs_path: Path,
    crypto: "CryptoBackend",
    scratch_dir: Path,
    session_n: int,
    source_checksum: str,
    first_chunk_budget: int,
    full_budget: int,
) -> list[_PreparedBlob]:
    """
    Input:  rel_path, abs_path - source identity
            crypto, scratch_dir, session_n - encode destination
            source_checksum - whole-file manifest field for a changed file
            first_chunk_budget, full_budget - encoded split sizes
    Output: one streamed _PreparedBlob per volume chunk
    Details:
        Compresses abs_path to one payload, then cuts the payload into
        encoded chunk lengths and encrypts each slice separately with the
        per-chunk AAD. Restore decrypts each chunk and appends the
        plaintext slices, so the format matches earlier archives.
        result_checksum and full_size_bytes describe the whole plaintext.
    """
    base_id = _blob_id(session_n, rel_path)
    encrypt_source, payload_tmp, codec, payload_size, read_checksum, original = (
        _build_split_payload(abs_path, scratch_dir, base_id)
    )
    try:
        lengths = _volume_split_lengths(
            payload_size, first_chunk_budget, full_budget,
        )
        return _encode_split_chunks(
            rel_path, encrypt_source, base_id, crypto, scratch_dir, session_n,
            source_checksum, lengths, 0, len(lengths), 0, read_checksum,
            codec, original,
        )
    finally:
        if payload_tmp is not None:
            payload_tmp.unlink(missing_ok=True)


def _prepare_tail_split_blobs(
    rel_path: str,
    abs_path: Path,
    crypto: "CryptoBackend",
    scratch_dir: Path,
    session_n: int,
    prefix: SplitPrefix,
    first_chunk_budget: int,
    full_budget: int,
) -> list[_PreparedBlob]:
    """
    Input:  rel_path, abs_path - source identity
            crypto, scratch_dir, session_n - encode destination
            prefix - clean chunk prefix already burned for this file
            first_chunk_budget, full_budget - encoded split sizes
    Output: only the missing chunks k+1..n-1, at the recorded boundaries
    Details:
        Rebuilds the whole payload, then verifies that its checksum,
        size, and codec match the burned prefix. On any difference the
        source changed or the plan no longer matches, so the function
        falls back to today's whole-file restage. Otherwise it slices
        the payload from prefix.payload_offset and keeps chunk indices
        and chunk_count, so restore assembles chunks 0..n-1 unchanged.
    """
    base_id = _blob_id(session_n, rel_path)
    encrypt_source, payload_tmp, codec, payload_size, read_checksum, original = (
        _build_split_payload(abs_path, scratch_dir, base_id)
    )
    try:
        expected_tail = prefix.chunk_count - prefix.next_chunk
        remaining = payload_size - prefix.payload_offset
        matches = (
            codec == prefix.codec
            and read_checksum == prefix.result_checksum
            and original == prefix.original_size
            and remaining > 0
        )
        if matches:
            lengths = _volume_split_lengths(remaining, 0, full_budget)
            if len(lengths) == expected_tail:
                _log.info(
                    "resuming volume split %s at chunk %d of %d",
                    rel_path, prefix.next_chunk, prefix.chunk_count,
                )
                return _encode_split_chunks(
                    rel_path, encrypt_source, base_id, crypto, scratch_dir,
                    session_n, "", lengths,
                    prefix.next_chunk, prefix.chunk_count,
                    prefix.payload_offset, read_checksum, codec, original,
                )
        _log.warning(
            "Cannot extend the volume split for %s (source changed or the "
            "chunk plan differs); restaging the whole file",
            rel_path,
        )
        lengths = _volume_split_lengths(
            payload_size, first_chunk_budget, full_budget,
        )
        return _encode_split_chunks(
            rel_path, encrypt_source, base_id, crypto, scratch_dir, session_n,
            "", lengths, 0, len(lengths), 0,
            read_checksum, codec, original,
        )
    finally:
        if payload_tmp is not None:
            payload_tmp.unlink(missing_ok=True)


def _open_staging_workspace(
    root: Path, session_n: int, created: list[Path],
) -> tuple[Path, Path]:
    """
    Input: root, session_n, and the list of paths to clean up.
    Output: staging and scratch paths.
    """
    staging = _ensure_staging_dir(root, session_n)
    created.append(staging)
    sidecar = staging.with_name(staging.name + ".cache_full")
    created.append(sidecar)
    if not remove_tree(sidecar, "old delta cache sidecar"):
        raise RuntimeError(f"Cannot remove old delta cache sidecar: {sidecar}")
    scratch_dir = root / f"oddarchiver_scratch_{session_n:03d}"
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    scratch_dir.mkdir(parents=True)
    created.append(scratch_dir)
    return staging, scratch_dir


def _hash_source_files(
    source: Path, source_files: list[Path], skipped: list[str], total_files: int,
) -> dict[str, str]:
    """Input: source root, files, skipped paths, and total count. Output: relative path to checksum."""
    current_state: dict[str, str] = {}
    done = 0
    skipped_set = set(skipped)
    for src_file in source_files:
        if _sigint_received:
            raise KeyboardInterrupt
        rel = str(src_file.relative_to(source))
        if rel in skipped_set:
            continue
        try:
            current_state[rel] = hash_file(src_file, STREAM_CHUNK_SIZE).hexdigest()
        except OSError as exc:
            _log.warning("Scan skips unreadable file %s: %s", src_file, exc)
            skipped_set.add(rel)
            print(f"  warning: skipped {rel} (unreadable: {exc})", flush=True)
            continue
        done += 1
        _print_bar(done, total_files, suffix=rel)
    return current_state


def _scan_staging_source(
    source: Path, staging: Path, precomputed: dict[str, str] | None,
) -> dict[str, str]:
    """
    Input: source and staging paths, and optional precomputed hashes.
    Output: source paths mapped to checksums.
    Details: Warns per skipped path (dangling symlink, FIFO, socket,
        unreadable file) and prints a skipped count.
    """
    if precomputed is not None:
        return precomputed
    from oddarchiver.cli import _scan_skips

    print(f"Scanning {source} ...", flush=True)
    source_files = sorted(
        f for f in source.rglob("*")
        if f.is_file() and not f.is_relative_to(staging)
    )
    skipped = _scan_skips(source, staging, sorted(source.rglob("*")))
    for rel in skipped:
        print(f"  warning: skipped {rel}", flush=True)
    if skipped:
        print(
            f"  skipped {len(skipped)} path(s) that cannot be archived.",
            flush=True,
        )
    total_files = len(source_files)
    print(f"  {total_files} file(s) found. Hashing...", flush=True)
    current_state = _hash_source_files(source, source_files, skipped, total_files)
    if total_files:
        print()
    return current_state


def _check_staging_space(
    source: Path, staging: Path, new_files: set[str],
    changed: dict[str, str], current_state: dict[str, str],
    checksum_index: dict[str, _DedupTarget],
) -> None:
    """
    Input: source and staging paths, changed paths, and known checksums.
    Output: None. Exits if staging lacks space for files that need encoding.
    """
    seen_new_checksums: set[str] = set()
    estimate_paths: list[str] = []
    for rel_path in list(new_files) + list(changed):
        checksum = current_state[rel_path]
        if checksum in checksum_index:
            continue
        if rel_path in new_files:
            if checksum in seen_new_checksums:
                continue
            seen_new_checksums.add(checksum)
        estimate_paths.append(rel_path)
    estimated_bytes = sum(
        (source / p).stat().st_size for p in estimate_paths
    )
    if estimated_bytes:
        staging_free = shutil.disk_usage(staging).free
        staging_limit = staging_free * SPACE_SAFETY_MARGIN
        if estimated_bytes >= staging_limit:
            _log.error(
                "Staging space check failed: need ~%d bytes for %d new/changed "
                "file(s), staging filesystem at %s has %d bytes free * %.2f = %d. "
                "The staging area is separate from the target disc/ISO capacity; "
                "it commonly lives on a size-limited tmpfs. Free up space there, "
                "or point XDG_RUNTIME_DIR at a larger filesystem, and retry.",
                estimated_bytes, len(estimate_paths), staging,
                staging_free, SPACE_SAFETY_MARGIN, int(staging_limit),
            )
            raise SystemExit(1)


def _appendable_remaining(disc_info: DiscInfo) -> int:
    """
    Input:  disc snapshot
    Output: bytes one growisofs -M session may add, or 0 when closed for -M
    Details:
        growisofs refuses a session that would cross the 4 GiB boundary
        ("next session would cross 4GB boundary"), so an append may only
        use the space below it. A blank disc (-Z) has no such limit.
    """
    # scbc ignore[is-true-false-strict] None means unknown; only False is closed
    if disc_info.appendable is False:
        return 0
    if disc_info.session_count > 0 and disc_info.used_bytes:
        from oddarchiver.disc import GROWISOFS_MERGE_LIMIT_BYTES

        below_boundary = max(0, GROWISOFS_MERGE_LIMIT_BYTES - disc_info.used_bytes)
        return min(disc_info.remaining_bytes, below_boundary)
    return disc_info.remaining_bytes


def _first_pack_budget(
    disc_info: DiscInfo, session_n: int, disc_state: dict[str, str],
    bundle_bytes: int, prior_file_count: int | None = None,
) -> tuple[int, int, int]:
    """
    Input: disc capacity, session number, prior paths, and bundle size.
    Output: first budget, merge overhead, and effective remaining bytes.
    """
    remaining = _appendable_remaining(disc_info)
    prior_files = (
        len(disc_state) if prior_file_count is None else prior_file_count
    )
    merge_overhead = (
        prior_files * MERGE_OVERHEAD_PER_PRIOR_FILE_BYTES
        if session_n > 0 else 0
    )
    # A medium that holds no image yet also pays the ISO scaffolding.
    first_scaffold = (
        _iso_scaffold_bytes(remaining)
        if disc_info.used_bytes == 0 else 0
    )
    effective_remaining = max(
        0, remaining - merge_overhead - first_scaffold
    )
    first_budget = _volume_budget(effective_remaining)
    if bundle_bytes:
        if bundle_bytes >= first_budget:
            _log.error(
                "Software bundle needs %d bytes; packing budget is %d. "
                "The bundle does not fit on the remaining disc space.",
                bundle_bytes, first_budget,
            )
            raise SystemExit(1)
        first_budget -= bundle_bytes
    return first_budget, merge_overhead, effective_remaining


def _real_pack_cost(items: list[_PreparedBlob]) -> int:
    """Input: prepared blobs. Output: on-disc cost of blobs that occupy space."""
    total = 0
    for item in items:
        if item.dedup_blob_path or item.dedup_origin_rel_path:
            continue
        total += _blob_pack_cost(item)
    return total


def _blobs_fit_first_budget(
    items: list[_PreparedBlob], first_budget: int,
) -> bool:
    """Input: blobs and bin-0 budget. Output: True when they all fit one bin."""
    if first_budget <= 0:
        return False
    return _real_pack_cost(items) < first_budget


def _convert_foreign_prepared(
    items: list[_PreparedBlob], source: Path, crypto: CryptoBackend,
    scratch_dir: Path, session_n: int, full_budget: int,
    current_state: dict[str, str],
) -> list[_PreparedBlob]:
    """
    Input: prepared blobs and encoding context for a different existing volume.
    Output: the same list with deltas and prior-disc refs turned into full blobs.
    """
    converted: list[_PreparedBlob] = []
    for item in items:
        if item.kind != "delta" and not item.dedup_blob_path:
            converted.append(item)
            continue
        converted.extend(_replacement_for_moved_blob(
            item, source, crypto, scratch_dir, session_n,
            full_budget, current_state,
        ))
    return converted


def _fill_target_budget(
    target: FillTarget, session_n: int, bundle_bytes: int,
) -> int:
    """Input: fill candidate, session, bundle size. Output: bin-0 budget."""
    first_budget, _merge, _effective = _first_pack_budget(
        target.disc_info, session_n, {}, bundle_bytes,
        prior_file_count=target.prior_file_count,
    )
    return first_budget


def _choose_fill_target(
    targets: list[FillTarget],
    items: list[_PreparedBlob],
    inserted_index: int,
    session_n: int,
    bundle_bytes: int,
    source: Path,
    crypto: CryptoBackend,
    scratch_dir: Path,
    full_budget: int,
    current_state: dict[str, str],
    image_costs: bool,
) -> tuple[FillTarget | None, list[_PreparedBlob]]:
    """
    Input: oldest-first fill candidates, encoded blobs, and packing context.
    Output: the oldest volume that can hold every blob, plus converted blobs.
    """
    _stamp_on_disc_overhead(items, crypto, image_costs)
    for target in targets:
        work = items
        if target.volume_index != inserted_index:
            work = _convert_foreign_prepared(
                items, source, crypto, scratch_dir, session_n,
                full_budget, current_state,
            )
            _stamp_on_disc_overhead(work, crypto, image_costs)
        budget = _fill_target_budget(target, session_n, bundle_bytes)
        if _blobs_fit_first_budget(work, budget):
            _log.info(
                "Fill packs session %03d onto volume %s (%d bytes, budget %d)",
                session_n, target.volume_index, _real_pack_cost(work), budget,
            )
            return target, work
    return None, items


def _split_fill_work(
    work: list[_PreparedBlob],
) -> tuple[list[_PreparedBlob], list[_PreparedBlob], list[_PreparedBlob]]:
    """Input: converted fill blobs. Output: ordinary, split, and this-run refs."""
    split_blobs = [item for item in work if item.volume_chunk is not None]
    this_run = [
        item for item in work
        if item.dedup_origin_rel_path and not item.dedup_blob_path
    ]
    prepared = [
        item for item in work
        if item.volume_chunk is None
        and not (item.dedup_origin_rel_path and not item.dedup_blob_path)
    ]
    return prepared, split_blobs, this_run


def _apply_fill_target_choice(
    fill_targets: list[FillTarget] | None,
    fill_chosen: list[int] | None,
    inserted_index: int | None,
    prepared: list[_PreparedBlob],
    split_blobs: list[_PreparedBlob],
    dedup_refs: list[_PreparedBlob],
    session_n: int,
    bundle_bytes: int,
    source: Path,
    crypto: CryptoBackend,
    scratch_dir: Path,
    full_budget: int,
    current_state: dict[str, str],
    image_costs: bool,
    first_budget: int,
) -> tuple[list[_PreparedBlob], list[_PreparedBlob], list[_PreparedBlob], int, bool]:
    """
    Input: fill candidates and encoded blobs.
    Output: blobs, first-bin budget, and whether bin 0 is a different volume.
    """
    if not fill_targets or inserted_index is None:
        return prepared, split_blobs, dedup_refs, first_budget, False
    prior_refs = [item for item in dedup_refs if item.dedup_blob_path]
    this_run = [
        item for item in dedup_refs
        if item.dedup_origin_rel_path and not item.dedup_blob_path
    ]
    items = prepared + split_blobs + prior_refs
    chosen, work = _choose_fill_target(
        fill_targets, items, inserted_index, session_n, bundle_bytes,
        source, crypto, scratch_dir, full_budget, current_state, image_costs,
    )
    if chosen is None:
        return prepared, split_blobs, dedup_refs, first_budget, False
    if fill_chosen is not None:
        fill_chosen.append(chosen.volume_index)
    first_budget = _fill_target_budget(chosen, session_n, bundle_bytes)
    prepared, split_blobs, converted_refs = _split_fill_work(work)
    convert_first = chosen.volume_index != inserted_index
    return prepared, split_blobs, this_run + converted_refs, first_budget, convert_first




def _full_pack_budget(
    backend: BurnBackend, crypto: CryptoBackend,
    volume_baseline: VolumeBaseline | None, manifests: list[Manifest] | None,
    volume_marker: VolumeMarker | None, volume_label: str,
    new_files: set[str], changed: dict[str, str], deleted: list[str],
    current_state: dict[str, str],
) -> tuple[int, int, bool]:
    """
    Input: disc backend, crypto, baseline, and paths for the new image.
    Output: medium bytes, full budget, and whether image costs apply.
    """
    full_medium = _full_medium_bytes(backend)
    # Every later chunk is a fresh image, so it always pays the scaffolding.
    full_scaffold = _iso_scaffold_bytes(full_medium)
    # A medium too small for the scaffolding cannot hold a real image.
    # Its budget counts file data and manifest entries only.
    image_costs = full_scaffold > 0
    reserve = _baseline_reservation(
        crypto, volume_baseline, manifests, volume_marker,
        volume_label, [*new_files, *changed, *deleted], current_state,
    )
    usable = max(0, full_medium - reserve - full_scaffold)
    full_budget = _volume_budget(usable)
    if full_budget <= 0:
        raise RuntimeError(
            "The archive history baseline does not fit on a new disc. "
            "Use a larger disc size."
        )
    return full_medium, full_budget, image_costs


def _blob_exceeds_volume(encoded: int, cost: int, full_budget: int) -> bool:
    """Input: encoded size, on-disc cost, full budget. Output: True if the file must split."""
    return encoded >= BLOB_SIZE_CAP or cost >= full_budget


def _max_encoded_for_budget(
    rel_path: str, budget: int, crypto: CryptoBackend, image_costs: bool,
) -> int:
    """
    Input: path, leftover pack budget, crypto, and image-cost flag
    Output: largest encoded size whose on-disc cost stays under budget
    """
    if budget <= 0:
        return 0
    lo = 0
    hi = budget
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _on_disk_cost(rel_path, mid, crypto, image_costs) < budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _first_bin_deferred(
    records: list[tuple[str, int, int, str]], first_budget: int,
) -> tuple[int, list[tuple[str, int, int, str]]]:
    """
    Input: (path, size, cost, group) records and bin 0 budget
    Output: leftover in bin 0 and records that did not fit that bin
    """
    ordered = sorted(records, key=lambda rec: (-rec[2], rec[0]))
    used = 0
    deferred: list[tuple[str, int, int, str]] = []
    for rec in ordered:
        cost = rec[2]
        if used + cost < first_budget:
            used += cost
        else:
            deferred.append(rec)
    return first_budget - used, deferred


def _gap_fill_split_job(
    records: list[tuple[str, int, int, str]], first_budget: int,
    crypto: CryptoBackend, image_costs: bool,
) -> tuple[tuple[str, int, int] | None, list[tuple[str, int, int, str]]]:
    """
    Input: whole-file records, current-medium budget, crypto, image costs
    Output: one gap-fill split job or None, and the remaining whole records
    """
    leftover, deferred = _first_bin_deferred(records, first_budget)
    if leftover <= 0 or not deferred:
        return None, records
    victim = deferred[0]
    chunk0 = _max_encoded_for_budget(
        victim[0], leftover, crypto, image_costs,
    )
    if chunk0 <= 0:
        return None, records
    rest = [rec for rec in records if rec[0] != victim[0]]
    return (victim[0], victim[1], chunk0), rest


def _must_split_jobs(
    must: list[tuple[str, int]], first_budget: int, full_budget: int,
) -> list[tuple[str, int, int]]:
    """
    Input: must-split (path, size) jobs and the two budgets
    Output: jobs with a chunk-0 budget. The largest file uses
            first_budget when that budget is positive.
    """
    must = sorted(must, key=lambda job: (-job[1], job[0]))
    jobs: list[tuple[str, int, int]] = []
    for i, (rel_path, size) in enumerate(must):
        if i == 0 and first_budget > 0:
            chunk0 = first_budget
        else:
            chunk0 = full_budget
        jobs.append((rel_path, size, chunk0))
    return jobs


def _staging_path_records(
    source: Path, new_files: set[str], changed: dict[str, str],
    crypto: CryptoBackend, image_costs: bool, full_budget: int,
) -> tuple[list[tuple[str, int]], list[tuple[str, int, int, str]]]:
    """
    Input: source paths, crypto, image costs, and full-medium budget
    Output: must-split (path, size) jobs and whole-file records
    """
    must: list[tuple[str, int]] = []
    records: list[tuple[str, int, int, str]] = []
    for rel_path in [*sorted(new_files), *sorted(changed)]:
        size = (source / rel_path).stat().st_size
        encoded = _encoded_size_upper_bound(size, crypto)
        cost = _on_disk_cost(rel_path, encoded, crypto, image_costs)
        if _blob_exceeds_volume(encoded, cost, full_budget):
            must.append((rel_path, size))
        else:
            group = "new" if rel_path in new_files else "changed"
            records.append((rel_path, size, cost, group))
    return must, records


def _classify_staging_paths(
    source: Path, new_files: set[str], changed: dict[str, str],
    crypto: CryptoBackend, image_costs: bool, full_budget: int,
    first_budget: int, split_large: bool,
) -> tuple[list[tuple[str, int, int]], list[str], list[str], bool]:
    """
    Input: source paths, crypto, image costs, budgets, and split_large
    Output: split jobs (path, size, chunk0 budget), new paths, changed
            paths, and whether packing must place whole blobs first
    """
    must, records = _staging_path_records(
        source, new_files, changed, crypto, image_costs, full_budget,
    )
    oversized_jobs = _must_split_jobs(must, first_budget, full_budget)
    ordinary_first = False
    if split_large and not must:
        extra, records = _gap_fill_split_job(
            records, first_budget, crypto, image_costs,
        )
        if extra is not None:
            oversized_jobs.append(extra)
            ordinary_first = True
    new_paths = [rec[0] for rec in records if rec[3] == "new"]
    changed_paths = [rec[0] for rec in records if rec[3] == "changed"]
    return oversized_jobs, new_paths, changed_paths, ordinary_first


def _stage_split_jobs(
    oversized_jobs: list[tuple[str, int, int]], source: Path,
    current_state: dict[str, str], checksum_index: dict[str, _DedupTarget],
    this_run_origins: dict[str, str], changed: dict[str, str],
    disc_origin: dict[str, int], disc_state: dict[str, str],
    crypto: CryptoBackend, scratch_dir: Path, session_n: int,
    full_budget: int,
    split_blobs: list[_PreparedBlob], dedup_refs: list[_PreparedBlob],
) -> None:
    """
    Input: oversized jobs, source state, budgets, and staging collections.
    Output: split blobs and dedup references appended in job order.
    """
    if oversized_jobs:
        print(
            f"Staging {len(oversized_jobs)} volume-split file(s)...",
            flush=True,
        )
    for idx, (rel_path, _size, chunk0_budget) in enumerate(oversized_jobs, 1):
        if _sigint_received:
            raise KeyboardInterrupt
        abs_path = source / rel_path
        scan_checksum = current_state[rel_path]
        ref = _verified_dedup_ref(
            rel_path, abs_path, scan_checksum, checksum_index,
            this_run_origins, current_state,
        )
        if ref is not None:
            dedup_refs.append(ref)
            _print_bar(idx, len(oversized_jobs), suffix=f"[dedup] {rel_path}")
            continue
        _require_disc_origin(rel_path, changed, disc_origin)
        budget0 = chunk0_budget if chunk0_budget > 0 else full_budget
        file_chunks = _prepare_volume_split_blobs(
            rel_path, abs_path, crypto, scratch_dir, session_n,
            disc_state.get(rel_path, ""), budget0, full_budget,
        )
        _report_read_checksum(
            rel_path, scan_checksum, file_chunks[0].result_checksum,
            current_state,
        )
        split_blobs.extend(file_chunks)
        _print_bar(
            idx, len(oversized_jobs),
            suffix=f"[split/{len(file_chunks)}] {rel_path}",
        )
        _log.info(
            "staged %s as volume-split (%d chunks)",
            rel_path, len(file_chunks),
        )
    if oversized_jobs:
        print()


def _resumable_split_prefixes(
    prefixes: "dict[str, SplitPrefix] | None", new_files: set[str],
) -> dict[str, SplitPrefix]:
    """
    Input: prefixes from the loaded manifests and this run's new paths.
    Output: prefixes whose interrupted file this run should tail-resume.
    Details:
        Only a path the diff calls new can resume. build_disc_state()
        drops an incomplete split from state, so the same path never
        appears as unchanged. The payload check in
        _prepare_tail_split_blobs() still guards a changed source.
    """
    if not prefixes:
        return {}
    return {path: prefixes[path] for path in new_files if path in prefixes}


def _stage_split_tail_jobs(
    prefixes: dict[str, SplitPrefix], source: Path,
    current_state: dict[str, str], crypto: CryptoBackend,
    scratch_dir: Path, session_n: int, first_budget: int, full_budget: int,
    split_blobs: list[_PreparedBlob],
) -> None:
    """
    Input: resumable prefixes, source state, budgets, and split collection.
    Output: the missing tail chunks appended in path order.
    """
    if not prefixes:
        return
    print(
        f"Resuming {len(prefixes)} interrupted volume split(s)...",
        flush=True,
    )
    total = len(prefixes)
    for idx, (rel_path, prefix) in enumerate(sorted(prefixes.items()), 1):
        if _sigint_received:
            raise KeyboardInterrupt
        blobs = _prepare_tail_split_blobs(
            rel_path, source / rel_path, crypto, scratch_dir, session_n,
            prefix, first_budget, full_budget,
        )
        _report_read_checksum(
            rel_path, current_state[rel_path], blobs[0].result_checksum,
            current_state,
        )
        split_blobs.extend(blobs)
        _print_bar(
            idx, total,
            suffix=f"[resume/{len(blobs)}] {rel_path}",
        )
        _log.info(
            "resumed %s: staged chunks %d..%d of %d",
            rel_path, blobs[0].volume_chunk, blobs[-1].volume_chunk,
            prefix.chunk_count,
        )
    print()


def _stage_new_files(
    new_paths: list[str], source: Path, current_state: dict[str, str],
    checksum_index: dict[str, _DedupTarget],
    this_run_origins: dict[str, str], crypto: CryptoBackend,
    scratch_dir: Path, session_n: int, prepared: list[_PreparedBlob],
    dedup_refs: list[_PreparedBlob],
) -> None:
    """
    Input: new paths, source state, and staging collections.
    Output: prepared blobs and dedup references appended in path order.
    """
    if new_paths:
        print(f"Staging {len(new_paths)} new file(s)...", flush=True)
    for idx, rel_path in enumerate(new_paths, 1):
        if _sigint_received:
            raise KeyboardInterrupt
        abs_path = source / rel_path
        scan_checksum = current_state[rel_path]
        if abs_path.stat().st_size > STREAM_CHUNK_SIZE:
            item = _verified_dedup_ref(
                rel_path, abs_path, scan_checksum, checksum_index,
                this_run_origins, current_state,
            )
            if item is None:
                item = _prepare_streamed_blob(
                    rel_path, abs_path, crypto, scratch_dir, session_n,
                )
                _report_read_checksum(
                    rel_path, scan_checksum, item.result_checksum,
                    current_state,
                )
        else:
            file_bytes = abs_path.read_bytes()
            read_checksum = hashlib.sha256(file_bytes).hexdigest()
            _report_read_checksum(
                rel_path, scan_checksum, read_checksum, current_state,
            )
            item = _dedup_ref(
                rel_path, read_checksum, len(file_bytes), checksum_index,
                this_run_origins,
            )
            if item is None:
                item = _prepare_encoded_blob(
                    rel_path, "full", file_bytes,
                    scratch_dir, session_n, read_checksum, len(file_bytes),
                    crypto=crypto,
                )
        if item.dedup_blob_path or item.dedup_origin_rel_path:
            dedup_refs.append(item)
            label = "dedup"
        else:
            prepared.append(item)
            this_run_origins.setdefault(item.result_checksum, rel_path)
            label = item.kind
        _print_bar(idx, len(new_paths), suffix=f"[{label}] {rel_path}")
        _log.info("staged new file %s as %s", rel_path, label)
    if new_paths:
        print()


def _read_changed_files(
    changed_paths: list[str], source: Path, current_state: dict[str, str],
    checksum_index: dict[str, _DedupTarget],
    this_run_origins: dict[str, str], changed: dict[str, str],
    disc_origin: dict[str, int], disc_state: dict[str, str],
    volume_first_session: int, crypto: CryptoBackend,
    scratch_dir: Path, session_n: int,
) -> tuple[dict[str, _PreparedBlob], list[tuple[str, str, bytes]]]:
    """
    Input: changed paths, source and disc state, and encoding context.
    Output: staged blobs and local changed files requiring prior blobs.
    """
    staged_changed: dict[str, _PreparedBlob] = {}
    local_changed: list[tuple[str, str, bytes]] = []
    for rel_path in changed_paths:
        if _sigint_received:
            raise KeyboardInterrupt
        abs_path = source / rel_path
        scan_checksum = current_state[rel_path]
        if abs_path.stat().st_size > STREAM_CHUNK_SIZE:
            ref = _verified_dedup_ref(
                rel_path, abs_path, scan_checksum, checksum_index,
                this_run_origins, current_state,
            )
            if ref is not None:
                staged_changed[rel_path] = ref
                continue
            _require_disc_origin(rel_path, changed, disc_origin)
            item = _prepare_streamed_blob(
                rel_path, abs_path, crypto, scratch_dir, session_n,
                source_checksum=disc_state[rel_path],
            )
            _report_read_checksum(
                rel_path, scan_checksum, item.result_checksum, current_state,
            )
            staged_changed[rel_path] = item
            continue
        new_bytes = abs_path.read_bytes()
        read_checksum = hashlib.sha256(new_bytes).hexdigest()
        _report_read_checksum(
            rel_path, scan_checksum, read_checksum, current_state,
        )
        ref = _dedup_ref(
            rel_path, read_checksum, len(new_bytes), checksum_index,
            this_run_origins,
        )
        if ref is not None:
            staged_changed[rel_path] = ref
            continue
        _require_disc_origin(rel_path, changed, disc_origin)
        if disc_origin[rel_path] < volume_first_session:
            staged_changed[rel_path] = _prepare_encoded_blob(
                rel_path, "full", new_bytes, scratch_dir,
                session_n, read_checksum, len(new_bytes),
                source_checksum=disc_state[rel_path], crypto=crypto,
            )
        else:
            local_changed.append((rel_path, read_checksum, new_bytes))
    return staged_changed, local_changed


def _collect_delta_jobs(
    local_changed: list[tuple[str, str, bytes]], cache: CacheManager,
    backend: BurnBackend, crypto: CryptoBackend,
    manifests: list[Manifest] | None, disc_origin: dict[str, int],
    disc_state: dict[str, str], staged_changed: dict[str, _PreparedBlob],
    scratch_dir: Path, session_n: int,
) -> tuple[list[tuple[bytes, bytes]], list[tuple[str, str, bytes, bytes]]]:
    """
    Input: local changes, prior blob sources, and staging state.
    Output: ordered delta jobs and their matching path metadata.
    """
    delta_jobs: list[tuple[bytes, bytes]] = []
    delta_meta: list[tuple[str, str, bytes, bytes]] = []
    for rel_path, read_checksum, new_bytes in local_changed:
        if _sigint_received:
            raise KeyboardInterrupt
        origin_session = disc_origin[rel_path]
        encrypted_old = cache.get_with_fallback(
            rel_path, origin_session, backend, crypto=crypto,
            manifests=manifests,
        )
        if len(encrypted_old) > STREAM_CHUNK_SIZE:
            staged_changed[rel_path] = _prepare_encoded_blob(
                rel_path, "full", new_bytes, scratch_dir,
                session_n, read_checksum, len(new_bytes),
                source_checksum=disc_state[rel_path], crypto=crypto,
            )
            continue
        prior_entry = next(
            (entry for manifest in manifests or [] if manifest.session == origin_session
             for entry in manifest.entries if entry.path == rel_path),
            None,
        )
        if is_bound_ciphertext(encrypted_old) and prior_entry is None:
            raise ValueError(f"Cannot resolve authenticated blob for {rel_path!r}.")
        old_bytes = decode_payload(
            crypto.decrypt(
                encrypted_old, aad=blob_aad(prior_entry) if is_bound_ciphertext(encrypted_old) else None,
            ),
            prior_entry.codec if prior_entry is not None else CODEC_NONE,
        )
        if hashlib.sha256(old_bytes).hexdigest() != disc_state[rel_path]:
            _log.warning(
                "cached prior version did not match for %s. Staging a full blob",
                rel_path,
            )
            staged_changed[rel_path] = _prepare_encoded_blob(
                rel_path, "full", new_bytes, scratch_dir,
                session_n, read_checksum, len(new_bytes),
                source_checksum=disc_state[rel_path], crypto=crypto,
            )
            continue
        delta_jobs.append((old_bytes, new_bytes))
        delta_meta.append((rel_path, read_checksum, old_bytes, new_bytes))
    return delta_jobs, delta_meta


def _encode_delta_jobs(
    delta_jobs: list[tuple[bytes, bytes]],
    delta_meta: list[tuple[str, str, bytes, bytes]],
    staged_changed: dict[str, _PreparedBlob], scratch_dir: Path,
    session_n: int, disc_state: dict[str, str], crypto: CryptoBackend,
) -> None:
    """
    Input: delta jobs, matching metadata, and staging context.
    Output: encoded changed blobs stored by relative path.
    """
    if delta_jobs:
        for meta, (kind, blob) in zip(delta_meta, process_bytes_files(delta_jobs)):
            rel_path, read_checksum, old_bytes, new_bytes = meta
            if kind == "delta" and not _delta_rebuilds(
                rel_path, old_bytes, blob, read_checksum,
            ):
                kind, blob = "full", new_bytes
            cache_full_path = None
            if kind == "delta":
                cache_full_path = scratch_dir / (
                    _blob_id(session_n, rel_path) + ".cache_full"
                )
                cache_full_path.write_bytes(crypto.encrypt(new_bytes))
            staged_changed[rel_path] = _prepare_encoded_blob(
                rel_path, kind, blob, scratch_dir, session_n,
                read_checksum, len(new_bytes),
                source_checksum=disc_state[rel_path],
                delta_size_bytes=len(blob) if kind == "delta" else 0,
                cache_full_path=cache_full_path, crypto=crypto,
            )


def _record_changed_blobs(
    changed_paths: list[str], staged_changed: dict[str, _PreparedBlob],
    prepared: list[_PreparedBlob], dedup_refs: list[_PreparedBlob],
) -> None:
    """
    Input: changed paths and their staged blobs.
    Output: prepared blobs and dedup references appended in path order.
    """
    for idx, rel_path in enumerate(changed_paths, 1):
        item = staged_changed[rel_path]
        if item.dedup_blob_path or item.dedup_origin_rel_path:
            dedup_refs.append(item)
            label = "dedup"
        else:
            prepared.append(item)
            label = item.kind
        _print_bar(idx, len(changed_paths), suffix=f"[{label}] {rel_path}")
        _log.info("staged %s as %s", rel_path, label)
    if changed_paths:
        print()


def _replacement_for_moved_blob(
    item: _PreparedBlob, source: Path, crypto: CryptoBackend,
    scratch_dir: Path, session_n: int, full_budget: int,
    current_state: dict[str, str],
) -> list[_PreparedBlob]:
    """
    Input: moved blob, source, crypto, scratch directory, budget, and state.
    Output: replacement full or volume-split blobs with checked checksum.
    """
    abs_path = source / item.rel_path
    if item.cache_full_path is not None:
        # A delta keeps the encrypted bytes that staging read.
        replacement = [_prepare_encoded_blob(
            item.rel_path, "full",
            crypto.decrypt(item.cache_full_path.read_bytes()),
            scratch_dir, session_n, item.result_checksum,
            item.full_size_bytes, source_checksum=item.source_checksum,
            crypto=crypto,
        )]
    elif _encoded_size_upper_bound(item.full_size_bytes, crypto) >= full_budget:
        replacement = _prepare_volume_split_blobs(
            item.rel_path, abs_path, crypto, scratch_dir, session_n,
            item.source_checksum, full_budget, full_budget,
        )
    elif item.full_size_bytes > STREAM_CHUNK_SIZE:
        replacement = [_prepare_streamed_blob(
            item.rel_path, abs_path, crypto, scratch_dir, session_n,
            item.source_checksum,
        )]
    else:
        file_bytes = abs_path.read_bytes()
        replacement = [_prepare_encoded_blob(
            item.rel_path, "full", file_bytes,
            scratch_dir, session_n,
            hashlib.sha256(file_bytes).hexdigest(), len(file_bytes),
            source_checksum=item.source_checksum, crypto=crypto,
        )]
    _report_read_checksum(
        item.rel_path, item.result_checksum,
        replacement[0].result_checksum, current_state,
    )
    return replacement


def _repack_moved_blobs(
    prepared: list[_PreparedBlob], bins: list[list[_PreparedBlob]],
    source: Path, crypto: CryptoBackend, scratch_dir: Path, session_n: int,
    full_budget: int, first_budget: int, current_state: dict[str, str],
    image_costs: bool, ordinary_first: bool = False,
    convert_first_bin: bool = False,
) -> tuple[list[_PreparedBlob], list[list[_PreparedBlob]]]:
    """
    Input: prepared blobs, packed bins, encoding context, and budgets.
    Output: prepared blobs and bins after no moved blob needs conversion.
    """
    while True:
        conversions: dict[int, list[_PreparedBlob]] = {}
        for bin_index, bin_blobs in enumerate(bins):
            if bin_index == 0 and not convert_first_bin:
                continue
            for item in bin_blobs:
                if item.kind != "delta" and not item.dedup_blob_path:
                    continue
                replacement = _replacement_for_moved_blob(
                    item, source, crypto, scratch_dir, session_n,
                    full_budget, current_state,
                )
                conversions[id(item)] = replacement
        if not conversions:
            break
        prepared = [
            replacement
            for item in prepared
            for replacement in conversions.get(id(item), [item])
        ]
        _stamp_on_disc_overhead(prepared, crypto, image_costs)
        bins = _pack_blobs(
            prepared, first_budget, full_budget, ordinary_first=ordinary_first,
        )
    return prepared, bins



def _skip_empty_current_bin(
    bins: list[list[_PreparedBlob]], session_n: int, fill_pending: bool,
) -> tuple[list[list[_PreparedBlob]], bool]:
    """
    Input:  packed bins, first session number, and whether a fill disc
            was chosen for bin 0
    Output: bins to write, and whether the new first bin is a new volume
    Details:
        A bin 0 with no blobs means nothing fits the loaded medium.
        Burning it would append an empty session to that disc, so the
        caller skips the medium and burns the first real bin on the next
        one. Session numbers start over at session_n, so the archive has
        no session gap. A fill run keeps its explicit disc choice.
    """
    if (
        fill_pending or session_n <= 0 or len(bins) < 2 or bins[0]
    ):
        return bins, False
    skipped = len(bins) - 1
    _log.info(
        "Nothing fits the loaded disc; skipping its empty session and "
        "burning %d session(s) from %03d on the next volume",
        skipped, session_n,
    )
    print(
        "Nothing fits the inserted disc; skipping its empty session.",
        flush=True,
    )
    return bins[1:], True


def _write_staging_volumes(
    bins: list[list[_PreparedBlob]], staging: Path, root: Path,
    session_n: int, deleted: list[str], source: Path, crypto: CryptoBackend,
    created: list[Path], first_is_new_volume: bool = False,
) -> list[tuple[Path, int, bool]]:
    """
    Input: packed bins, staging roots, deletions, source, crypto, and
           the caller's created-path list.
    Output: (staging_path, session_n, is_new_volume) for each bin.
    Details:
        Bins map to sessions in order. A non-empty first bin that must
        skip the loaded medium passes first_is_new_volume=True, so its
        session burns on the next disc after the swap.
    """
    result: list[tuple[Path, int, bool]] = []
    written_blob_paths: dict[str, str] = {}
    written_blob_origins: dict[str, int | None] = {}
    for i, bin_blobs in enumerate(bins):
        chunk_session = session_n + i
        is_new_volume = i > 0 or first_is_new_volume
        if i == 0:
            chunk_staging = staging
        else:
            chunk_staging = _ensure_staging_dir(root, chunk_session)
            created.append(chunk_staging)
            sidecar = chunk_staging.with_name(chunk_staging.name + ".cache_full")
            created.append(sidecar)
            if not remove_tree(sidecar, "old delta cache sidecar"):
                raise RuntimeError(f"Cannot remove old delta cache sidecar: {sidecar}")
        chunk_deleted = deleted if i == 0 else []
        _write_chunk(
            chunk_staging, chunk_session, bin_blobs, chunk_deleted, source,
            crypto, written_blob_paths, written_blob_origins,
        )
        used = _staging_bytes(chunk_staging)
        _log.info(
            "Staged %s: %d file(s), %d bytes%s",
            f"session_{chunk_session:03d}",
            len(bin_blobs),
            used,
            " (new volume)" if is_new_volume else "",
        )
        result.append((chunk_staging, chunk_session, is_new_volume))
    return result


def _disc_changes(
    current_state: dict[str, str], disc_state: dict[str, str],
) -> tuple[dict[str, str], set[str], list[str]]:
    """
    Input: current source checksums and disc checksums.
    Output: changed paths, new paths, and deleted paths in disc order.
    """
    changed = {
        p: cs for p, cs in current_state.items()
        if p in disc_state and cs != disc_state[p]
    }
    new_files = {p for p in current_state if p not in disc_state}
    deleted = [p for p in disc_state if p not in current_state]
    return changed, new_files, deleted


def _report_staged_changes(
    session_n: int, result: list[tuple[Path, int, bool]],
    new_files: set[str], changed: dict[str, str], deleted: list[str],
    remaining_bytes: int,
) -> None:
    """
    Input: first session, staged volumes, change sets, and remaining bytes.
    Output: space and staged-session summaries printed and logged.
    """
    if len(result) == 1 and not result[0][2]:
        used = _staging_bytes(result[0][0])
        pct = 100.0 * used / remaining_bytes if remaining_bytes else 0
        print(f"  OK: session is {pct:.1f}% of remaining space.", flush=True)
        _log.info(
            "Space check OK: staging %d bytes, remaining %d bytes",
            used, remaining_bytes,
        )

    _log.info(
        "Staged session %03d%s: %d new, %d changed, %d deleted",
        session_n,
        f"-{session_n + len(result) - 1:03d}" if len(result) > 1 else "",
        len(new_files), len(changed), len(deleted),
    )


def _stage_embedded_software(
    staging: Path, backend: BurnBackend, embed_software: str | None,
    session_n: int,
) -> int:
    """
    Input: first staging root, backend, software mode, and session number.
    Output: byte size of the staged software bundle, or 0 when disabled.
    """
    bundle_bytes = 0
    if embed_software:
        from oddarchiver.software_bundle import stage_software_bundle
        bundle_bytes = stage_software_bundle(
            staging, backend, embed_software, session_n,
        )
    return bundle_bytes


def _prefetch_origin_blobs(
    cache: CacheManager, local_changed: list[tuple[str, str, bytes]],
    disc_origin: dict[str, int], backend: BurnBackend,
    manifests: list[Manifest] | None, crypto: CryptoBackend,
) -> None:
    """
    Input: cache, local changed files, origin sessions, and read context.
    Output: prior blobs prefetched when the cache supports it.
    """
    prefetch = getattr(cache, "prefetch_origin_blobs", None)
    if prefetch is not None and local_changed:
        prefetch(
            [(rel, disc_origin[rel]) for rel, _, _ in local_changed],
            backend,
            manifests=manifests,
            crypto=crypto,
        )


def _pack_staged_blobs(
    prepared: list[_PreparedBlob], split_blobs: list[_PreparedBlob],
    dedup_refs: list[_PreparedBlob], crypto: CryptoBackend,
    image_costs: bool, first_budget: int, full_budget: int,
    ordinary_first: bool = False,
) -> tuple[list[_PreparedBlob], list[list[_PreparedBlob]]]:
    """
    Input: staged blobs, split blobs, dedup references, costs, and budgets.
    Output: ordered prepared blobs and their first packing bins.
    """
    if dedup_refs:
        print(
            f"Reusing existing blobs for {len(dedup_refs)} duplicate file(s).",
            flush=True,
        )

    if _sigint_received:
        raise KeyboardInterrupt

    prepared.extend(dedup_refs)
    prepared.sort(key=lambda item: item.rel_path)
    prepared = split_blobs + prepared
    _stamp_on_disc_overhead(prepared, crypto, image_costs)
    bins = _pack_blobs(
        prepared, first_budget, full_budget, ordinary_first=ordinary_first,
    )
    return prepared, bins


def _apply_fill_choice_to_staging(
    fill_targets: "list[FillTarget] | None",
    fill_chosen: "list[int] | None",
    inserted_volume_index: int | None,
    prepared: list,
    split_blobs: list,
    dedup_refs: list,
    session_n: int,
    bundle_bytes: int,
    source: Path,
    crypto: "CryptoBackend",
    scratch_dir: Path,
    full_budget: int,
    current_state: dict[str, str],
    image_costs: object,
    first_budget: int,
    disc_info: object,
) -> tuple[list, list, list, int, bool, object]:
    """Input: staging blobs and fill choice. Output: updated blobs and disc info."""
    prepared, split_blobs, dedup_refs, first_budget, convert_first = (
        _apply_fill_target_choice(
            fill_targets, fill_chosen, inserted_volume_index,
            prepared, split_blobs, dedup_refs, session_n, bundle_bytes,
            source, crypto, scratch_dir, full_budget, current_state,
            image_costs, first_budget,
        )
    )
    if fill_chosen and fill_targets:
        chosen_index = fill_chosen[-1]
        for target in fill_targets:
            if target.volume_index == chosen_index:
                disc_info = target.disc_info
                break
    return (
        prepared, split_blobs, dedup_refs, first_budget, convert_first, disc_info,
    )


def build_staging(
    session_n: int,
    source: Path,
    disc_state: dict[str, str],
    disc_origin: dict[str, int],
    backend: "BurnBackend",
    cache: "CacheManager",
    crypto: "CryptoBackend",
    _staging_root: Path | None = None,
    _current_state: dict[str, str] | None = None,
    manifests: "list[Manifest] | None" = None,
    volume_first_session: int = 0,
    embed_software: str | None = None,
    volume_baseline: VolumeBaseline | None = None,
    volume_marker: VolumeMarker | None = None,
    volume_label: str = "",
    split_large: bool = False,
    fill_targets: list[FillTarget] | None = None,
    fill_chosen: list[int] | None = None,
    inserted_volume_index: int | None = None,
    split_prefixes: "dict[str, SplitPrefix] | None" = None,
) -> list[tuple[Path, int, bool]]:
    """
    Input:  session_n      - first session number to build (e.g. 1 for first sync)
            source         - directory being archived
            disc_state     - {path: result_checksum} from manifest.build_disc_state()
            disc_origin    - {path: session} from manifest.build_disc_origin(). The
                              session that last wrote each path currently in
                              disc_state, used to fetch the prior blob
                              of a changed file from its true origin session
                              rather than session_n - 1
            backend        - BurnBackend for disc reads on cache miss and for
                              remaining / full-medium capacity
            cache          - CacheManager for encrypted blob retrieval
            crypto         - CryptoBackend for encrypt/decrypt
            _staging_root  - override staging root (default: _default_staging_root)
            _current_state - pre-computed {path: sha256} to skip re-scanning source
            manifests      - loaded manifests for cache fallback and dedup
            volume_first_session - first session on the loaded disc
            embed_software - None, "minimal", or "all". None writes no software
                              bundle. See oddarchiver.software_bundle.
            volume_baseline, volume_marker, volume_label - closing disc
                              identity used to reserve baseline space on
                              each new disc
            split_large    - True splits a file that fits one medium so
                              leftover space on the current disc is filled
            fill_targets   - measured appendable volumes, oldest first
            fill_chosen    - receives the volume_index selected for bin 0
            inserted_volume_index - volume currently in the backend
            split_prefixes - {path: SplitPrefix} from
                              manifest.build_split_prefixes(). Each entry
                              is a clean chunk prefix already burned, so
                              the run stages only the missing tail. None
                              restages every oversized file whole.
    Output: list of (staging_path, session_n, is_new_volume) tuples
    Details:
        Encodes each changed or new file before packing it. A path can
        reuse a full blob on the current disc. A reference uses no space
        until packing moves it to a new disc. In that case, the code
        writes a full blob and packs again. It also converts a delta
        when packing puts it on a disc without its previous version.
        This repeat step accounts for the larger full blob.
        Each staged file uses the checksum of the bytes that staging read
        and encoded, not the scan checksum. A small file is read once into
        memory. A large file is hashed by the same read that encrypts it.
        When a file changed after the scan, the code logs a warning,
        prints one stderr line, sets current_state[rel_path] to the new
        checksum, and continues. Dedup lookups use the checksum of the
        bytes read. A large file whose scan checksum names a dedup target
        is hashed first, and is encoded only on a miss. A delta that does
        not rebuild the bytes read becomes a full blob of those bytes.
        The first chunk budget is remaining_bytes * SPACE_SAFETY_MARGIN
        on the current medium, minus MERGE_OVERHEAD_PER_PRIOR_FILE_BYTES
        for every path already in disc_state when session_n > 0 (appending
        onto an existing session chain costs real space to rebuild the
        cumulative directory tree of the disc. A fresh medium at
        session_n == 0 has no prior session to merge with, so the code
        deducts nothing there), and minus the ISO scaffolding of a
        medium that holds no image yet. Every later chunk uses a full
        fresh medium (backend.disc_size when that is a positive int,
        else BDXL_DISC_BYTES) minus the ISO scaffolding and the encoded
        baseline bound, then times SPACE_SAFETY_MARGIN, with no
        merge-overhead deduction, since every chunk after the first is
        always a fresh -Z init, never a -M append. Each packed file
        also pays its own on-disc cost: block rounding, UDF and Rock
        Ridge records, and one manifest entry. The baseline bound is
        the exact closing disc baseline plus one measured event per
        pending path plus the volume records of this pack. A changeset
        that fits returns exactly one tuple with is_new_volume=False. A
        split returns 2+ tuples. The first tuple has is_new_volume=False.
        Every later tuple has True, with contiguous session numbers.
        Combined file content covers each changed/new file exactly once.
        A file splits only when its encoded payload exceeds
        full_budget or BLOB_SIZE_CAP. With split_large, a file that
        fits one medium can still split to fill leftover space on the
        current disc. The code range-encrypts a split file into volume
        chunks (volume_chunk / volume_chunk_count on each ManifestEntry)
        instead of raising. The code computes the disc budgets before
        any encrypt_file call, so the code chunks an oversized file in
        one pass.
        When split_prefixes names a file whose chunks 0..k already sit
        on disc, the code rebuilds the payload, checks it against the
        recorded result checksum, original size, and codec, and stages
        only chunks k+1..n-1 from the summed offset of the burned
        chunks. A changed source falls back to the whole-file restage.
        An incomplete split is never reported as up to date.
        When packing places nothing on the loaded medium, the code
        drops that empty bin, renumbers the remaining sessions from
        session_n, and marks the first one as a new volume. The burn
        then swaps media before its first session instead of appending
        an empty one.
        Uses a deterministic named staging dir so the next run can
        detect and remove a dir left by a crash. Before removing an existing
        dir, checks a PID lockfile via _check_staging_lock(): if the PID
        that created it is still alive, raises RuntimeError instead of
        deleting live data out from under another in-flight build (see
        Assist/HANDOVER_disc_sync_bug.md).
        The source scan excludes the staging tree itself (including that
        lockfile) if staging happens to be nested inside source. Example:
        the home-directory fallback staging root when syncing a source
        tree that contains the home directory of the user. The code then
        never mistakes staging bookkeeping for archive content.
        The code encrypts files larger than STREAM_CHUNK_SIZE (and every
        volume-split chunk) to a scratch directory under the staging root
        via encrypt_file, then moves them into the packed session
        directory. The code removes scratch on every exit path.
        Installs a SIGINT handler at entry. The handler only sets a flag.
        Raises SystemExit(1) if the staging filesystem space check fails.
        On BaseException the code removes every staging dir created by
        this call before re-raising.
        The code fetches the prior blob of each changed file from
        disc_origin[rel_path] (its true last-written session), not
        session_n - 1. The code does not restage a file left unchanged
        across intervening syncs, so its blob may live several sessions
        further back than the immediately prior one.
        When embed_software is set, the first staging root also receives a
        plaintext oddarchiver-software/ directory. Its byte size is deducted
        from first_budget. crypto.encrypt is never used for those files.
    """
    global _sigint_received
    _sigint_received = False
    old_sigint = signal.signal(signal.SIGINT, _handle_sigint)

    root = _staging_root if _staging_root is not None else _default_staging_root()
    created: list[Path] = []
    try:
        staging, scratch_dir = _open_staging_workspace(root, session_n, created)

        bundle_bytes = _stage_embedded_software(
            staging, backend, embed_software, session_n,
        )

        # Scan source (skipped if caller pre-computed current_state)
        current_state = _scan_staging_source(source, staging, _current_state)

        # Diff current source hashes against disc_state
        changed, new_files, deleted = _disc_changes(current_state, disc_state)

        # Dedup decisions wait until staging reads each file. The key is
        # the checksum of the bytes read, not the scan checksum.
        checksum_index = _build_checksum_index(
            disc_state, disc_origin, manifests, volume_first_session,
        )

        # A path whose newest entries are chunks 0..k of an interrupted
        # volume split is absent from disc_state. Stage only the missing
        # tail so the burned chunks are never re-encoded or abandoned.
        resumable = _resumable_split_prefixes(split_prefixes, new_files)
        staged_new = new_files - set(resumable)

        if _current_state is None:
            # Only reprint the summary when the caller has not already shown it.
            unchanged = len(current_state) - len(new_files) - len(changed)
            print(
                f"  {len(new_files)} new, {len(changed)} changed, "
                f"{len(deleted)} deleted, {unchanged} unchanged",
                flush=True,
            )

        # Staging-filesystem space check, separate from the disc/ISO capacity
        # packing further below. Staging commonly lives on a small, RAM-backed
        # tmpfs (see _default_staging_root / XDG_RUNTIME_DIR), which can run
        # out long before the real disk or the target disc/ISO does. Uses a
        # worst-case estimate: source size for every new or changed file that
        # still needs encoding. A file whose scan checksum names a blob on
        # disc is expected to become a dedup reference, which costs zero
        # staging bytes. Extra same-sync copies of a new-file checksum are
        # also excluded. The actual delta-vs-full size of a changed file
        # stays unknown until the code diffs it. This check fails fast,
        # before any code reads a file for a blob or stages it, instead of
        # dying mid-job after hours of work.
        _check_staging_space(source, staging, new_files, changed, current_state, checksum_index)

        print("Checking available disc space...", flush=True)
        disc_info = backend.mediainfo()
        # ISO9660 multisession append (session_n > 0, growisofs -M) rebuilds
        # the whole cumulative directory tree of the disc, so it costs real space
        # beyond the new files' own bytes. A fresh medium (session_n == 0,
        # growisofs -Z) has no prior session to merge with, so no deduction
        # applies there. See MERGE_OVERHEAD_PER_PRIOR_FILE_BYTES above.
        first_budget, merge_overhead, effective_remaining = _first_pack_budget(
            disc_info, session_n, disc_state, bundle_bytes,
        )
        full_medium, full_budget, image_costs = _full_pack_budget(
            backend, crypto, volume_baseline, manifests, volume_marker,
            volume_label, new_files, changed, deleted, current_state,
        )
        _log.info(
            "Packing budget: remaining=%d merge_overhead=%d "
            "effective_remaining=%d first_budget=%d full_medium=%d "
            "full_budget=%d safety_margin=%.2f prior_files=%d",
            disc_info.remaining_bytes,
            merge_overhead,
            effective_remaining,
            first_budget,
            full_medium,
            full_budget,
            SPACE_SAFETY_MARGIN,
            len(disc_state) if session_n > 0 else 0,
        )

        prepared: list[_PreparedBlob] = []
        split_blobs: list[_PreparedBlob] = []
        dedup_refs: list[_PreparedBlob] = []
        # {read checksum: rel_path} for each complete full blob of a new file
        # in this run. Only the bytes that staging read give a key.
        this_run_origins: dict[str, str] = {}
        oversized_jobs, new_paths, changed_paths, ordinary_first = (
            _classify_staging_paths(
                source, staged_new, changed, crypto, image_costs, full_budget,
                first_budget, split_large,
            )
        )

        _stage_split_tail_jobs(
            resumable, source, current_state, crypto, scratch_dir, session_n,
            first_budget, full_budget, split_blobs,
        )

        oversized_jobs.sort(key=lambda job: (-job[1], job[0]))
        _stage_split_jobs(
            oversized_jobs, source, current_state, checksum_index,
            this_run_origins, changed, disc_origin, disc_state, crypto,
            scratch_dir, session_n, full_budget, split_blobs,
            dedup_refs,
        )

        # Encode each new file from the bytes that one read returns. New
        # files go first so that a changed file can share their blobs.
        _stage_new_files(
            new_paths, source, current_state, checksum_index,
            this_run_origins, crypto, scratch_dir, session_n, prepared,
            dedup_refs,
        )

        # Encode each changed file from the bytes that one read returns.
        if changed_paths:
            print(f"Staging {len(changed_paths)} changed file(s)...", flush=True)
        staged_changed, local_changed = _read_changed_files(
            changed_paths, source, current_state, checksum_index,
            this_run_origins, changed, disc_origin, disc_state,
            volume_first_session, crypto, scratch_dir, session_n,
        )

        _prefetch_origin_blobs(
            cache, local_changed, disc_origin, backend, manifests, crypto,
        )

        delta_jobs, delta_meta = _collect_delta_jobs(
            local_changed, cache, backend, crypto, manifests, disc_origin,
            disc_state, staged_changed, scratch_dir, session_n,
        )

        _encode_delta_jobs(
            delta_jobs, delta_meta, staged_changed, scratch_dir, session_n,
            disc_state, crypto,
        )

        _record_changed_blobs(
            changed_paths, staged_changed, prepared, dedup_refs,
        )

        (
            prepared, split_blobs, dedup_refs, first_budget, convert_first,
            disc_info,
        ) = _apply_fill_choice_to_staging(
            fill_targets, fill_chosen, inserted_volume_index,
            prepared, split_blobs, dedup_refs, session_n, bundle_bytes,
            source, crypto, scratch_dir, full_budget, current_state,
            image_costs, first_budget, disc_info,
        )
        prepared, bins = _pack_staged_blobs(
            prepared, split_blobs, dedup_refs, crypto, image_costs,
            first_budget, full_budget, ordinary_first=ordinary_first,
        )
        # A converted full blob can move other blobs to the next disc.
        # Pack again until each new disc holds its own blobs and delta bases.
        prepared, bins = _repack_moved_blobs(
            prepared, bins, source, crypto, scratch_dir, session_n,
            full_budget, first_budget, current_state, image_costs,
            ordinary_first=ordinary_first,
            convert_first_bin=convert_first,
        )


        # A pack that places nothing on the loaded disc would append an
        # empty session there. Skip that disc instead and burn the first
        # real session on the next volume.
        bins, first_is_new_volume = _skip_empty_current_bin(
            bins, session_n, bool(fill_chosen),
        )

        if len(bins) > 1:
            _log.info(
                "Changeset split into %d session(s) starting at %03d",
                len(bins), session_n,
            )
            print(f"  Changeset packed into {len(bins)} volume(s).", flush=True)

        result = _write_staging_volumes(
            bins, staging, root, session_n, deleted, source, crypto, created,
            first_is_new_volume=first_is_new_volume,
        )

        _report_staged_changes(
            session_n, result, staged_new, changed, deleted,
            disc_info.remaining_bytes,
        )
        remove_tree(scratch_dir, "staging scratch")
        return result

    except BaseException:
        _log.exception("Failed to build session %03d staging", session_n)
        for path in created:
            remove_tree(path, "staging directory")
        raise
    finally:
        signal.signal(signal.SIGINT, old_sigint)


if __name__ == "__main__":
    pass
