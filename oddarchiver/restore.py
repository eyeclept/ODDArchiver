"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Reconstruct a version of the source directory from disc or ISO.
    Skip dest files that already match the target checksum unless
    force is True. Marked multi-volume archives restore one disc per
    invocation, oldest first, with a restore-state file under the
    oddarchiver state directory tracking already-applied volumes.

    --skip-volume N marks a volume permanently gone and activates
    recovery mode: files land in restored, stale, or missing.

    Session-ordered replay (TODO 23c/23d): the engine below
    (run_session_replay, _FileTask and friends) visits disc sessions
    in ascending order exactly once each. A SessionSource
    (oddarchiver.diskread) batch-materializes the blobs of each
    session locally before the engine reconstructs any file that
    needs them.

    This replaces the old "build a per-path chain, iterate every path
    alphabetically, pull blobs on demand" loop. That pattern jumped
    around the medium once per file instead of streaming forward.

    Per-file reconstruction rules are the same as before
    (skip-if-already-current, deleted-path handling, delta chains,
    recovery-mode stale/missing bucketing, streamed fast path). Only
    the order and batching of the underlying reads differ. The
    verify.py --level full check also uses this engine.
"""
# Imports
from __future__ import annotations

import hashlib
import heapq
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.delta import apply_delta
from oddarchiver.crypto import STREAM_CHUNK_SIZE
from oddarchiver.diskread import DEFAULT_READ_MODE, make_session_source
from oddarchiver.manifest import ManifestEntry, validate_blob_path, safe_join_under, load_session_manifests
from oddarchiver.restore_state import (
    FileReplay,
    RestoreState,
    VolumeRecord,
    load_restore_state,
    new_restore_state,
    write_restore_state,
)
from oddarchiver.volume import VolumeMarker, read_volume_marker

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.diskread import SessionSource

# Globals
_log = logging.getLogger(__name__)

_BLOB_ERRORS = (OSError, ValueError, RuntimeError)


@dataclass
class NeededVolume:
    """Earlier volume that restore still needs before the archive is complete."""

    volume_index: int
    label: str
    first_session: int | None = None
    last_session: int | None = None


@dataclass
class RestoreResult:
    """
    restore() return value.

    Unpacks as (restored, failures) so existing call sites keep working.
    restore sets needed when the archive still needs an earlier volume.
    That is not a failure, and legacy (unmarked) archives always have
    needed=None. stale and missing are recovery-mode buckets
    (--skip-volume): they do not unpack and do not count as failures.
    """

    restored: int
    failures: int
    needed: NeededVolume | None = None
    stale: list[tuple[str, int]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

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
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest)


# Functions

def _sha256_path(path: Path) -> str:
    """
    Input:  path - file to hash
    Output: hex sha256 digest
    Details:
        Reads STREAM_CHUNK_SIZE chunks so a large restored file is never
        fully materialized.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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
    Output: RestoreResult (restored_count, failure_count, needed volume or None)
    Details:
        Loads manifests from disc in session order via load_session_manifests,
        then slices to --session locally. Builds a SessionSource (23d) that
        batch-materializes the blobs of each disc session locally in
        ascending order, and replays the manifest.entries chain of every
        file against dest through that source (23c). Verifies sha256 of
        each result against result_checksum. On checksum mismatch, logs
        ERROR. Does not write corrupt data. _build_chains skips SUSPECT
        manifests with a WARNING. Rejects dest paths that escape dest via
        safe_join_under. Skips a path deleted after its last entry when
        that delete is in range. With a paths filter, restore applies
        only matching keys. restore prints unmatched requested paths as
        warnings. Zero matches across every requested path increments
        failures by 1. If read_volume_marker returns None, this is a
        legacy archive: restore does not read or write restore-state, and
        behaviour matches the single-volume path. A present marker loads
        dest restore-state, reconstructs every chain resolvable from
        applied volumes plus the current disc, records the current volume
        as applied on a clean pass, and reports the next earlier volume
        still needed. An unresolved file (delta with no prior full from
        applied volumes) is not a failure. A non-empty skip_volumes list
        activates recovery mode: restore never reports a skipped volume
        as needed, and a chain that cannot reach its target checksum
        becomes stale or missing instead of failed.
        restore() tears down the scratch directory of the SessionSource
        on return, regardless of mode.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    print("Scanning disc for sessions...", flush=True)
    marker = read_volume_marker(backend)
    if manifests is None:
        manifests = load_session_manifests(
            backend, crypto, start_session=marker.first_session if marker else 0,
        )
    with tempfile.TemporaryDirectory(prefix="oddarchiver_restore_read_") as workdir:
        source = make_session_source(backend, manifests, Path(workdir), mode=read_mode)
        if marker is None:
            return _restore_legacy(
                dest, source, crypto, manifests, session, force, paths,
                skip_volumes,
            )
        return _restore_multivolume(
            dest, source, backend, crypto, manifests, marker, session, force, paths,
            skip_volumes,
        )


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
    Output: RestoreResult with needed=None
    Details:
        Unmarked-archive path. Does not touch restore-state. Session
        slicing is the original list prefix so single-volume behaviour
        stays the same. skip_volumes still gates recovery-mode
        stale/missing.
    """
    if session is not None:
        manifests = manifests[: session + 1]
    if not manifests:
        _log.warning("No sessions found on disc; nothing to restore.")
        print("No sessions found on disc.", flush=True)
        return RestoreResult(0, 0)

    max_session = len(manifests) - 1
    print(f"  Found {max_session + 1} session(s). Reconstructing...", flush=True)
    file_chains, deleted_at = _build_chains(manifests)
    restored, failures, _updated, empty_filter, _unresolved, stale, missing = run_session_replay(
        dest, source, crypto, file_chains, deleted_at, max_session,
        force, paths, prior_files={}, allow_unresolved=False,
        recover=bool(skip_volumes),
    )
    if empty_filter:
        failures += 1
    return RestoreResult(restored, failures, stale=stale, missing=missing)


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
    Input:  dest, source, backend, crypto, manifests, marker, session, force,
            paths, skip_volumes
    Output: RestoreResult, with needed set when restore still needs an
            earlier volume
    Details:
        The restore-state key combines archive_id and --session.
        restore does not re-read already-applied volumes. File chains
        come from the current disc only. dest plus the replay dict
        supplies the base for a delta whose full blob lived on an
        earlier applied volume. A gap before the first_session of this
        volume, with no matching dest base, leaves the file unresolved
        and names the previous volume by label.
        restore treats skip_volumes indexes as permanently gone: it
        never reports them as needed, and unresolved files that only
        needed a skipped volume become missing so this volume can still
        commit.
        restore uses backend only for the mediainfo() label read in
        _commit_volume. Every blob read goes through source.
    """
    state = _load_or_init_state(dest, marker, session)
    needed = _needed_earlier_volume(marker, state, skip_volumes)

    if marker.volume_index in state.applied_volumes:
        print(
            f"  Volume {marker.volume_index} already applied; skipping.",
            flush=True,
        )
        _log.info(
            "Skipping already-applied volume %d (archive_id=%s)",
            marker.volume_index, marker.archive_id,
        )
        return RestoreResult(0, 0, needed)

    if session is not None:
        manifests = [m for m in manifests if m.session <= session]
    if not manifests:
        _log.warning("No in-range sessions found on this volume; nothing to restore.")
        print("No sessions found on disc.", flush=True)
        return RestoreResult(0, 0, needed)

    max_session = max(m.session for m in manifests)
    print(f"  Found {len(manifests)} session(s). Reconstructing...", flush=True)
    file_chains, deleted_at = _build_chains(manifests)
    restored, failures, updated_files, empty_filter, unresolved_paths, stale, missing = run_session_replay(
        dest, source, crypto, file_chains, deleted_at, max_session,
        force, paths, prior_files=state.files, allow_unresolved=True,
        recover=bool(skip_volumes),
    )
    if empty_filter:
        failures += 1

    if skip_volumes and unresolved_paths:
        still_needed = _needed_earlier_volume(marker, state, skip_volumes)
        if still_needed is None:
            for rel_path in unresolved_paths:
                _log.warning(
                    "recovery: %s is missing; needed volume is permanently skipped",
                    rel_path,
                )
            missing.extend(unresolved_paths)
            unresolved_paths = []

    if failures == 0 and not unresolved_paths:
        _commit_volume(
            state, dest, backend, marker, manifests, updated_files, deleted_at,
        )
        needed = _needed_earlier_volume(marker, state, skip_volumes)

    return RestoreResult(
        restored, failures, needed, stale=stale, missing=missing,
    )


