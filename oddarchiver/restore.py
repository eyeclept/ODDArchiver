"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Reconstruct the newest selected version on the inserted disc.
    A separate planner collects files from several discs when requested.
"""
# Imports
from __future__ import annotations

import errno
import hashlib
import heapq
import logging
import os
import stat
import sys
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.crypto import blob_aad, new_blob_aad
from oddarchiver.compress import CODEC_LZMA, CODEC_NONE, decode_file, decode_payload, decompress_file
from oddarchiver.delta import apply_delta
from oddarchiver.diskread import DEFAULT_READ_MODE, make_session_source
from oddarchiver.filehash import hash_file
from oddarchiver.manifest import ManifestEntry, validate_blob_path, safe_join_under, load_session_manifests
from oddarchiver.restore_state import (
    FileReplay, RestoreState, load_restore_state, new_restore_state,
    write_restore_state,
)
from oddarchiver.volume import VolumeMarker, read_volume_marker

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.diskread import SessionSource

# Globals
logger = logging.getLogger(__name__)
_log = logger

HASH_CHUNK_SIZE = 1024 * 1024
_BLOB_ERRORS = (OSError, ValueError, RuntimeError)



@dataclass
class RestoreResult:
    """Return restored and failed file counts and recovery details."""

    restored: int
    failures: int
    stale: list[tuple[str, int]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    paused: bool = False

    def __iter__(self):
        yield self.restored
        yield self.failures

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index):
        return (self.restored, self.failures)[index]


@dataclass
class _FileResult:
    """Per-file restore outcome used only inside run_session_replay."""

    status: str
    recovered_at: int | None = None
    checksum: str = ""
    volume_chunk: int | None = None
    volume_chunk_count: int | None = None
    failed: bool = False


@dataclass
class _FileTask:
    """
    Per-file replay state carried across the session-ordered pass (23c).

    entries is the ordered (session, ManifestEntry) list this task
    still needs to apply. In recovery mode it is the full chain, so a
    broken later entry can still fall back to an earlier full snapshot.

    Otherwise it is just the tail from the last full entry onward,
    matching the prior per-file trimming rule. idx tracks the next
    entry to apply.

    current / has_full / last_valid_session / stopped mirror the
    running state the old _reconstruct() / _reconstruct_verified()
    functions built up in one call. Here they persist across session
    boundaries instead.
    """

    rel_path: str
    entries: list[tuple[int, ManifestEntry]]
    dest_file: Path
    target_checksum: str
    last_entry_session: int
    recover: bool
    streamed: bool
    current: bytes | None = None
    has_full: bool = False
    last_valid_session: int | None = None
    stopped: bool = False
    idx: int = 0
    volume_split: bool = False
    already_chunk: int = -1
    volume_chunk_count: int | None = None
    blob_failed: bool = False
    last_delta_blob: bytes | None = None


class _LocalDirReader:
    """
    Input:  root - a SessionSource.open_session() result
    Output: N/A (class)
    Details:
        Adapts a locally materialized session directory to the
        read_path() / extract_path_to_file() surface _read_full(),
        _apply_delta_entry(), and the streamed fast path already know
        how to call. These are the same two methods a BurnBackend
        exposes, so those functions need no change to run against a
        SessionSource instead of a live backend.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def read_path(self, path: str) -> bytes:
        target = self._root / path
        if not target.is_file():
            raise FileNotFoundError(f"Path not found in session source: {path!r}")
        return target.read_bytes()

    def extract_path_to_file(self, path: str, dest: Path) -> None:
        target = self._root / path
        if not target.is_file():
            raise FileNotFoundError(f"Path not found in session source: {path!r}")
        _mkdir_private(dest.parent)
        shutil.copy2(target, dest)


# Functions

def _mkdir_private(path: Path) -> None:
    """
    Input: path - restore output directory
    Output: None
    Details: Create each missing directory with mode 0700. Leave existing
        directories unchanged.
    """
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)




