"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Staging directory construction. Orchestrates scan, diff, delta/full
    encoding, greedy volume packing, and SIGINT-safe cleanup.
"""
# Imports
from __future__ import annotations

import datetime
import hashlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.crypto import STREAM_CHUNK_SIZE, NullCrypto
from oddarchiver.delta import process_files
from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.cache import CacheManager
    from oddarchiver.crypto import CryptoBackend

# Globals
SPACE_SAFETY_MARGIN = 0.95

# growisofs -M appends a new session by handing genisoimage/mkisofs the
# previous session as a merge source (-C last,next -M /dev/fd/N). An
# ISO9660 multisession append rebuilds the entire cumulative directory
# tree (path table, directory records, and one full UDF File Entry
# logical block per file) for every file already on the disc, not only
# the files being added now. The file data extents are different: they
# are reused by reference and never recopied. A packing budget that
# only sums new/changed file bytes undercounts this. The shortfall
# grows with the disc's total prior file count. Direct reproduction
# confirmed and calibrated this: appending 3085 new files onto a real
# UDF+RockRidge image already holding 8000 files needed 19.9 MB more
# than a fresh, non-merged build of the same 3085 files alone (about
# 2.5 KB of extra overhead per prior file). This constant is that
# measurement rounded up to 8192 bytes/file. The margin covers longer
# filenames, deeper paths, and other genisoimage/mkisofs versions.
MERGE_OVERHEAD_PER_PRIOR_FILE_BYTES = 8192
_log = logging.getLogger(__name__)


@dataclass
class _PreparedBlob:
    """Encoded on-disc blob plus the manifest fields needed to write it."""
    rel_path: str
    kind: str
    blob: bytes
    result_checksum: str
    full_size_bytes: int
    encoded_size: int
    source_checksum: str = ""
    delta_size_bytes: int = 0
    streamed: bool = False
    blob_path: Path | None = None
    volume_chunk: int | None = None
    volume_chunk_count: int | None = None


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
    Input:  session_n - session index
            rel_path  - relative source path
    Output: 64-char hex string used as the on-disc blob filename
    Details:
        sha256 of "session_n:rel_path". Deterministic and opaque.
        Stored flat in full/ or deltas/ with no extension so the disc
        directory tree does not show original file names or layout.
    """
    import hashlib
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


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
    label = suffix[:40].ljust(40)
    print(f"\r  [{bar}] {current}/{total}  {label}", end="", flush=True)


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