def _load_or_init_state(
    dest: Path,
    marker: VolumeMarker,
    session: int | None,
) -> RestoreState:
    """
    Input:  dest    - restore destination
            marker  - current disc VolumeMarker
            session - --session target
    Output: existing or new RestoreState
    Details:
        Raises RuntimeError on a --session mismatch against a prior
        restore-state for the same (archive_id, dest) pair.
    """
    state = load_restore_state(dest, marker.archive_id)
    if state is None:
        return new_restore_state(dest, marker.archive_id, session)
    # state.archive_id always equals marker.archive_id here: state_path()
    # keys the file by (archive_id, dest) together, so a different
    # archive_id resolves to a different file and can never collide.
    if state.session != session:
        raise RuntimeError(
            f"Restore dest was started with --session {state.session!r}, "
            f"not {session!r}"
        )
    return state


def _commit_volume(
    state: RestoreState,
    dest: Path,
    backend: "BurnBackend",
    marker: VolumeMarker,
    manifests: list,
    updated_files: dict[str, FileReplay],
    deleted_at: dict[str, int],
) -> None:
    """
    Input:  state, dest, backend, marker, manifests, updated_files, deleted_at
    Output: None
    Details:
        Marks the current volume applied, merges replay data, and writes
        restore-state atomically. Called only after a zero-failure pass.
    """
    last_session = max(m.session for m in manifests)
    label = ""
    try:
        label = backend.mediainfo().label or ""
    except Exception:
        _log.warning("Could not read disc label while recording restore-state")
    if not label and manifests:
        label = manifests[0].label or ""

    state.dest = str(dest.resolve())
    if marker.volume_index not in state.applied_volumes:
        state.applied_volumes.append(marker.volume_index)
        state.applied_volumes = sorted(set(state.applied_volumes))
    state.volumes[marker.volume_index] = VolumeRecord(
        volume_index=marker.volume_index,
        first_session=marker.first_session,
        last_session=last_session,
        label=label,
        previous_volume_label=marker.previous_volume_label,
    )
    state.files.update(updated_files)
    for path, session_n in deleted_at.items():
        prev = state.deleted.get(path)
        if prev is None or session_n > prev:
            state.deleted[path] = session_n
        state.files.pop(path, None)
    write_restore_state(dest, state)
    _log.info(
        "Recorded volume %d as applied (%d file(s) in replay dict)",
        marker.volume_index, len(state.files),
    )