def _write_restored_file(dest_file: Path, data: bytes) -> None:
    """
    Input: destination path and plaintext bytes
    Output: None
    Details: Write a private 0600 temp file in the dest directory, check
        its size, then replace the destination. A failed write leaves
        the old destination file in place. Overwriting an existing file
        whose content differs from the archive prints one warning line.
    """
    if dest_file.is_file():
        try:
            existing = dest_file.read_bytes()
        except OSError:
            _log.warning("Cannot read existing destination file %s", dest_file)
            existing = None
        if existing is not None and existing != data:
            _log.warning(
                "Overwriting existing destination file with archive content: %s",
                dest_file,
            )
            print(
                f"warning: overwrote existing file with archive content: "
                f"{dest_file}",
                flush=True,
            )
    _mkdir_private(dest_file.parent)
    fd, name = tempfile.mkstemp(
        prefix=".oddarchiver_restore_", dir=str(dest_file.parent),
    )
    tmp = Path(name)
    owned = True
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            owned = False
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if tmp.stat().st_size != len(data):
            raise RuntimeError(f"Restored file size differs: {tmp}")
        os.replace(tmp, dest_file)
    except (OSError, RuntimeError) as exc:
        _log.exception("Failed to write restored file %s", dest_file)
        print(
            f"error: could not write {dest_file}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            _log.warning("Cannot remove restore temporary path %s", tmp)
        raise
    finally:
        if owned:
            os.close(fd)


def _open_split_partial(partial: Path, *, create: bool) -> int:
    """
    Input: partial path and create flag
    Output: writable file descriptor
    Details: Open with O_NOFOLLOW. Require a regular file owned by this user.
    """
    if create:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(partial, flags, 0o600)
    else:
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
        fd = os.open(partial, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(
                errno.EPERM, f"Unsafe restore partial file: {partial}",
            )
    except OSError:
        _log.exception("Cannot validate restore partial file %s", partial)
        os.close(fd)
        raise
    return fd


def _remove_restore_temp(path: Path, *, directory: bool = False) -> bool:
    """
    Input: temporary plaintext path and directory flag
    Output: True if removal succeeds
    Details: Report cleanup errors without hiding an earlier restore error.
    """
    try:
        if directory:
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        _log.warning("Cannot remove restore temporary path %s: %s", path, exc)
        return False
    return True


def _suspect_session_count(manifests: list, first_session: int) -> int:
    """
    Input: selected manifests and the first session number on the disc
    Output: number of suspect sessions
    Details: Use the disc position when an unreadable manifest has session -1.
        When every manifest on the disc is unreadable, the key or
        passphrase is wrong (or the disc is from another archive), not
        a partially damaged session list.
    """
    count = 0
    all_suspect = bool(manifests) and all(m.suspect for m in manifests)
    for index, manifest in enumerate(manifests, start=first_session):
        if manifest.suspect:
            session_n = manifest.session if manifest.session >= 0 else index
            if all_suspect:
                message = (
                    f"Cannot restore session {session_n}: SUSPECT manifest. "
                    "Every manifest on this disc is unreadable. The passphrase "
                    "or key is wrong, or the disc is from another archive."
                )
            else:
                message = f"Cannot restore session {session_n}: SUSPECT manifest."
            _log.error("%s", message)
            print(message, file=sys.stderr, flush=True)
            count += 1
    return count


def restore(
    dest: Path,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    session: int | None = None,
    force: bool = False,
    paths: list[str] | None = None,
    skip_volumes: list[int] | None = None,
    read_mode: str = DEFAULT_READ_MODE,
    manifests: list | None = None,
) -> RestoreResult:
    """
    Input:  dest         - destination directory for reconstructed files
            backend      - BurnBackend to read blobs from
            crypto       - CryptoBackend for decryption
            session      - stop replay at this session index (None = latest)
            force        - write dest files even when the existing checksum matches
            paths        - if set, restore only these exact paths or dir prefixes
            skip_volumes - volume indexes marked permanently unavailable
            read_mode    - "sequential" (default) or "full-copy", see
                           oddarchiver.diskread.make_session_source
            manifests    - optional already-loaded Manifest list, skips a
                           second load_session_manifests() disc scan
    Output: restored and failed counts with per-file recovery details
    Details:
        A marked disc restores only files available on that disc. A file
        whose delta needs a base from another disc remains unresolved.
        Unmarked discs retain their original replay behavior.
    """
    dest = Path(dest)
    _mkdir_private(dest)

    print("Scanning disc for sessions...", flush=True)
    marker = read_volume_marker(backend)
    if manifests is None:
        elsewhere = set()
        if marker is not None:
            from oddarchiver.catalog import other_volume_sessions
            elsewhere = other_volume_sessions(marker.archive_id, marker.volume_index)
        manifests = load_session_manifests(
            backend, crypto,
            start_session=marker.first_session if marker else 0,
            marker=marker,
            elsewhere_sessions=elsewhere,
        )
    with tempfile.TemporaryDirectory(prefix="oddarchiver_restore_read_") as workdir:
        selected = _selected_read_manifests(manifests, session, paths, bool(skip_volumes))
        source = make_session_source(backend, selected, Path(workdir), mode=read_mode)
        if marker is None:
            return _restore_legacy(
                dest, source, crypto, manifests, session, force, paths,
                skip_volumes,
            )
        return _restore_multivolume(
            dest, source, backend, crypto, manifests, marker, session, force, paths,
            skip_volumes,
        )


def _needed_chain(
    chain: list[tuple[int, ManifestEntry]], recover: bool,
) -> list[tuple[int, ManifestEntry]]:
    """
    Input: file version chain and recovery mode
    Output: entries whose blobs can be needed for replay
    """
    if not recover:
        split_group = _tail_volume_chunks(chain)
        if split_group:
            chain = split_group
        else:
            for idx in range(len(chain) - 1, -1, -1):
                if chain[idx][1].type == "full":
                    chain = chain[idx:]
                    break
    return chain


def _wanted_blob_keys(
    chains: dict[str, list[tuple[int, ManifestEntry]]],
    deleted_at: dict[str, int],
    paths: list[str] | None,
    recover: bool,
) -> set[tuple[int, str, str]]:
    """
    Input: file chains, deletion sessions, selected paths and recovery mode
    Output: session, path and blob keys needed for replay
    """
    wanted: set[tuple[int, str, str]] = set()
    for path, chain in chains.items():
        if deleted_at.get(path, -1) > chain[-1][0]:
            continue
        if paths is not None and not any(
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
            for prefix in paths
        ):
            continue
        chain = _needed_chain(chain, recover)
        for session_n, entry in chain:
            wanted.add((
                session_n, path,
                entry.file if entry.type == "full" else entry.delta_file,
            ))
    return wanted


def _selected_read_manifests(
    manifests: list,
    session: int | None,
    paths: list[str] | None,
    recover: bool,
) -> list:
    """
    Input: disc manifests, target session, path selection and recovery mode
    Output: manifests with only the encrypted blobs that replay can need
    Details: Do not copy superseded versions in full-copy read mode.
    """
    current = [m for m in manifests if session is None or m.session <= session]
    chains, deleted_at = _build_chains(current)
    wanted = _wanted_blob_keys(chains, deleted_at, paths, recover)
    return [
        replace(m, entries=[
            entry for entry in m.entries
            if (m.session, entry.path,
                entry.file if entry.type == "full" else entry.delta_file) in wanted
        ]) for m in current
    ]


def _restore_legacy(
    dest: Path,
    source: "SessionSource",
    crypto: "CryptoBackend",
    manifests: list,
    session: int | None,
    force: bool,
    paths: list[str] | None,
    skip_volumes: list[int] | None,
) -> RestoreResult:
    """
    Input:  dest, source, crypto, manifests, session, force, paths, skip_volumes
    Output: RestoreResult for the unmarked disc
    Details:
        Unmarked archives do not use restore-state. A skip-volume flag
        activates the existing per-file recovery mode.
    """
    if session is not None:
        manifests = manifests[: session + 1]
    if not manifests:
        _log.warning("No sessions found on disc; nothing to restore.")
        print("No sessions found on disc.", flush=True)
        return RestoreResult(0, 0)

    max_session = len(manifests) - 1
    print(f"  Found {max_session + 1} session(s). Reconstructing...", flush=True)
    suspect_failures = _suspect_session_count(manifests, 0)
    file_chains, deleted_at = _build_chains(manifests)
    restored, failures, _updated, empty_filter, _unresolved, stale, missing = run_session_replay(
        dest, source, crypto, file_chains, deleted_at, max_session,
        force, paths, prior_files={}, allow_unresolved=False,
        recover=bool(skip_volumes),
    )
    failures += suspect_failures
    if empty_filter:
        failures += 1
    return RestoreResult(restored, failures, stale=stale, missing=missing)


def _multivolume_chains(
    manifests: list, state: RestoreState,
) -> tuple[dict[str, list[tuple[int, ManifestEntry]]], dict[str, int]]:
    """
    Input: inserted-disc manifests and prior restore state
    Output: file chains eligible for replay and deletion sessions
    """
    file_chains, deleted_at = _build_chains(manifests)
    file_chains = {
        path: chain for path, chain in file_chains.items()
        if path not in state.files or state.files[path].last_session <= chain[-1][0]
    }
    return file_chains, deleted_at


def _save_multivolume_progress(
    dest: Path,
    state: RestoreState,
    marker: VolumeMarker,
    skip_volumes: list[int] | None,
    updated: dict[str, FileReplay],
    unresolved: list[str],
    missing: list[str],
) -> list[str]:
    """
    Input: destination, restore state, volume selection and replay outcomes
    Output: paths still needing an earlier volume
    """
    if skip_volumes and marker.volume_index - 1 in skip_volumes:
        missing.extend(unresolved)
        unresolved = []
    state.files.update(updated)
    if skip_volumes:
        state.missing_discs = sorted(set(state.missing_discs) | set(skip_volumes))
    if updated or skip_volumes:
        write_restore_state(dest, state)
    return unresolved


def _restore_multivolume(
    dest: Path,
    source: "SessionSource",
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    manifests: list,
    marker: VolumeMarker,
    session: int | None,
    force: bool,
    paths: list[str] | None,
    skip_volumes: list[int] | None,
) -> RestoreResult:
    """
    Input: inserted disc, destination, selection and replay options
    Output: RestoreResult for only the inserted disc
    Details: Never ask for a second disc. Keep newer destination versions.
    """
    from oddarchiver.catalog import build_archive_catalog, disc_name

    state = load_restore_state(dest, marker.archive_id)
    if state is None:
        state = new_restore_state(dest, marker.archive_id, session)
    if session is not None:
        manifests = [
            m for index, m in enumerate(manifests, start=marker.first_session)
            if (m.session if m.session >= 0 else index) <= session
        ]
    if not manifests:
        _log.warning("No in-range sessions found on this disc.")
        print("No sessions found on disc.", flush=True)
        return RestoreResult(0, 0)
    max_session = max(m.session for m in manifests)
    suspect_failures = _suspect_session_count(manifests, marker.first_session)
    print(f"  Found {len(manifests)} session(s). Reconstructing...", flush=True)
    file_chains, deleted_at = _multivolume_chains(manifests, state)
    restored, failures, updated, empty_filter, unresolved, stale, missing = run_session_replay(
        dest, source, crypto, file_chains, deleted_at, max_session,
        force, paths, prior_files={}, allow_unresolved=True,
        recover=bool(skip_volumes),
    )
    failures += suspect_failures
    if empty_filter:
        failures += 1
    unresolved = _save_multivolume_progress(
        dest, state, marker, skip_volumes, updated, unresolved, missing,
    )
    catalog = build_archive_catalog(backend, crypto, manifests, marker)
    earlier = marker.volume_index - 1
    needed = [(path, disc_name(catalog, earlier)) for path in unresolved]
    return RestoreResult(restored, failures, stale=stale, missing=missing, unresolved=needed)




def _file_task_destination_digest(
    dest_file: Path, dest_digest: str | None, dest_digest_known: bool,
) -> str | None:
    """
    Input: destination path, optional known digest, and whether it was computed
    Output: known digest or the destination file's digest when it exists
    """
    if dest_digest_known:
        return dest_digest
    if dest_file.is_file():
        return hash_file(dest_file, HASH_CHUNK_SIZE).hexdigest()
    return None


def _last_full_index(chain: list[tuple[int, ManifestEntry]]) -> int | None:
    """
    Input: ordered entries for a file
    Output: index of the last full entry, or None
    """
    restart_idx = None
    for i, (_, entry) in enumerate(chain):
        if entry.type == "full":
            restart_idx = i
    return restart_idx


def _start_streamed_file_task(
    rel_path: str,
    chain: list[tuple[int, ManifestEntry]],
    restart_idx: int,
    dest_file: Path,
    target_checksum: str,
    last_entry_session: int,
    recover: bool,
    force: bool,
    digest: str | None,
) -> _FileTask | _FileResult:
    """
    Input: file identity, chain, last full index, destination and replay options
    Output: streamed task or a terminal failed/skipped result
    """
    for _, later in chain[restart_idx + 1:]:
        if later.type == "delta":
            _log.error(
                "Streamed full entry for %s is followed by a delta; "
                "unsupported manifest",
                rel_path,
            )
            return _FileResult("failed")
    if not force and digest == target_checksum:
        _log.info("skip %s: already at target checksum", rel_path)
        return _FileResult("skipped")
    return _FileTask(
        rel_path=rel_path,
        entries=[chain[restart_idx]],
        dest_file=dest_file,
        target_checksum=target_checksum,
        last_entry_session=last_entry_session,
        recover=recover,
        streamed=True,
    )


def _file_task_base(
    rel_path: str,
    chain: list[tuple[int, ManifestEntry]],
    dest_file: Path,
    digest: str | None,
    prior: FileReplay | None,
    allow_unresolved: bool,
) -> tuple[bytes | None, int | None] | _FileResult:
    """
    Input: file chain, destination, digest, prior replay and unresolved option
    Output: base bytes and session, or an unresolved result
    """
    base: bytes | None = None
    base_session: int | None = None
    starts_with_full = any(entry.type == "full" for _, entry in chain)
    if not starts_with_full:
        if (
            prior is not None
            and prior.has_full
            and digest == prior.checksum
            and dest_file.is_file()
        ):
            base = dest_file.read_bytes()
            base_session = prior.last_session
        elif allow_unresolved:
            _log.info(
                "Cannot restore %s: earliest entry is a delta with no applied prior volume",
                rel_path,
            )
            return _FileResult("unresolved")
    return base, base_session


def _file_task_path(
    rel_path: str,
    last_entry_session: int,
    deleted_at: dict[str, int],
    max_session: int,
    dest: Path,
) -> Path | _FileResult:
    """
    Input: file path, last entry session, deletion history and destination
    Output: destination path or a terminal skipped/failed result
    """
    del_session = deleted_at.get(rel_path, max_session + 1)
    if max_session >= del_session > last_entry_session:
        return _FileResult("skipped")
    try:
        return safe_join_under(dest, rel_path)
    except ValueError as exc:
        _log.warning("Refused %s: %s", rel_path, exc)
        return _FileResult("failed")


def _start_file_task(
    rel_path: str,
    chain: list[tuple[int, ManifestEntry]],
    deleted_at: dict[str, int],
    max_session: int,
    dest: Path,
    force: bool,
    prior: FileReplay | None,
    allow_unresolved: bool,
    recover: bool,
    dest_digest: str | None = None,
    dest_digest_known: bool = False,
) -> "_FileTask | _FileResult":
    """
    Input:  rel_path, chain, deleted_at, max_session - file state context
            dest, force                              - restore parameters
            prior            - replay entry from already-applied volumes
            allow_unresolved - if True, a delta with no base is "unresolved"
            recover          - if True, a target-checksum miss is stale/missing
            dest_digest      - sha256 of dest if already hashed
            dest_digest_known - True when dest_digest is the hash result
    Output: a _FileTask ready for session-ordered replay, or a terminal
            _FileResult (skipped / failed / unresolved) already knowable
            with no disc read
    Details:
        Hash the destination file at most once. Reuse dest_digest when
        the caller already hashed it.
    """
    last_entry_session = chain[-1][0]
    dest_state = _file_task_path(
        rel_path, last_entry_session, deleted_at, max_session, dest,
    )
    if isinstance(dest_state, _FileResult):
        return dest_state

    target_checksum = chain[-1][1].result_checksum
    dest_file = dest_state

    digest = _file_task_destination_digest(dest_file, dest_digest, dest_digest_known)
    restart_idx = _last_full_index(chain)

    split_group = _tail_volume_chunks(chain)
    if split_group:
        return _start_volume_split_task(
            rel_path, chain, dest_file, target_checksum, force, recover,
            last_entry_session, prior, allow_unresolved, digest,
        )

    streamed = restart_idx is not None and chain[restart_idx][1].streamed
    if streamed:
        return _start_streamed_file_task(
            rel_path, chain, restart_idx, dest_file, target_checksum,
            last_entry_session, recover, force, digest,
        )

    if not force and digest == target_checksum:
        _log.info("skip %s: already at target checksum", rel_path)
        return _FileResult("skipped")

    base_state = _file_task_base(
        rel_path, chain, dest_file, digest, prior, allow_unresolved,
    )
    if isinstance(base_state, _FileResult):
        return base_state
    base, base_session = base_state

    entries = chain if recover or restart_idx is None else chain[restart_idx:]
    return _FileTask(
        rel_path=rel_path,
        entries=entries,
        dest_file=dest_file,
        target_checksum=target_checksum,
        last_entry_session=last_entry_session,
        recover=recover,
        streamed=False,
        current=base,
        has_full=base is not None,
        last_valid_session=base_session,
    )


def _tail_volume_chunks(
    chain: list[tuple[int, ManifestEntry]],
) -> list[tuple[int, ManifestEntry]]:
    """
    Input:  chain - ordered (session, entry) list for one path
    Output: tail entries that carry volume_chunk fields, oldest first
    Details:
        Walks backward from the last entry while volume_chunk and
        volume_chunk_count stay set and the count does not change. An
        empty result means the terminal encoding of this chain is not
        volume-split.
    """
    items: list[tuple[int, ManifestEntry]] = []
    for session_n, entry in reversed(chain):
        if entry.volume_chunk is None or entry.volume_chunk_count is None:
            break
        if items and entry.volume_chunk_count != items[-1][1].volume_chunk_count:
            break
        items.append((session_n, entry))
    items.reverse()
    return items


def _resumable_chunk(prior: FileReplay | None, partial: Path, count: int) -> int:
    """
    Input:  prior - replay from already-applied volumes
            partial - candidate partial file for resuming
            count - required chunk count
    Output: last resumed chunk, or -1 when the partial cannot be used
    """
    already = -1
    usable_partial = False
    if prior is not None and prior.volume_chunk is not None:
        try:
            info = partial.lstat()
        except FileNotFoundError:
            _log.warning("Volume-split partial file is absent: %s", partial)
        else:
            usable_partial = (
                stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            )
        if usable_partial and prior.volume_chunk_count == count:
            already = prior.volume_chunk
    return already


def _split_recovery_gap(
    rel_path: str,
    last_entry_session: int,
    partial: Path,
    already: int,
    present: set[int | None],
    count: int,
    recover: bool,
) -> _FileResult | None:
    """
    Input:  rel_path, last_entry_session - file identity and target session
            partial, already, present, count - available split chunks
            recover - whether to require the complete group
    Output: missing or failed result for a recovery gap, otherwise None
    """
    have = set(range(already + 1)) | present
    if recover:
        if have != set(range(count)):
            if not _remove_restore_temp(partial):
                return _FileResult("failed")
            _log.warning(
                "recovery: %s is missing; volume-split group incomplete "
                "(target session %d)",
                rel_path, last_entry_session,
            )
            return _FileResult("missing")
    return None


def _consecutive_split_entries(
    group: list[tuple[int, ManifestEntry]],
    needed_next: int,
) -> list[tuple[int, ManifestEntry]]:
    """
    Input:  group - sorted split entries from the current volume
            needed_next - first chunk index to apply
    Output: consecutive entries beginning at needed_next
    """
    by_idx = {
        entry.volume_chunk: (session_n, entry)
        for session_n, entry in group
        if entry.volume_chunk is not None
    }
    to_apply: list[tuple[int, ManifestEntry]] = []
    cursor = needed_next
    while cursor in by_idx:
        to_apply.append(by_idx[cursor])
        cursor += 1
    return to_apply


def _split_missing_next(
    rel_path: str,
    needed_next: int,
    allow_unresolved: bool,
) -> _FileResult:
    """
    Input:  rel_path - file identity
            needed_next - missing chunk index
            allow_unresolved - whether more volumes may supply the chunk
    Output: unresolved or failed result for the missing chunk
    """
    if allow_unresolved:
        _log.info(
            "Cannot restore %s: volume-split needs chunk %d next",
            rel_path, needed_next,
        )
        return _FileResult("unresolved")
    _log.error(
        "volume-split restore for %s missing chunk %d",
        rel_path, needed_next,
    )
    return _FileResult("failed")


def _split_target_match(
    dest_file: Path,
    dest_digest: str | None,
    target_checksum: str,
) -> bool:
    """
    Input:  dest_file - target path
            dest_digest - cached target hash, if available
            target_checksum - required hash
    Output: whether the target already has the required checksum
    """
    return dest_digest == target_checksum if dest_digest is not None else (
        dest_file.is_file() and hash_file(dest_file, HASH_CHUNK_SIZE).hexdigest() == target_checksum
    )


def _start_volume_split_task(
    rel_path: str,
    chain: list[tuple[int, ManifestEntry]],
    dest_file: Path,
    target_checksum: str,
    force: bool,
    recover: bool,
    last_entry_session: int,
    prior: FileReplay | None,
    allow_unresolved: bool,
    dest_digest: str | None = None,
) -> "_FileTask | _FileResult":
    """
    Input:  rel_path, chain - file identity and entries from this volume
            dest_file, target_checksum, force, recover, last_entry_session
            prior - replay from already-applied volumes
            allow_unresolved - True on marked multi-volume restore
            dest_digest - sha256 of dest if already hashed
    Output: a volume-split _FileTask, or a terminal _FileResult
    Details:
        Resolves skip / missing / unresolved without a disc read. The
        remaining consecutive chunks from needed_next become the task
        entries the session loop decrypts through SessionSource.
        Recovery mode reports missing when the group is incomplete.
        It never reports stale, and dest is not left as a partial file.
        A resume uses a regular partial file owned by this user.
    """
    group = _tail_volume_chunks(chain)
    if not group:
        _log.error("volume-split restore for %s has no volume_chunk entries", rel_path)
        return _FileResult("failed")

    group = sorted(group, key=lambda item: item[1].volume_chunk or 0)
    count = group[0][1].volume_chunk_count
    if count is None or count < 1:
        _log.error("volume-split restore for %s has invalid volume_chunk_count", rel_path)
        return _FileResult("failed")

    partial = dest_file.with_name(dest_file.name + ".oddarchiver-partial")
    already = _resumable_chunk(prior, partial, count)

    present = {entry.volume_chunk for _, entry in group if entry.volume_chunk is not None}
    gap = _split_recovery_gap(
        rel_path, last_entry_session, partial, already, present, count, recover,
    )
    if gap is not None:
        return gap

    dest_matches = _split_target_match(dest_file, dest_digest, target_checksum)
    if dest_matches:
        if not force or present != set(range(count)):
            _log.info("skip %s: already at target checksum", rel_path)
            return _FileResult("skipped")
        already = -1

    needed_next = already + 1
    to_apply = _consecutive_split_entries(group, needed_next)

    if not to_apply:
        return _split_missing_next(rel_path, needed_next, allow_unresolved)

    return _FileTask(
        rel_path=rel_path,
        entries=to_apply,
        dest_file=dest_file,
        target_checksum=target_checksum,
        last_entry_session=last_entry_session,
        recover=recover,
        streamed=False,
        volume_split=True,
        already_chunk=already,
        volume_chunk_count=count,
    )



def _append_split_plain(task: _FileTask, partial: Path, tmp_plain: Path) -> None:
    """
    Input:  task - volume-split file task
            partial - private partial path beside the target
            tmp_plain - decrypted chunk path
    Output: None; appends the chunk to the partial file
    """
    if task.idx == 0 and task.already_chunk < 0:
        if not _remove_restore_temp(partial):
            raise OSError(f"Cannot remove old partial file {partial}")
        fd = _open_split_partial(partial, create=True)
        with os.fdopen(fd, "wb") as outf, tmp_plain.open("rb") as inf:
            shutil.copyfileobj(inf, outf)
    else:
        fd = _open_split_partial(partial, create=False)
        with os.fdopen(fd, "ab") as outf, tmp_plain.open("rb") as inf:
            shutil.copyfileobj(inf, outf)


def _finalize_split_payload(
    entry: ManifestEntry, partial: Path, dest_file: Path,
    target_checksum: str,
) -> None:
    """
    Input:  last split entry, assembled plaintext payload, dest, checksum
    Output: None; dest holds plaintext that matches target_checksum
    Details:
        Each chunk carries its own ciphertext, so the assembled file is
        the encoded payload: compressed bytes for codec lzma, else the
        file bytes.
    """
    final = partial
    decoded = None
    if getattr(entry, "codec", CODEC_NONE) == CODEC_LZMA:
        decoded = partial.with_name(partial.name + ".decoded")
        decompress_file(partial, decoded)
        final = decoded
    if (
        getattr(entry, "original_size", None) is not None
        and final.stat().st_size != getattr(entry, "original_size", None)
    ):
        raise ValueError(
            f"Restored size {final.stat().st_size} does not match "
            f"original_size {getattr(entry, "original_size", None)} for {entry.path}"
        )
    if hash_file(final, HASH_CHUNK_SIZE).hexdigest() != target_checksum:
        raise ValueError(
            f"Checksum mismatch for {entry.path}: expected {target_checksum}"
        )
    os.replace(final, dest_file)
    partial.unlink(missing_ok=True)
    if decoded is not None and decoded.exists():
        decoded.unlink(missing_ok=True)


def _finish_split_chunk(
    task: _FileTask, entry: ManifestEntry, partial: Path,
) -> _FileResult | None:
    """
    Input:  task - volume-split file task
            entry - chunk just appended
            partial - assembled plaintext chunks beside the target
    Output: restored result for the last applied chunk, otherwise None
    """
    last_applied = entry.volume_chunk
    if last_applied is None:
        raise ValueError("Volume chunk index is absent.")
    if task.idx + 1 == len(task.entries):
        count = task.volume_chunk_count
        if last_applied + 1 == count:
            _finalize_split_payload(
                entry, partial, task.dest_file, task.target_checksum,
            )
        _log.info("restored %s chunks 0..%d of %d", task.rel_path, last_applied, count)
        return _FileResult(
            "restored",
            checksum=task.target_checksum,
            volume_chunk=last_applied,
            volume_chunk_count=count,
        )
    return None


def _apply_volume_split_chunk(
    task: _FileTask,
    reader: "_LocalDirReader",
    crypto: "CryptoBackend",
) -> _FileResult | None:
    """
    Input:  task   - a volume-split _FileTask (entries are consecutive chunks)
            reader - extract_path_to_file() scoped to the session of this chunk
            crypto - CryptoBackend for decrypt_file
    Output: terminal _FileResult, or None if more chunks remain
    Details:
        Decrypt each chunk and append it to a private partial file next
        to the target. Verify the complete file before replacing the target.
        A failed write removes the partial file and preserves the target.
    """
    _session_n, entry = task.entries[task.idx]
    partial = task.dest_file.with_name(task.dest_file.name + ".oddarchiver-partial")
    tmp_dir = Path(tempfile.mkdtemp(prefix="oddarchiver_vsplit_"))
    outcome: _FileResult | None = None
    try:
        tmp_blob = tmp_dir / f"blob_{entry.volume_chunk}"
        tmp_plain = tmp_dir / f"plain_{entry.volume_chunk}"
        validate_blob_path(entry.file)
        reader.extract_path_to_file(entry.file, tmp_blob)
        crypto.decrypt_file(tmp_blob, tmp_plain, aad=blob_aad(entry))
        _mkdir_private(task.dest_file.parent)
        _append_split_plain(task, partial, tmp_plain)
        outcome = _finish_split_chunk(task, entry, partial)
    except _BLOB_ERRORS as exc:
        _log.warning(
            "Failed to restore volume-split %s chunk %s: %s",
            task.rel_path, entry.volume_chunk, exc,
        )
        outcome = _FileResult("failed")
        if not partial.is_symlink() and not _remove_restore_temp(partial):
            _log.error("Partial file cleanup failed for %s", task.rel_path)
    finally:
        # Runs on interrupt too: the directory holds decrypted plaintext.
        tmp_removed = _remove_restore_temp(tmp_dir, directory=True)
    if not tmp_removed:
        outcome = _FileResult("failed")
        if not partial.is_symlink() and not _remove_restore_temp(partial):
            _log.error("Partial file cleanup failed for %s", task.rel_path)
    return outcome


def _apply_full_task_entry(
    task: _FileTask,
    session_n: int,
    entry: ManifestEntry,
    reader: _LocalDirReader,
    crypto: CryptoBackend,
) -> None:
    """
    Input:  task - in-flight file task, mutated in place
            session_n, entry - full entry and its session
            reader - reader scoped to this session
            crypto - decryption backend
    Output: None; updates the task or stops it on failure
    """
    data, ok = _read_full(task.rel_path, entry, reader, crypto)
    if not ok:
        task.blob_failed = True
        task.stopped = True
        return
    if task.recover:
        if hashlib.sha256(data).hexdigest() != entry.result_checksum:
            _log.warning(
                "recovery: full blob checksum mismatch for %s at session %d",
                task.rel_path, session_n,
            )
            task.blob_failed = True
            task.stopped = True
            return
        task.last_valid_session = session_n
    task.current = data
    task.has_full = True
    task.last_delta_blob = None


def _apply_task_entry(
    task: _FileTask,
    session_n: int,
    entry: ManifestEntry,
    reader: "_LocalDirReader",
    crypto: "CryptoBackend",
) -> None:
    """
    Input:  task    - in-flight _FileTask (mutated in place)
            session_n, entry - the chain entry that restore applies now
            reader  - read_path()/extract_path_to_file() scoped to
                      the locally materialized blobs of session_n
            crypto  - CryptoBackend for decryption
    Output: None (mutates task.current / has_full / last_valid_session / stopped)
    Details:
        Read full blobs and keep the plaintext delta blob from the last
        successful delta. Check each delta result against the manifest.
        Retry a failed delta with the previous delta blob as its source.
        In recovery mode, also check each full entry. Keep the last
        verified content when a later entry fails.
    """
    if entry.type == "full":
        _apply_full_task_entry(task, session_n, entry, reader, crypto)
        return

    if task.current is None:
        _log.error("Delta entry for %s with no prior full blob", task.rel_path)
        task.stopped = True
        return
    if task.recover:
        if hashlib.sha256(task.current).hexdigest() != entry.source_checksum:
            _log.info(
                "recovery: source checksum mismatch for %s at session %d; stopping chain",
                task.rel_path, session_n,
            )
            task.stopped = True
            return
    patched, delta_blob, ok = _apply_delta_entry(
        task.rel_path, session_n, entry, task.current,
        task.last_delta_blob, reader, crypto,
    )
    if not ok:
        task.blob_failed = True
        task.stopped = True
        return
    if task.recover:
        task.last_valid_session = session_n
    task.current = patched
    task.last_delta_blob = delta_blob

def _apply_streamed_task(
    task: _FileTask,
    reader: "_LocalDirReader",
    crypto: "CryptoBackend",
) -> _FileResult:
    """
    Input:  task   - a streamed _FileTask (single entry, already validated
                     to have no delta after it)
            reader - read_path()/extract_path_to_file() scoped to the
                     session of this single entry
            crypto - CryptoBackend for decrypt_file
    Output: _FileResult
    Details:
        Decrypt to a private file in the destination directory.
        Check its checksum before replacing the destination.
        Never materialize the blob in memory.
    """
    entry = task.entries[0][1]
    tmp_dir = Path(tempfile.mkdtemp(prefix="oddarchiver_stream_"))
    tmp_blob = tmp_dir / "blob"
    tmp_plain: Path | None = None
    outcome = _FileResult("failed")
    try:
        _mkdir_private(task.dest_file.parent)
        fd, name = tempfile.mkstemp(
            prefix=".oddarchiver_restore_", dir=str(task.dest_file.parent),
        )
        os.fchmod(fd, 0o600)
        os.close(fd)
        tmp_plain = Path(name)
        validate_blob_path(entry.file)
        reader.extract_path_to_file(entry.file, tmp_blob)
        tmp_stored = tmp_dir / "stored"
        crypto.decrypt_file(tmp_blob, tmp_stored, aad=blob_aad(entry))
        codec = getattr(entry, "codec", CODEC_NONE)
        if codec == CODEC_LZMA:
            decode_file(tmp_stored, tmp_plain, codec)
        else:
            # The decrypted bytes already equal the file, so adopt them
            # instead of copying the whole blob a second time.
            os.replace(tmp_stored, tmp_plain)
        if (
            codec == CODEC_LZMA
            and getattr(entry, "original_size", None)
            and tmp_plain.stat().st_size != getattr(entry, "original_size", None)
        ):
            raise ValueError(
                f"Restored size {tmp_plain.stat().st_size} != original_size {getattr(entry, "original_size", None)}"
            )
        if hash_file(tmp_plain, HASH_CHUNK_SIZE).hexdigest() != task.target_checksum:
            raise ValueError(
                f"Checksum mismatch for {task.rel_path}: expected {task.target_checksum}"
            )
        os.replace(tmp_plain, task.dest_file)
        _log.info("restored %s", task.rel_path)
        outcome = _FileResult("restored")
    except _BLOB_ERRORS as exc:
        _log.warning("Failed to restore streamed %s: %s", task.rel_path, exc)
    if tmp_plain is not None and not _remove_restore_temp(tmp_plain):
        outcome = _FileResult("failed")
    if not _remove_restore_temp(tmp_dir, directory=True):
        outcome = _FileResult("failed")
    return outcome


def _finalize_normal_task(task: _FileTask) -> _FileResult:
    """
    Input:  task - completed non-recovery file task
    Output: restored result if checksum matches and write succeeds, else failed
    """
    if not task.stopped and task.has_full:
        result = task.current
        if hashlib.sha256(result).hexdigest() == task.target_checksum:
            try:
                _write_restored_file(task.dest_file, result)
            except (OSError, RuntimeError):
                _log.warning("Failed to write restored file %s", task.rel_path)
                return _FileResult("failed")
            _log.info("restored %s", task.rel_path)
            return _FileResult("restored")
        _log.error(
            "Checksum mismatch for %s: expected %s",
            task.rel_path, task.target_checksum,
        )
    return _FileResult("failed")


def _finalize_task(task: _FileTask) -> _FileResult:
    """
    Input:  task - _FileTask after the code applied its last scheduled
                   entry (or an earlier stop)
    Output: _FileResult, matching the prior _process_file()'s tail exactly
    Details:
        Non-recover: a final checksum mismatch (or an earlier stop) is
        "failed". A match writes dest_file and is "restored".
        Recover: no validated prefix at all is "missing". A validated
        prefix short of the target session is "stale". A validated
        prefix matching the target checksum is "restored".
    """
    if not task.recover:
        return _finalize_normal_task(task)

    if task.last_valid_session is None or task.current is None:
        if task.blob_failed:
            _log.error("recovery: blob read or decrypt failed for %s", task.rel_path)
            return _FileResult("failed")
        _log.warning("recovery: %s is missing; no validated prefix", task.rel_path)
        return _FileResult("missing")

    checksum = hashlib.sha256(task.current).hexdigest()
    try:
        _write_restored_file(task.dest_file, task.current)
    except (OSError, RuntimeError):
        _log.warning("Failed to write restored file %s", task.rel_path)
        return _FileResult("failed")
    if checksum == task.target_checksum:
        _log.info("restored %s", task.rel_path)
        return _FileResult(
            "restored", recovered_at=task.last_valid_session,
            checksum=checksum, failed=task.blob_failed,
        )
    _log.warning(
        "recovery: %s is stale at session %d (target session %d)",
        task.rel_path, task.last_valid_session, task.last_entry_session,
    )
    return _FileResult(
        "stale", recovered_at=task.last_valid_session,
        checksum=checksum, failed=task.blob_failed,
    )


def _filter_replay_chains(
    file_chains: dict[str, list[tuple[int, ManifestEntry]]],
    prior_files: dict[str, FileReplay],
    paths: list[str] | None,
    max_session: int,
) -> tuple[dict[str, list[tuple[int, ManifestEntry]]], bool]:
    """
    Input: file_chains, prior_files - current and prior replay paths
           paths, max_session - requested paths and target session
    Output: selected chains and whether the requested filter matched nothing
    """
    empty_filter = False
    if paths is not None:
        matched_requested = {p: False for p in paths}
        filtered: dict[str, list[tuple[int, ManifestEntry]]] = {}
        for rel_path, chain in file_chains.items():
            for p in paths:
                if rel_path == p or rel_path.startswith(p.rstrip("/") + "/"):
                    filtered[rel_path] = chain
                    matched_requested[p] = True
        for rel_path in prior_files:
            for p in paths:
                if rel_path == p or rel_path.startswith(p.rstrip("/") + "/"):
                    matched_requested[p] = True
        for p, did_match in matched_requested.items():
            if not did_match:
                print(
                    f"warning: no archive entry matches {p!r} as of session {max_session}",
                    flush=True,
                )
        file_chains = filtered
        empty_filter = not file_chains and not any(matched_requested.values())
    return file_chains, empty_filter


def _record_replay_file(
    rel_path: str,
    outcome: _FileResult,
    chain: list[tuple[int, ManifestEntry]],
    prior: FileReplay | None,
    deleted_at: dict[str, int],
    max_session: int,
    updated_files: dict[str, FileReplay],
) -> None:
    """
    Input: rel_path, outcome, chain, prior - file replay outcome and history
           deleted_at, max_session - deletion and target session
           updated_files - replay records to update
    Output: None; updates the replay record when appropriate
    """
    if outcome.status == "restored":
        if outcome.volume_chunk is not None:
            updated_files[rel_path] = FileReplay(
                checksum=outcome.checksum or chain[-1][1].result_checksum,
                last_session=chain[-1][0],
                has_full=(
                    outcome.volume_chunk_count is not None
                    and outcome.volume_chunk + 1 == outcome.volume_chunk_count
                ),
                volume_chunk=outcome.volume_chunk,
                volume_chunk_count=outcome.volume_chunk_count,
            )
        else:
            updated_files[rel_path] = _replay_from_chain(chain, prior=prior)
    elif outcome.status == "stale":
        updated_files[rel_path] = FileReplay(
            checksum=outcome.checksum,
            last_session=outcome.recovered_at or 0,
            has_full=True,
        )
    elif outcome.status == "skipped":
        del_session = deleted_at.get(rel_path, max_session + 1)
        last_entry_session = chain[-1][0]
        if not (del_session <= max_session and del_session > last_entry_session):
            updated_files[rel_path] = _replay_from_chain(chain, prior=prior)


def _queue_replay_tasks(
    file_chains: dict[str, list[tuple[int, ManifestEntry]]],
    deleted_at: dict[str, int],
    max_session: int,
    dest: Path,
    force: bool,
    prior_files: dict[str, FileReplay],
    allow_unresolved: bool,
    recover: bool,
    dest_digests: dict[str, str | None] | None,
    tasks: dict[str, _FileTask],
    schedule,
    record,
) -> None:
    """
    Input: file_chains, deleted_at, max_session - replay chains and boundaries
           dest, force, prior_files, allow_unresolved, recover, dest_digests
           tasks, schedule, record - task state and replay callbacks
    Output: None; records known outcomes and schedules remaining tasks
    """
    for rel_path, chain in file_chains.items():
        known = dest_digests is not None and rel_path in dest_digests
        outcome_or_task = _start_file_task(
            rel_path, chain, deleted_at, max_session, dest, force,
            prior_files.get(rel_path), allow_unresolved, recover,
            dest_digest=dest_digests.get(rel_path) if known else None,
            dest_digest_known=known,
        )
        if isinstance(outcome_or_task, _FileResult):
            record(rel_path, outcome_or_task)
            continue
        tasks[rel_path] = outcome_or_task
        schedule(rel_path, outcome_or_task.entries[0][0])


def _advance_replay_task(
    task: _FileTask,
    rel_path: str,
    session_n: int,
    reader: _LocalDirReader,
    crypto: CryptoBackend,
    tasks: dict[str, _FileTask],
    schedule,
    record,
) -> None:
    """
    Input: task, rel_path, session_n, reader, crypto - current file and session
           tasks, schedule, record - task state and replay callbacks
    Output: None; applies the next entry and records or schedules the result
    Details:
        A volume-split file can hold several chunks in one session, so
        this applies every consecutive chunk of the current session
        before it schedules the next session. The session walk visits
        each session once, so a reschedule here would never run.
    """
    entry = task.entries[task.idx][1]
    if task.volume_split:
        while True:
            outcome = _apply_volume_split_chunk(task, reader, crypto)
            if outcome is not None:
                del tasks[rel_path]
                record(rel_path, outcome)
                return
            task.idx += 1
            next_session = task.entries[task.idx][0]
            if next_session != session_n:
                schedule(rel_path, next_session)
                return
    if task.streamed:
        outcome = _apply_streamed_task(task, reader, crypto)
        del tasks[rel_path]
        record(rel_path, outcome)
        return
    _apply_task_entry(task, session_n, entry, reader, crypto)
    task.idx += 1
    if task.stopped or task.idx == len(task.entries):
        outcome = _finalize_task(task)
        del tasks[rel_path]
        record(rel_path, outcome)
    else:
        schedule(rel_path, task.entries[task.idx][0])


def _visit_replay_sessions(
    heap: list[int],
    sessions_needed: dict[int, list[str]],
    tasks: dict[str, _FileTask],
    source: SessionSource,
    crypto: CryptoBackend,
    schedule,
    record,
) -> None:
    """
    Input: heap, sessions_needed, tasks - scheduled replay state
           source, crypto - session reader and decryption
           schedule, record - replay callbacks
    Output: None; visits each needed session and advances its tasks
    """
    visited: set[int] = set()
    while heap:
        session_n = heapq.heappop(heap)
        if session_n in visited:
            continue
        visited.add(session_n)
        pending = sessions_needed.pop(session_n, [])
        if not pending:
            continue
        session_root = source.open_session(session_n)
        reader = _LocalDirReader(session_root)
        for rel_path in pending:
            task = tasks.get(rel_path)
            if task is None:
                continue
            entry_session, entry = task.entries[task.idx]
            if entry_session != session_n:
                continue
            _advance_replay_task(
                task, rel_path, session_n, reader, crypto, tasks, schedule, record,
            )


def _unfinished_replay_result(task: _FileTask) -> _FileResult:
    """
    Input: task - unfinished replay task
    Output: result for a task left after all needed sessions were visited
    """
    if task.volume_split:
        if task.recover:
            partial = task.dest_file.with_name(task.dest_file.name + ".oddarchiver-partial")
            outcome = (
                _FileResult("missing") if _remove_restore_temp(partial)
                else _FileResult("failed")
            )
        elif task.idx == 0:
            outcome = _FileResult("failed")
        else:
            last_applied = task.entries[task.idx - 1][1].volume_chunk
            outcome = _FileResult(
                "restored",
                checksum=task.target_checksum,
                volume_chunk=last_applied,
                volume_chunk_count=task.volume_chunk_count,
            )
    else:
        outcome = _FileResult("failed") if task.streamed else _finalize_task(task)
    return outcome


def _replay_status_text(
    restored: int,
    failures: int,
    status_failed: int,
    unresolved_paths: list[str],
    stale: list[tuple[str, int]],
    missing: list[str],
    total_files: int,
) -> str:
    """
    Input: restored, failures, status_failed - replay outcome counts
           unresolved_paths, stale, missing - incomplete file lists
           total_files - number of selected paths
    Output: summary text for the completed replay
    """
    unresolved = len(unresolved_paths)
    status = f"{restored} restored"
    if failures:
        status += f", {failures} failed"
    if unresolved:
        status += f", {unresolved} need an earlier volume"
    if stale:
        status += f", {len(stale)} stale"
    if missing:
        status += f", {len(missing)} missing"
    skipped = total_files - restored - status_failed - unresolved - len(stale) - len(missing)
    if skipped:
        status += f", {skipped} already up to date"
    return status


def run_session_replay(
    dest: Path,
    source: "SessionSource",
    crypto: "CryptoBackend",
    file_chains: dict[str, list[tuple[int, ManifestEntry]]],
    deleted_at: dict[str, int],
    max_session: int,
    force: bool,
    paths: list[str] | None,
    prior_files: dict[str, FileReplay],
    allow_unresolved: bool,
    recover: bool = False,
    dest_digests: dict[str, str | None] | None = None,
) -> tuple[int, int, dict[str, FileReplay], bool, list[str], list[tuple[str, int]], list[str]]:
    """
    Input:  dest, source, crypto     - restore destination and session-ordered blob source
            file_chains, deleted_at  - from _build_chains(manifests)
            max_session, force, paths, prior_files, allow_unresolved, recover
                                     - same meaning as the prior per-file loop
    Output: (restored, failures, updated_files, empty_filter,
             unresolved_paths, stale, missing)
    Details:
        Shared session-ordered replay engine (23c). restore() and the
        verify.py --level full check use it. _start_file_task resolves,
        with zero I/O, every file whose outcome is knowable without a
        disc read (already deleted, already at target checksum,
        unresolved with no prior volume, or an invalid streamed+delta
        chain). Every remaining file becomes a _FileTask scheduled
        against the sessions its own (possibly trimmed) chain still
        needs. The loop then visits sessions in strictly ascending
        order, exactly once each, via source.open_session(). A min-heap
        orders the visits, so the second, third, and later entries of a
        file schedule correctly even though the walk did not know them
        when it began. The walk discovers each entry only after it
        applies the prior entry. Every task at a given session shares
        that one source.open_session() call.
    """
    from oddarchiver.session import _print_bar

    file_chains, empty_filter = _filter_replay_chains(
        file_chains, prior_files, paths, max_session,
    )

    total_files = len(file_chains)
    print(f"  {total_files} file(s) to reconstruct. Writing to {dest} ...", flush=True)

    restored = 0
    failures = 0
    status_failed = 0
    unresolved_paths: list[str] = []
    stale: list[tuple[str, int]] = []
    missing: list[str] = []
    updated_files: dict[str, FileReplay] = {}
    done = 0

    def _record(rel_path: str, outcome: _FileResult) -> None:
        nonlocal restored, failures, done, status_failed
        chain = file_chains[rel_path]
        if outcome.failed:
            failures += 1
        if outcome.status == "failed":
            status_failed += 1
        if outcome.status == "restored":
            restored += 1
        elif outcome.status == "failed":
            failures += 1
        elif outcome.status == "unresolved":
            unresolved_paths.append(rel_path)
        elif outcome.status == "stale":
            stale.append((rel_path, outcome.recovered_at or 0))
        elif outcome.status == "missing":
            missing.append(rel_path)
        _record_replay_file(
            rel_path, outcome, chain, prior_files.get(rel_path),
            deleted_at, max_session, updated_files,
        )
        done += 1
        _print_bar(done, total_files, suffix=rel_path)

    tasks: dict[str, _FileTask] = {}
    sessions_needed: dict[int, list[str]] = {}
    heap: list[int] = []

    def _schedule(rel_path: str, session_n: int) -> None:
        sessions_needed.setdefault(session_n, []).append(rel_path)
        heapq.heappush(heap, session_n)

    _queue_replay_tasks(
        file_chains, deleted_at, max_session, dest, force, prior_files,
        allow_unresolved, recover, dest_digests, tasks, _schedule, _record,
    )

    _visit_replay_sessions(
        heap, sessions_needed, tasks, source, crypto, _schedule, _record,
    )

    for rel_path, task in list(tasks.items()):
        outcome = _unfinished_replay_result(task)
        _record(rel_path, outcome)

    if total_files:
        print()

    status = _replay_status_text(
        restored, failures, status_failed, unresolved_paths, stale, missing, total_files,
    )
    print(f"  Done: {status}.", flush=True)
    _log.info(
        "Restore complete: %d restored, %d failures, %d stale, %d missing",
        restored, failures, len(stale), len(missing),
    )
    if failures:
        _log.error("%d file(s) could not be restored", failures)
    return restored, failures, updated_files, empty_filter, unresolved_paths, stale, missing


def _replay_from_chain(
    chain: list[tuple[int, ManifestEntry]],
    prior: FileReplay | None = None,
) -> FileReplay:
    """
    Input:  chain - ordered (session, entry) list for one path
            prior - replay from already-applied volumes, if any
    Output: FileReplay at the last entry of the chain
    Details:
        has_full is True if this chain contains a full blob or a prior
        applied volume already reconstructed one (dest holds those bytes).
    """
    last_session, last_entry = chain[-1]
    has_full = any(entry.type == "full" for _, entry in chain)
    if prior is not None and prior.has_full:
        has_full = True
    return FileReplay(
        checksum=last_entry.result_checksum,
        last_session=last_session,
        has_full=has_full,
    )


def _build_chains(
    manifests: list,
) -> tuple[dict[str, list[tuple[int, ManifestEntry]]], dict[str, int]]:
    """
    Input:  manifests - list of Manifest objects in ascending session order
    Output: (file_chains, deleted_at)
            file_chains - {path: [(session_n, entry), ...]}
            deleted_at  - {path: last_session_that_deleted_it}
    Details:
        Skips SUSPECT manifests. The caller counts each one as a failure.
    """
    file_chains: dict[str, list[tuple[int, ManifestEntry]]] = {}
    deleted_at: dict[str, int] = {}
    for m in manifests:
        if m.suspect:
            _log.warning("Skipping SUSPECT manifest for session %d", m.session)
            continue
        for entry in m.entries:
            file_chains.setdefault(entry.path, []).append((m.session, entry))
        for del_path in m.deleted:
            deleted_at[del_path] = m.session
    return file_chains, deleted_at


def _read_full(
    rel_path: str,
    entry: ManifestEntry,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
) -> tuple[bytes, bool]:
    """
    Input:  rel_path, entry - file identity and manifest entry
            backend, crypto - I/O and decryption
    Output: (plaintext_bytes, success_bool)
    Details:
        Validates entry.file with validate_blob_path before the disc read.
        backend needs only read_path(). A _LocalDirReader satisfies this.
    """
    try:
        validate_blob_path(entry.file)
        stored = crypto.decrypt(backend.read_path(entry.file), aad=blob_aad(entry))
        return decode_payload(stored, getattr(entry, "codec", CODEC_NONE)), True
    except _BLOB_ERRORS as exc:
        _log.warning("Failed to read full blob for %s: %s", rel_path, exc)
        return b"", False


def _try_delta_sources(
    rel_path: str,
    session_n: int,
    entry: ManifestEntry,
    current: bytes,
    previous_delta: bytes | None,
    delta_bytes: bytes,
) -> tuple[bytes, str | None, bool]:
    """
    Input: rel_path, session_n, entry - delta identity and expected checksum
           current, previous_delta, delta_bytes - candidate bases and delta
    Output: result, last failure reason, and checksum success
    """
    failure: str | None = None
    for fallback, source in enumerate(
        (current, previous_delta) if previous_delta is not None else (current,)
    ):
        try:
            result = apply_delta(source, delta_bytes)
        except _BLOB_ERRORS as exc:
            _log.warning("Delta decode failed for %s at session %d: %s", rel_path, session_n, exc)
            failure = str(exc)
            continue
        if hashlib.sha256(result).hexdigest() == entry.result_checksum:
            if fallback:
                _log.warning(
                    "%s at session %d: recovered a delta written against the previous delta blob",
                    rel_path, session_n,
                )
            return result, None, True
        failure = f"result checksum mismatch at session {session_n}"
    return b"", failure, False


def _apply_delta_entry(
    rel_path: str,
    session_n: int,
    entry: ManifestEntry,
    current: bytes,
    previous_delta: bytes | None,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
) -> tuple[bytes, bytes | None, bool]:
    """
    Input: path, session, entry, rebuilt content, last delta blob, reader, crypto
    Output: verified content, current delta blob, and success flag
    Details: Try rebuilt content first. Retry with the last delta blob only
        when it exists. Check the result checksum before accepting either result.
    """
    try:
        validate_blob_path(entry.delta_file)
        delta_bytes = decode_payload(
            crypto.decrypt(
                backend.read_path(entry.delta_file), aad=blob_aad(entry),
            ),
            getattr(entry, "codec", CODEC_NONE),
        )
    except _BLOB_ERRORS as exc:
        _log.warning("Failed to apply delta for %s: %s", rel_path, exc)
        return b"", None, False

    result, failure, ok = _try_delta_sources(
        rel_path, session_n, entry, current, previous_delta, delta_bytes,
    )
    if ok:
        return result, delta_bytes, True
    if previous_delta is None:
        _log.warning(
            "Failed to apply delta for %s: %s. No previous delta blob is available for recovery.",
            rel_path, failure,
        )
    else:
        _log.warning("Failed to apply delta for %s: %s", rel_path, failure)
    return b"", None, False


if __name__ == "__main__":
    pass