def _sha256_file(path: Path) -> str:
    """
    Input:  path - file to hash
    Output: hex sha256 digest of file contents
    Details:
        Reads the file in STREAM_CHUNK_SIZE chunks so a large file is
        never fully materialized in memory.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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
        deletes the other's in-flight files mid-burn. Reads a PID from
        staging/.lock (written by build_staging on every successful
        start) and probes it with os.kill(pid, 0):
          - No lockfile, or unreadable/non-integer contents. This is a
            leftover from an old oddarchiver version or a crash before
            the lock was written. It is stale. The caller may remove
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
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except OSError as exc:
        _log.warning("Could not probe staging lock pid %d for %s: %s", pid, staging, exc)
    raise RuntimeError(
        f"Staging directory for session {session_n:03d} ({staging}) is in use "
        f"by pid {pid}; refusing to remove it. If that process is no longer "
        f"running, remove {staging} manually and retry."
    )


def _full_medium_bytes(backend: "BurnBackend") -> int:
    """
    Input:  backend - burn backend whose fresh-medium capacity is needed
    Output: int - bytes of a blank medium
    Details:
        ISOBackend stores the configured --disc-size on .disc_size. That
        value is used when it is a positive int. DiscBackend and anything
        else (including mocks without an int disc_size) fall back to
        BDXL_DISC_BYTES, the largest BD medium this tool targets.
    """
    disc_size = getattr(backend, "disc_size", None)
    if isinstance(disc_size, int) and disc_size > 0:
        return disc_size
    from oddarchiver.disc import BDXL_DISC_BYTES
    return BDXL_DISC_BYTES


def _volume_budget(medium_bytes: int) -> int:
    """
    Input:  medium_bytes - remaining or full-medium capacity in bytes
    Output: int - packable budget after SPACE_SAFETY_MARGIN
    """
    return int(medium_bytes * SPACE_SAFETY_MARGIN)


_STREAM_HEADER_MAX = 60  # KeyfileCrypto kek_nonce(12) + enc_dek(48)
_STREAM_AEAD_OVERHEAD = 28  # nonce(12) + tag(16) per STREAM_CHUNK_SIZE chunk


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
    return plaintext_size + _STREAM_HEADER_MAX + nchunks * _STREAM_AEAD_OVERHEAD


def _volume_split_lengths(
    plaintext_size: int,
    first_chunk_budget: int,
    full_budget: int,
) -> list[int]:
    """
    Input:  plaintext_size     - whole-file size in bytes
            first_chunk_budget - budget active for chunk 0
            full_budget        - budget for every later chunk
    Output: list of plaintext lengths, one per volume chunk
    Details:
        Chunk 0 is min(remaining, first_chunk_budget) when that budget
        is positive, otherwise min(remaining, full_budget). Chunks
        1..N-2 are exactly full_budget. The last chunk takes the
        remainder. A zero full_budget cannot split a file.
    """
    if full_budget <= 0:
        raise RuntimeError(
            f"Cannot split a file: full volume budget is {full_budget} bytes"
        )
    budget = first_chunk_budget if first_chunk_budget > 0 else full_budget
    lengths: list[int] = []
    remaining = plaintext_size
    while remaining > 0:
        n = min(remaining, budget)
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


def _pack_blobs(
    blobs: list[_PreparedBlob],
    first_budget: int,
    full_budget: int,
) -> list[list[_PreparedBlob]]:
    """
    Input:  blobs        - encoded blobs (ordinary and optional volume-split)
            first_budget - remaining_bytes * SPACE_SAFETY_MARGIN on the current medium
            full_budget  - fresh-medium bytes * SPACE_SAFETY_MARGIN
    Output: list of bins; each bin is the blobs for one session directory
    Details:
        Volume-split blobs (volume_chunk is not None) each get their own
        dedicated bin, grouped by rel_path in first-seen order, then by
        ascending volume_chunk. Dedicated bins never share with another
        file or another chunk of the same file, so chunk N cannot land
        in an earlier bin than chunk N-1 even when encoded sizes tie.
        If first_budget is 0 or negative, an empty first bin is kept so
        the current medium is skipped. Ordinary blobs then pack with
        full_budget after the split file's bins.
        Ordinary blobs still use greedy largest-first fill (ties broken
        by rel_path). A non-split blob larger than full_budget raises
        RuntimeError naming that file. An empty first bin is kept when
        nothing fits the current medium's remaining space.
    """
    if not blobs:
        return [[]]

    split_items = [b for b in blobs if b.volume_chunk is not None]
    ordinary = [b for b in blobs if b.volume_chunk is None]

    for item in ordinary:
        size = item.encoded_size
        if size >= full_budget:
            raise _blob_too_large_error(item.rel_path, size, full_budget)

    groups: dict[str, list[_PreparedBlob]] = {}
    group_order: list[str] = []
    for item in split_items:
        if item.rel_path not in groups:
            group_order.append(item.rel_path)
            groups[item.rel_path] = []
        groups[item.rel_path].append(item)
    for rel_path in group_order:
        groups[rel_path].sort(key=lambda b: b.volume_chunk or 0)

    chunks: list[list[_PreparedBlob]] = []
    if group_order:
        if first_budget <= 0:
            chunks.append([])
        for rel_path in group_order:
            for item in groups[rel_path]:
                chunks.append([item])
        ordinary_budget = full_budget
    else:
        ordinary_budget = first_budget

    if not ordinary:
        return chunks if chunks else [[]]

    remaining = sorted(ordinary, key=lambda b: (-b.encoded_size, b.rel_path))
    budget = ordinary_budget
    while remaining:
        bin_items: list[_PreparedBlob] = []
        deferred: list[_PreparedBlob] = []
        used = 0
        for item in remaining:
            size = item.encoded_size
            if used + size < budget:
                bin_items.append(item)
                used += size
            else:
                deferred.append(item)
        if not bin_items:
            chunks.append([])
            budget = full_budget
            continue
        chunks.append(bin_items)
        remaining = deferred
        budget = full_budget
    return chunks



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


def partition_by_staging_budget(
    paths: set[str],
    source: Path,
    staging_root: Path | None = None,
) -> list[list[str]]:
    """
    Input:  paths        - source-relative new/changed paths to partition
            source       - directory being archived
            staging_root - override staging root (default: _default_staging_root())
    Output: list of groups; each group is a sorted list of source-relative paths
    Details:
        Greedily bin-packs paths, sorted for determinism, by
        (source / p).stat().st_size, into groups whose running total
        stays under shutil.disk_usage(staging_root or
        _default_staging_root()).free * SPACE_SAFETY_MARGIN. An empty
        input returns [[]]. A changeset that fits in one group returns
        [sorted(paths)]. That is indistinguishable from the ungrouped
        path this replaces: no split, one session, unchanged output.
        The staging budget is fixed across groups. This differs from
        _pack_blobs' disc-capacity chunks, where a fresh medium grants
        fresh headroom on every later chunk. A single path whose own
        size exceeds the whole budget therefore raises RuntimeError
        naming that file, rather than ever being routed to a group of
        its own.
    """
    if not paths:
        return [[]]

    root = staging_root if staging_root is not None else _default_staging_root()
    budget = shutil.disk_usage(root).free * SPACE_SAFETY_MARGIN

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


def _write_chunk(
    staging: Path,
    session_n: int,
    blobs: list[_PreparedBlob],
    deleted: list[str],
    source: Path,
) -> None:
    """
    Input:  staging   - staging directory root for this chunk
            session_n - session number written into session_NNN/
            blobs     - encoded blobs that belong in this session
            deleted   - paths removed in this session (empty on later volumes)
            source    - archive source directory, stored on the manifest
    Output: None
    Details:
        Writes full/ and deltas/ blobs, then the plaintext session manifest.
        Checks the SIGINT flag between files.
    """
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
        blob_id = _blob_id(session_n, item.rel_path)
        if item.kind == "delta":
            dest = deltas_dir / blob_id
        else:
            dest = full_dir / blob_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        if item.streamed:
            if item.blob_path is None:
                raise RuntimeError(
                    f"streamed blob {item.rel_path!r} has no blob_path"
                )
            shutil.move(str(item.blob_path), str(dest))
        else:
            dest.write_bytes(item.blob)
        entries.append(ManifestEntry(
            path=item.rel_path,
            type=item.kind,
            result_checksum=item.result_checksum,
            full_size_bytes=item.full_size_bytes,
            source_checksum=item.source_checksum,
            delta_file=f"{session_name}/deltas/{blob_id}" if item.kind == "delta" else "",
            delta_size_bytes=item.delta_size_bytes,
            file=f"{session_name}/full/{blob_id}" if item.kind == "full" else "",
            streamed=item.streamed,
            volume_chunk=item.volume_chunk,
            volume_chunk_count=item.volume_chunk_count,
        ))

    if _sigint_received:
        raise KeyboardInterrupt

    manifest = Manifest(
        version=1,
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
    write_manifest(session_dir, manifest)

def _prepare_streamed_blob(
    rel_path: str,
    abs_path: Path,
    crypto: "CryptoBackend",
    scratch_dir: Path,
    session_n: int,
    result_checksum: str,
    source_checksum: str = "",
) -> _PreparedBlob:
    """
    Input:  rel_path, abs_path - source identity
            crypto, scratch_dir, session_n - encode destination
            result_checksum, source_checksum - manifest fields
    Output: _PreparedBlob with streamed=True and blob_path set
    Details:
        Encrypts abs_path to a scratch file via encrypt_file. blob stays
        empty; encoded_size is the on-disk ciphertext size.
    """
    scratch_path = scratch_dir / _blob_id(session_n, rel_path)
    crypto.encrypt_file(abs_path, scratch_path)
    return _PreparedBlob(
        rel_path=rel_path,
        kind="full",
        blob=b"",
        result_checksum=result_checksum,
        full_size_bytes=abs_path.stat().st_size,
        encoded_size=scratch_path.stat().st_size,
        source_checksum=source_checksum,
        streamed=True,
        blob_path=scratch_path,
    )

def _prepare_volume_split_blobs(
    rel_path: str,
    abs_path: Path,
    crypto: "CryptoBackend",
    scratch_dir: Path,
    session_n: int,
    result_checksum: str,
    source_checksum: str,
    first_chunk_budget: int,
    full_budget: int,
) -> list[_PreparedBlob]:
    """
    Input:  rel_path, abs_path - source identity
            crypto, scratch_dir, session_n - encode destination
            result_checksum, source_checksum - whole-file manifest fields
            first_chunk_budget, full_budget - plaintext split sizes
    Output: one streamed _PreparedBlob per volume chunk
    Details:
        Range-encrypts abs_path once per chunk via encrypt_file(offset,
        length). Each blob is independently decryptable. full_size_bytes
        and result_checksum are the original whole file on every chunk.
    """
    plaintext_size = abs_path.stat().st_size
    lengths = _volume_split_lengths(
        plaintext_size, first_chunk_budget, full_budget,
    )
    count = len(lengths)
    blobs: list[_PreparedBlob] = []
    offset = 0
    for i, length in enumerate(lengths):
        if _sigint_received:
            raise KeyboardInterrupt
        scratch_path = scratch_dir / f"{_blob_id(session_n, rel_path)}_{i}"
        crypto.encrypt_file(abs_path, scratch_path, offset=offset, length=length)
        blobs.append(_PreparedBlob(
            rel_path=rel_path,
            kind="full",
            blob=b"",
            result_checksum=result_checksum,
            full_size_bytes=plaintext_size,
            encoded_size=scratch_path.stat().st_size,
            source_checksum=source_checksum,
            streamed=True,
            blob_path=scratch_path,
            volume_chunk=i,
            volume_chunk_count=count,
        ))
        offset += length
    return blobs




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
) -> list[tuple[Path, int, bool]]:
    """
    Input:  session_n      - first session number to build (e.g. 1 for first sync)
            source         - directory being archived
            disc_state     - {path: result_checksum} from manifest.build_disc_state()
            disc_origin    - {path: session} from manifest.build_disc_origin(); the
                              session that last wrote each path currently in
                              disc_state, used to fetch a changed file's prior blob
                              from its true origin session rather than session_n - 1
            backend        - BurnBackend for disc reads on cache miss and for
                              remaining / full-medium capacity
            cache          - CacheManager for encrypted blob retrieval
            crypto         - CryptoBackend for encrypt/decrypt
            _staging_root  - override staging root (default: _default_staging_root)
            _current_state - pre-computed {path: sha256} to skip re-scanning source
            manifests      - optional already-loaded Manifest list (23f); passed
                              to the cache-fallback path so origin sessions are
                              not re-extracted per changed file
    Output: list of (staging_path, session_n, is_new_volume) tuples
    Details:
        Encodes every changed/new file to its final on-disc blob before
        choosing a session directory. Blobs are then greedily packed, sorted
        by path, into as many session_NNN staging directories as needed.
        The first chunk's budget is remaining_bytes * SPACE_SAFETY_MARGIN
        on the current medium, minus MERGE_OVERHEAD_PER_PRIOR_FILE_BYTES for
        every path already in disc_state when session_n > 0 (appending onto
        an existing session chain costs real space to rebuild the disc's
        cumulative directory tree. A fresh medium at session_n == 0 has no
        prior session to merge with, so nothing is deducted there). Every
        later chunk uses a full fresh medium (backend.disc_size when that is
        a positive int, else BDXL_DISC_BYTES) times SPACE_SAFETY_MARGIN, with
        no merge-overhead deduction, since every chunk after the first is
        always a fresh -Z init, never a -M append. A changeset that fits
        returns exactly one tuple with is_new_volume=False. A split returns
        2+ tuples: the first
        is_new_volume=False, every later one True, with contiguous session
        numbers. Combined file content covers each changed/new file exactly
        once. A file whose encoded size would exceed full_budget is
        range-encrypted into dedicated-bin volume chunks (volume_chunk /
        volume_chunk_count on each ManifestEntry) instead of raising.
        Disc budgets are computed before any encrypt_file call so an
        oversized file is chunked in one pass.
        Uses a deterministic named staging dir so a crash-left dir can be
        detected and removed on the next run. Before removing an existing
        dir, checks a PID lockfile via _check_staging_lock(): if the PID
        that created it is still alive, raises RuntimeError instead of
        deleting live data out from under another in-flight build (see
        Assist/HANDOVER_disc_sync_bug.md).
        The source scan excludes the staging tree itself (including that
        lockfile) if staging happens to be nested inside source. Example:
        the home-directory fallback staging root when syncing a source
        tree that contains the user's home directory. Staging bookkeeping
        is then never mistaken for archive content.
        Files larger than STREAM_CHUNK_SIZE (and every volume-split chunk)
        are encrypt_file'd to a scratch directory under the staging root,
        then moved into the packed session directory. Scratch is removed
        on every exit path.
        Installs a SIGINT handler at entry. The handler only sets a flag.
        Raises SystemExit(1) if the staging filesystem space check fails.
        On BaseException every staging dir created by this call is removed
        before re-raising.
        Each changed file's prior blob is fetched from disc_origin[rel_path]
        (its true last-written session), not session_n - 1. A file left
        unchanged across intervening syncs was never restaged, so its blob
        may live several sessions further back than the immediately prior
        one.
    """
    global _sigint_received
    _sigint_received = False
    old_sigint = signal.signal(signal.SIGINT, _handle_sigint)

    root = _staging_root if _staging_root is not None else _default_staging_root()
    created: list[Path] = []
    try:
        staging = _ensure_staging_dir(root, session_n)
        created.append(staging)
        scratch_dir = root / f"oddarchiver_scratch_{session_n:03d}"
        if scratch_dir.exists():
            shutil.rmtree(scratch_dir)
        scratch_dir.mkdir(parents=True)
        created.append(scratch_dir)

        # Scan source (skipped if caller pre-computed current_state)
        if _current_state is not None:
            current_state = _current_state
        else:
            print(f"Scanning {source} ...", flush=True)
            source_files = sorted(
                f for f in source.rglob("*")
                if f.is_file() and not f.is_relative_to(staging)
            )
            total_files = len(source_files)
            print(f"  {total_files} file(s) found. Hashing...", flush=True)
            current_state = {}
            for i, src_file in enumerate(source_files, 1):
                if _sigint_received:
                    raise KeyboardInterrupt
                rel = str(src_file.relative_to(source))
                current_state[rel] = _sha256_file(src_file)
                _print_bar(i, total_files, suffix=rel)
            if total_files:
                print()

        # Diff current source hashes against disc_state
        changed = {
            p: cs for p, cs in current_state.items()
            if p in disc_state and cs != disc_state[p]
        }
        new_files = {p for p in current_state if p not in disc_state}
        deleted = [p for p in disc_state if p not in current_state]

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
        # worst-case estimate: source size for every new or changed file. A
        # changed file's actual delta-vs-full size is not known until it is
        # diffed. This fails fast, before any file is hashed for a blob or
        # staged, instead of dying mid-job after hours of work.
        estimated_bytes = sum(
            (source / p).stat().st_size for p in list(new_files) + list(changed)
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
                    estimated_bytes, len(new_files) + len(changed), staging,
                    staging_free, SPACE_SAFETY_MARGIN, int(staging_limit),
                )
                raise SystemExit(1)

        print("Checking available disc space...", flush=True)
        disc_info = backend.mediainfo()
        # ISO9660 multisession append (session_n > 0, growisofs -M) rebuilds
        # the whole disc's cumulative directory tree, so it costs real space
        # beyond the new files' own bytes. A fresh medium (session_n == 0,
        # growisofs -Z) has no prior session to merge with, so no deduction
        # applies there. See MERGE_OVERHEAD_PER_PRIOR_FILE_BYTES above.
        merge_overhead = (
            len(disc_state) * MERGE_OVERHEAD_PER_PRIOR_FILE_BYTES
            if session_n > 0 else 0
        )
        effective_remaining = max(0, disc_info.remaining_bytes - merge_overhead)
        first_budget = _volume_budget(effective_remaining)
        full_budget = _volume_budget(_full_medium_bytes(backend))

        prepared: list[_PreparedBlob] = []
        split_blobs: list[_PreparedBlob] = []
        oversized_jobs: list[tuple[str, Path, str, str, int]] = []
        ordinary_changed: list[tuple[str, str]] = []
        ordinary_new: list[str] = []

        for rel_path, new_checksum in changed.items():
            if rel_path not in disc_origin:
                raise RuntimeError(
                    f"No origin session recorded for changed file {rel_path!r}; "
                    "disc_origin is out of sync with disc_state."
                )
            abs_path = source / rel_path
            size = abs_path.stat().st_size
            if _encoded_size_upper_bound(size, crypto) >= full_budget:
                oversized_jobs.append((
                    rel_path, abs_path, new_checksum,
                    disc_state.get(rel_path, ""), size,
                ))
            else:
                ordinary_changed.append((rel_path, new_checksum))

        for rel_path in sorted(new_files):
            abs_path = source / rel_path
            size = abs_path.stat().st_size
            if _encoded_size_upper_bound(size, crypto) >= full_budget:
                oversized_jobs.append((
                    rel_path, abs_path, current_state[rel_path], "", size,
                ))
            else:
                ordinary_new.append(rel_path)

        oversized_jobs.sort(key=lambda job: (-job[4], job[0]))
        split_first_budget = first_budget
        if oversized_jobs:
            print(
                f"Staging {len(oversized_jobs)} volume-split file(s)...",
                flush=True,
            )
        for idx, job in enumerate(oversized_jobs, 1):
            if _sigint_received:
                raise KeyboardInterrupt
            rel_path, abs_path, result_checksum, source_checksum, _size = job
            budget0 = split_first_budget if split_first_budget > 0 else full_budget
            file_chunks = _prepare_volume_split_blobs(
                rel_path, abs_path, crypto, scratch_dir, session_n,
                result_checksum, source_checksum, budget0, full_budget,
            )
            split_blobs.extend(file_chunks)
            split_first_budget = 0
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

        # Encode ordinary changed files as delta or full (23f/23g).
        # Origin blobs are prefetched per origin session, then every
        # non-streamed changed file is handed to delta.process_files once.
        if ordinary_changed:
            print(f"Staging {len(ordinary_changed)} changed file(s)...", flush=True)
        staged_changed: dict[str, _PreparedBlob] = {}
        regular: list[tuple[str, str]] = []
        for rel_path, new_checksum in ordinary_changed:
            if _sigint_received:
                raise KeyboardInterrupt
            abs_path = source / rel_path
            if abs_path.stat().st_size > STREAM_CHUNK_SIZE:
                staged_changed[rel_path] = _prepare_streamed_blob(
                    rel_path, abs_path, crypto, scratch_dir, session_n,
                    result_checksum=new_checksum,
                    source_checksum=disc_state.get(rel_path, ""),
                )
            else:
                regular.append((rel_path, new_checksum))

        prefetch = getattr(cache, "prefetch_origin_blobs", None)
        if prefetch is not None and regular:
            prefetch(
                [(rel, disc_origin[rel]) for rel, _ in regular],
                backend,
                manifests=manifests,
                crypto=crypto,
            )

        delta_jobs: list[tuple[bytes, Path]] = []
        delta_meta: list[tuple[str, str]] = []
        for rel_path, new_checksum in regular:
            if _sigint_received:
                raise KeyboardInterrupt
            origin_session = disc_origin[rel_path]
            encrypted_old = cache.get_with_fallback(
                rel_path, origin_session, backend, crypto=crypto,
                manifests=manifests,
            )
            abs_path = source / rel_path
            if len(encrypted_old) > STREAM_CHUNK_SIZE:
                encrypted_blob = crypto.encrypt(abs_path.read_bytes())
                staged_changed[rel_path] = _PreparedBlob(
                    rel_path=rel_path,
                    kind="full",
                    blob=encrypted_blob,
                    result_checksum=new_checksum,
                    full_size_bytes=abs_path.stat().st_size,
                    encoded_size=len(encrypted_blob),
                    source_checksum=disc_state.get(rel_path, ""),
                )
            else:
                old_bytes = crypto.decrypt(encrypted_old)
                delta_jobs.append((old_bytes, abs_path))
                delta_meta.append((rel_path, new_checksum))

        if delta_jobs:
            for (rel_path, new_checksum), (kind, blob) in zip(
                delta_meta, process_files(delta_jobs),
            ):
                abs_path = source / rel_path
                encrypted_blob = crypto.encrypt(blob)
                staged_changed[rel_path] = _PreparedBlob(
                    rel_path=rel_path,
                    kind=kind,
                    blob=encrypted_blob,
                    result_checksum=new_checksum,
                    full_size_bytes=abs_path.stat().st_size,
                    encoded_size=len(encrypted_blob),
                    source_checksum=disc_state.get(rel_path, ""),
                    delta_size_bytes=len(blob) if kind == "delta" else 0,
                )

        for idx, (rel_path, _checksum) in enumerate(ordinary_changed, 1):
            item = staged_changed[rel_path]
            prepared.append(item)
            _print_bar(idx, len(ordinary_changed), suffix=f"[{item.kind}] {rel_path}")
            _log.info("staged %s as %s", rel_path, item.kind)
        if ordinary_changed:
            print()

        # Encode ordinary new files as full blobs
        if ordinary_new:
            print(f"Staging {len(ordinary_new)} new file(s)...", flush=True)
        for idx, rel_path in enumerate(ordinary_new, 1):
            if _sigint_received:
                raise KeyboardInterrupt
            abs_path = source / rel_path
            if abs_path.stat().st_size > STREAM_CHUNK_SIZE:
                prepared.append(_prepare_streamed_blob(
                    rel_path, abs_path, crypto, scratch_dir, session_n,
                    result_checksum=current_state[rel_path],
                ))
            else:
                file_bytes = abs_path.read_bytes()
                encrypted_blob = crypto.encrypt(file_bytes)
                prepared.append(_PreparedBlob(
                    rel_path=rel_path,
                    kind="full",
                    blob=encrypted_blob,
                    result_checksum=current_state[rel_path],
                    full_size_bytes=abs_path.stat().st_size,
                    encoded_size=len(encrypted_blob),
                ))

            _print_bar(idx, len(ordinary_new), suffix=rel_path)
            _log.info("staged new file %s", rel_path)
        if ordinary_new:
            print()

        if _sigint_received:
            raise KeyboardInterrupt

        prepared.sort(key=lambda item: item.rel_path)
        prepared = split_blobs + prepared
        bins = _pack_blobs(prepared, first_budget, full_budget)


        if len(bins) > 1:
            _log.info(
                "Changeset split into %d session(s) starting at %03d",
                len(bins), session_n,
            )
            print(f"  Changeset packed into {len(bins)} volume(s).", flush=True)

        result: list[tuple[Path, int, bool]] = []
        for i, bin_blobs in enumerate(bins):
            chunk_session = session_n + i
            is_new_volume = i > 0
            if i == 0:
                chunk_staging = staging
            else:
                chunk_staging = _ensure_staging_dir(root, chunk_session)
                created.append(chunk_staging)
            chunk_deleted = deleted if i == 0 else []
            _write_chunk(chunk_staging, chunk_session, bin_blobs, chunk_deleted, source)
            used = _staging_bytes(chunk_staging)
            _log.info(
                "Staged %s: %d file(s), %d bytes%s",
                f"session_{chunk_session:03d}",
                len(bin_blobs),
                used,
                " (new volume)" if is_new_volume else "",
            )
            result.append((chunk_staging, chunk_session, is_new_volume))

        if len(result) == 1:
            used = _staging_bytes(result[0][0])
            pct = 100.0 * used / disc_info.remaining_bytes if disc_info.remaining_bytes else 0
            print(f"  OK: session is {pct:.1f}% of remaining space.", flush=True)
            _log.info(
                "Space check OK: staging %d bytes, remaining %d bytes",
                used, disc_info.remaining_bytes,
            )

        _log.info(
            "Staged session %03d%s: %d new, %d changed, %d deleted",
            session_n,
            f"-{session_n + len(result) - 1:03d}" if len(result) > 1 else "",
            len(new_files), len(changed), len(deleted),
        )
        shutil.rmtree(scratch_dir, ignore_errors=True)
        return result

    except BaseException:
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
        raise
    finally:
        signal.signal(signal.SIGINT, old_sigint)


if __name__ == "__main__":
    pass