def _needed_earlier_volume(
    marker: VolumeMarker,
    state: RestoreState,
    skip_volumes: list[int] | None = None,
) -> NeededVolume | None:
    """
    Input:  marker       - current disc marker
            state        - restore-state including any volumes just applied
            skip_volumes - volume indexes marked permanently unavailable
    Output: NeededVolume for the nearest unapplied predecessor, or None
    Details:
        Walks backward via previous_volume_label / volume_index. Does not
        look forward. The walk fills in the session range when restore
        needs volume 0 or when the first_session of a later applied or
        current volume bounds it.
        restore treats indexes in skip_volumes as gone: the walk
        continues past them to an earlier still-needed volume, or stops
        if none.
    """
    skipped = set(skip_volumes or [])
    applied = set(state.applied_volumes)
    records = dict(state.volumes)
    cursor_index = marker.volume_index
    cursor_label = marker.previous_volume_label
    cursor_first = marker.first_session
    if marker.volume_index not in records:
        records[marker.volume_index] = VolumeRecord(
            volume_index=marker.volume_index,
            first_session=marker.first_session,
            last_session=marker.first_session,
            label="",
            previous_volume_label=marker.previous_volume_label,
        )

    while cursor_index > 0:
        prev_index = cursor_index - 1
        if prev_index in skipped or prev_index in applied:
            rec = records.get(prev_index)
            if rec is None:
                if prev_index in skipped:
                    cursor_index = prev_index
                    continue
                return None
            cursor_index = prev_index
            cursor_label = rec.previous_volume_label
            cursor_first = rec.first_session
            continue
        first_s: int | None = 0 if prev_index == 0 else None
        last_s: int | None = cursor_first - 1 if cursor_first > 0 else None
        prev_rec = records.get(prev_index)
        if prev_rec is not None:
            first_s = prev_rec.first_session
            last_s = prev_rec.last_session
        nxt = records.get(prev_index + 1)
        if nxt is not None:
            last_s = nxt.first_session - 1
            if not cursor_label:
                cursor_label = nxt.previous_volume_label
        earlier = [
            records[i] for i in sorted(records) if i < prev_index
        ]
        if first_s is None and earlier:
            first_s = earlier[-1].last_session + 1
        if first_s is None and prev_index == 0:
            first_s = 0
        return NeededVolume(
            volume_index=prev_index,
            label=cursor_label,
            first_session=first_s,
            last_session=last_s,
        )
    return None


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
) -> "_FileTask | _FileResult":
    """
    Input:  rel_path, chain, deleted_at, max_session - file state context
            dest, force                              - restore parameters
            prior            - replay entry from already-applied volumes
            allow_unresolved - if True, a delta with no base is "unresolved"
            recover          - if True, a target-checksum miss is stale/missing
    Output: a _FileTask ready for session-ordered replay, or a terminal
            _FileResult (skipped / failed / unresolved) already knowable
            with no disc read
    Details:
        Every check here is local: dest hashing and chain inspection,
        never a backend/source call. This is the "resolve without
        touching the disc" half of 23c. Only a file that falls through
        becomes a _FileTask the session loop then drives. Mirrors the
        preamble of the prior _process_file() exactly: deleted-path skip,
        volume-split tail detection, streamed detection plus its
        delta-after-streamed rejection, already-at-target-checksum skip,
        and delta-first-chain base resolution against dest/prior.
    """
    last_entry_session = chain[-1][0]
    del_session = deleted_at.get(rel_path, max_session + 1)
    if del_session <= max_session and del_session > last_entry_session:
        return _FileResult("skipped")

    target_checksum = chain[-1][1].result_checksum
    try:
        dest_file = safe_join_under(dest, rel_path)
    except ValueError as exc:
        _log.error("Refused %s: %s", rel_path, exc)
        return _FileResult("failed")

    restart_idx = None
    for i, (_, entry) in enumerate(chain):
        if entry.type == "full":
            restart_idx = i

    split_group = _tail_volume_chunks(chain)
    if split_group:
        return _start_volume_split_task(
            rel_path, chain, dest_file, target_checksum, force, recover,
            last_entry_session, prior, allow_unresolved,
        )

    streamed = restart_idx is not None and chain[restart_idx][1].streamed
    if streamed:
        for _, later in chain[restart_idx + 1:]:
            if later.type == "delta":
                _log.error(
                    "Streamed full entry for %s is followed by a delta; "
                    "unsupported manifest",
                    rel_path,
                )
                return _FileResult("failed")
        if not force and dest_file.exists() and _sha256_path(dest_file) == target_checksum:
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

    if not force and dest_file.exists():
        if hashlib.sha256(dest_file.read_bytes()).hexdigest() == target_checksum:
            _log.info("skip %s: already at target checksum", rel_path)
            return _FileResult("skipped")

    base: bytes | None = None
    base_session: int | None = None
    starts_with_full = any(entry.type == "full" for _, entry in chain)
    if not starts_with_full:
        if (
            prior is not None
            and prior.has_full
            and dest_file.exists()
            and hashlib.sha256(dest_file.read_bytes()).hexdigest() == prior.checksum
        ):
            base = dest_file.read_bytes()
            base_session = prior.last_session
        elif allow_unresolved:
            _log.info(
                "Cannot restore %s: earliest entry is a delta with no applied prior volume",
                rel_path,
            )
            return _FileResult("unresolved")

    entries = chain if recover else (chain[restart_idx:] if restart_idx is not None else chain)
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
) -> "_FileTask | _FileResult":
    """
    Input:  rel_path, chain - file identity and entries from this volume
            dest_file, target_checksum, force, recover, last_entry_session
            prior - replay from already-applied volumes
            allow_unresolved - True on marked multi-volume restore
    Output: a volume-split _FileTask, or a terminal _FileResult
    Details:
        Resolves skip / missing / unresolved without a disc read. The
        remaining consecutive chunks from needed_next become the task
        entries the session loop decrypts through SessionSource.
        Recovery mode reports missing when the group is incomplete.
        It never reports stale, and dest is not left as a partial file.
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

    already = -1
    if (
        prior is not None
        and prior.volume_chunk is not None
        and prior.volume_chunk_count == count
        and dest_file.exists()
    ):
        already = prior.volume_chunk

    present = {entry.volume_chunk for _, entry in group if entry.volume_chunk is not None}
    have = set(range(already + 1)) | present

    if recover:
        if have != set(range(count)):
            dest_file.unlink(missing_ok=True)
            _log.warning(
                "recovery: %s is missing; volume-split group incomplete "
                "(target session %d)",
                rel_path, last_entry_session,
            )
            return _FileResult("missing")

    dest_matches = dest_file.exists() and _sha256_path(dest_file) == target_checksum
    if dest_matches:
        if not force or present != set(range(count)):
            _log.info("skip %s: already at target checksum", rel_path)
            return _FileResult("skipped")
        already = -1

    needed_next = already + 1
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

    if not to_apply:
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
        Decrypts the current chunk independently and concatenates its
        plaintext onto dest_file. restore checksums a complete
        0..count-1 group against target_checksum. The result records an
        in-progress prefix via volume_chunk. Recovery mode reports
        missing on decrypt or checksum failure and does not leave dest
        as a partial file.
    """
    _session_n, entry = task.entries[task.idx]
    tmp_dir = Path(tempfile.mkdtemp(prefix="oddarchiver_vsplit_"))
    try:
        tmp_blob = tmp_dir / f"blob_{entry.volume_chunk}"
        tmp_plain = tmp_dir / f"plain_{entry.volume_chunk}"
        try:
            validate_blob_path(entry.file)
            reader.extract_path_to_file(entry.file, tmp_blob)
            crypto.decrypt_file(tmp_blob, tmp_plain)
        except _BLOB_ERRORS:
            _log.exception(
                "Failed to restore volume-split %s chunk %s",
                task.rel_path, entry.volume_chunk,
            )
            if task.recover or task.already_chunk < 0:
                task.dest_file.unlink(missing_ok=True)
            if task.recover:
                return _FileResult("missing")
            return _FileResult("failed")

        task.dest_file.parent.mkdir(parents=True, exist_ok=True)
        append = (
            (task.already_chunk >= 0 or task.idx > 0)
            and task.dest_file.exists()
        )
        write_mode = "ab" if append else "wb"
        with task.dest_file.open(write_mode) as outf:
            with tmp_plain.open("rb") as inf:
                shutil.copyfileobj(inf, outf)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    last_applied = entry.volume_chunk
    if last_applied is None:
        task.dest_file.unlink(missing_ok=True)
        return _FileResult("failed")
    if task.idx + 1 < len(task.entries):
        return None

    count = task.volume_chunk_count
    if last_applied + 1 == count:
        if _sha256_path(task.dest_file) == task.target_checksum:
            _log.info("restored %s", task.rel_path)
            return _FileResult(
                "restored",
                checksum=task.target_checksum,
                volume_chunk=last_applied,
                volume_chunk_count=count,
            )
        task.dest_file.unlink(missing_ok=True)
        if task.recover:
            _log.warning(
                "recovery: %s is missing; volume-split checksum failed",
                task.rel_path,
            )
            return _FileResult("missing")
        _log.error(
            "Checksum mismatch for %s: expected %s",
            task.rel_path, task.target_checksum,
        )
        return _FileResult("failed")

    _log.info(
        "restored %s chunks 0..%d of %d",
        task.rel_path, last_applied, count,
    )
    return _FileResult(
        "restored",
        checksum=task.target_checksum,
        volume_chunk=last_applied,
        volume_chunk_count=count,
    )



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
        Non-recover: mirrors the per-step behaviour of the prior
        _reconstruct(). It stops (task.stopped=True) on any read/apply
        failure, with no intermediate checksum checks. The caller
        checks the final checksum once, after the last entry.
        Recover: mirrors the per-step behaviour of the prior
        _reconstruct_verified(). Every step verifies the checksum of
        each full/delta result before the step accepts it, so
        task.current/last_valid_session only advance on a verified step
        and a broken chain keeps the last good state.
    """
    if entry.type == "full":
        data, ok = _read_full(task.rel_path, entry, reader, crypto)
        if not ok:
            task.stopped = True
            return
        if task.recover:
            if hashlib.sha256(data).hexdigest() != entry.result_checksum:
                _log.warning(
                    "recovery: full blob checksum mismatch for %s at session %d",
                    task.rel_path, session_n,
                )
                task.stopped = True
                return
            task.last_valid_session = session_n
        task.current = data
        task.has_full = True
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
    patched, ok = _apply_delta_entry(task.rel_path, entry, task.current, reader, crypto)
    if not ok:
        task.stopped = True
        return
    if task.recover:
        if hashlib.sha256(patched).hexdigest() != entry.result_checksum:
            _log.warning(
                "recovery: delta result checksum mismatch for %s at session %d",
                task.rel_path, session_n,
            )
            task.stopped = True
            return
        task.last_valid_session = session_n
    task.current = patched


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
        Additive fast path for a streamed full entry (section 29):
        decrypts straight to dest_file, file-to-file, and hashes the
        written file. Never materializes the blob in memory.
    """
    entry = task.entries[0][1]
    tmp_dir = Path(tempfile.mkdtemp(prefix="oddarchiver_stream_"))
    tmp_blob = tmp_dir / "blob"
    task.dest_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            validate_blob_path(entry.file)
            reader.extract_path_to_file(entry.file, tmp_blob)
            crypto.decrypt_file(tmp_blob, task.dest_file)
        except _BLOB_ERRORS as exc:
            _log.error("Failed to restore streamed %s: %s", task.rel_path, exc)
            task.dest_file.unlink(missing_ok=True)
            return _FileResult("failed")
        if _sha256_path(task.dest_file) == task.target_checksum:
            _log.info("restored %s", task.rel_path)
            return _FileResult("restored")
        task.dest_file.unlink(missing_ok=True)
        if not task.recover:
            _log.error(
                "Checksum mismatch for %s: expected %s",
                task.rel_path, task.target_checksum,
            )
            return _FileResult("failed")
        _log.warning(
            "recovery: %s is missing; streamed full failed checksum", task.rel_path,
        )
        return _FileResult("missing")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
        if not task.stopped and task.has_full:
            result = task.current
            if hashlib.sha256(result).hexdigest() == task.target_checksum:
                task.dest_file.parent.mkdir(parents=True, exist_ok=True)
                task.dest_file.write_bytes(result)
                _log.info("restored %s", task.rel_path)
                return _FileResult("restored")
            _log.error(
                "Checksum mismatch for %s: expected %s",
                task.rel_path, task.target_checksum,
            )
        return _FileResult("failed")

    if task.last_valid_session is None or task.current is None:
        _log.warning("recovery: %s is missing; no validated prefix", task.rel_path)
        return _FileResult("missing")

    checksum = hashlib.sha256(task.current).hexdigest()
    task.dest_file.parent.mkdir(parents=True, exist_ok=True)
    task.dest_file.write_bytes(task.current)
    if checksum == task.target_checksum:
        _log.info("restored %s", task.rel_path)
        return _FileResult("restored", recovered_at=task.last_valid_session, checksum=checksum)
    _log.warning(
        "recovery: %s is stale at session %d (target session %d)",
        task.rel_path, task.last_valid_session, task.last_entry_session,
    )
    return _FileResult("stale", recovered_at=task.last_valid_session, checksum=checksum)


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

    total_files = len(file_chains)
    print(f"  {total_files} file(s) to reconstruct. Writing to {dest} ...", flush=True)

    restored = 0
    failures = 0
    unresolved_paths: list[str] = []
    stale: list[tuple[str, int]] = []
    missing: list[str] = []
    updated_files: dict[str, FileReplay] = {}
    done = 0

    def _record(rel_path: str, outcome: _FileResult) -> None:
        nonlocal restored, failures, done
        chain = file_chains[rel_path]
        if outcome.status == "restored":
            restored += 1
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
                updated_files[rel_path] = _replay_from_chain(
                    chain, prior=prior_files.get(rel_path),
                )
        elif outcome.status == "failed":
            failures += 1
        elif outcome.status == "unresolved":
            unresolved_paths.append(rel_path)
        elif outcome.status == "stale":
            stale.append((rel_path, outcome.recovered_at or 0))
            updated_files[rel_path] = FileReplay(
                checksum=outcome.checksum,
                last_session=outcome.recovered_at or 0,
                has_full=True,
            )
        elif outcome.status == "missing":
            missing.append(rel_path)
        elif outcome.status == "skipped":
            del_session = deleted_at.get(rel_path, max_session + 1)
            last_entry_session = chain[-1][0]
            if not (del_session <= max_session and del_session > last_entry_session):
                updated_files[rel_path] = _replay_from_chain(
                    chain, prior=prior_files.get(rel_path),
                )
        done += 1
        _print_bar(done, total_files, suffix=rel_path)

    tasks: dict[str, _FileTask] = {}
    sessions_needed: dict[int, list[str]] = {}
    heap: list[int] = []

    def _schedule(rel_path: str, session_n: int) -> None:
        sessions_needed.setdefault(session_n, []).append(rel_path)
        heapq.heappush(heap, session_n)

    for rel_path, chain in file_chains.items():
        outcome_or_task = _start_file_task(
            rel_path, chain, deleted_at, max_session, dest, force,
            prior_files.get(rel_path), allow_unresolved, recover,
        )
        if isinstance(outcome_or_task, _FileResult):
            _record(rel_path, outcome_or_task)
            continue
        tasks[rel_path] = outcome_or_task
        _schedule(rel_path, outcome_or_task.entries[0][0])

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
            if task.volume_split:
                outcome = _apply_volume_split_chunk(task, reader, crypto)
                if outcome is not None:
                    del tasks[rel_path]
                    _record(rel_path, outcome)
                    continue
                task.idx += 1
                _schedule(rel_path, task.entries[task.idx][0])
                continue
            if task.streamed:
                outcome = _apply_streamed_task(task, reader, crypto)
                del tasks[rel_path]
                _record(rel_path, outcome)
                continue
            _apply_task_entry(task, entry_session, entry, reader, crypto)
            task.idx += 1
            if task.stopped or task.idx == len(task.entries):
                outcome = _finalize_task(task)
                del tasks[rel_path]
                _record(rel_path, outcome)
            else:
                _schedule(rel_path, task.entries[task.idx][0])

    for rel_path, task in list(tasks.items()):
        if task.volume_split:
            if task.recover:
                task.dest_file.unlink(missing_ok=True)
                outcome = _FileResult("missing")
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
        _record(rel_path, outcome)

    if total_files:
        print()

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
    skipped = total_files - restored - failures - unresolved - len(stale) - len(missing)
    if skipped:
        status += f", {skipped} already up to date"
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
        Skips SUSPECT manifests with a WARNING log.
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
        return crypto.decrypt(backend.read_path(entry.file)), True
    except _BLOB_ERRORS as exc:
        _log.error("Failed to read full blob for %s: %s", rel_path, exc)
        return b"", False


def _apply_delta_entry(
    rel_path: str,
    entry: ManifestEntry,
    current: bytes,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
) -> tuple[bytes, bool]:
    """
    Input:  rel_path, entry - file identity and manifest entry
            current         - bytes to patch
            backend, crypto - I/O and decryption
    Output: (patched_bytes, success_bool)
    Details:
        Validates entry.delta_file with validate_blob_path before the disc
        read. backend needs only read_path(). A _LocalDirReader satisfies
        this.
    """
    try:
        validate_blob_path(entry.delta_file)
        delta_bytes = crypto.decrypt(backend.read_path(entry.delta_file))
        return apply_delta(current, delta_bytes), True
    except _BLOB_ERRORS as exc:
        _log.error("Failed to apply delta for %s: %s", rel_path, exc)
        return b"", False


if __name__ == "__main__":
    pass
