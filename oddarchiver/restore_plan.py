"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Plan file versions across discs and collect only the blobs in each plan.
    Keep encrypted cross-disc blobs outside the restore destination.
"""
# Imports
from __future__ import annotations

import errno
import dataclasses
import logging
import os
import stat
import shutil
import sys
import tempfile
from glob import escape as glob_escape
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from oddarchiver.catalog import (
    ArchiveCatalog, catalog_latest_session, disc_name,
    discs_with_unlearned_sessions, elsewhere_sessions_for_disc,
    highest_seen_volume, learn_disc, remembered_discs,
    remembered_encryption_mode, safe_display, volume_for_session,
)
from oddarchiver.diskread import READ_MODES, make_session_source
from oddarchiver.filehash import hash_file
from oddarchiver.manifest import ManifestEntry, load_session_manifests, validate_blob_path
from oddarchiver.restore_state import (
    FileReplay, RestoreState, load_restore_state, new_restore_state,
    state_path, write_restore_state,
)
from oddarchiver.session import SPACE_SAFETY_MARGIN
from oddarchiver.volume import PathVersion, read_volume_marker, session_ranges_of

if TYPE_CHECKING:
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.disc import BurnBackend

# Globals
logger = logging.getLogger(__name__)
_log = logger


# Functions

def _version_disc(catalog: ArchiveCatalog, version: PathVersion) -> int | None:
    """
    Input: catalog and one version
    Output: disc holding the blob, or None when its range is unknown
    Details: Use the blob session for deduplicated references.
    """
    session = version.blob_session if version.blob_session is not None else version.session
    index = volume_for_session(catalog, session)
    if index is not None:
        return index
    if version.verified and version.volume_index is not None and session == version.session:
        return version.volume_index
    return None


def _disc_excludes_session(disc, session: int) -> bool:
    """Input: disc range and session. Output: whether the range excludes the session."""
    ranges = session_ranges_of(
        disc.first_session, disc.last_session,
        getattr(disc, "session_ranges", None),
    )
    if not ranges:
        if disc.first_session is not None and session < disc.first_session:
            return True
        if disc.first_session is not None and disc.last_session is None:
            return True
        return False
    return all(session < first or session > last for first, last in ranges)



def _unknown_candidate(catalog: ArchiveCatalog, session: int,
                       missing: set[int]) -> int | None:
    """
    Input: catalog, unresolved session, and missing discs
    Output: next older disc to inspect, or None
    Details: A known range that excludes the session cannot hold its blob.
        A session with no unresolved path needs no discovery.
    """
    source = catalog.source_volume_index
    if source is None or not catalog._unknown.get(session):
        return None
    for index in range(source - 1, -1, -1):
        if index in missing:
            continue
        disc = catalog.discs.get(index)
        if disc is not None and _disc_excludes_session(disc, session):
            continue
        return index
    return None


def _unresolved_sessions(catalog: ArchiveCatalog) -> list[int]:
    """Input: catalog. Output: sorted sessions that have a path with no known disc."""
    return sorted(session for session, names in catalog._unknown.items() if names)


def _needed_name(catalog: ArchiveCatalog, index: int,
                 missing: set[int] | None = None) -> str:
    """Input: catalog and requested index. Output: a range-safe disc name."""
    for session in _unresolved_sessions(catalog):
        if _unknown_candidate(catalog, session, missing or set()) == index:
            return f"{disc_name(catalog, None, session)} (disc {index})"
    return disc_name(catalog, index)


def _chunk_chain_start(versions: list[PathVersion], target: int) -> int:
    """Input: ordered path history and target. Output: first matching chunk index."""
    start = target
    count = versions[target].volume_chunk_count
    while start > 0 and versions[start].volume_chunk != 0:
        previous = versions[start - 1]
        if previous.volume_chunk_count != count:
            break
        start -= 1
    return start


def _verified_full_start(versions: list[PathVersion], start: int) -> int:
    """Input: ordered path history and base hint. Output: nearest eligible verified full index."""
    if not versions[start].verified:
        for base in range(start - 1, -1, -1):
            if versions[base].kind == "deleted":
                break
            if versions[base].kind == "full" and versions[base].verified:
                start = base
                break
    return start


def _chain(versions: list[PathVersion], target: int) -> list[PathVersion]:
    """
    Input: ordered path history and target index
    Output: entries from the nearest full version, or an incomplete delta chain
    Details: An incomplete chain drives discovery, not restoration. An
        unverified full is a hint, because its real kind is not known. When
        the chain starts at a hint, the chain starts at the nearest older
        verified full instead, if one exists after the last delete. A disc
        learned in any order then supplies its blobs on its first read.
    """
    if versions[target].volume_chunk is not None:
        return versions[_chunk_chain_start(versions, target):target + 1]
    start = target
    while start > 0 and versions[start].kind != "full":
        start -= 1
    start = _verified_full_start(versions, start)
    return versions[start:target + 1]


def _chain_complete(chain: list[PathVersion]) -> bool:
    """
    Input: chain of path versions
    Output: True when the chain can restore the file
    Details: The chain starts at a full version or at chunk 0, and each
        version came from a disc manifest. A v1 placeholder only drives
        discovery. A volume-split chain is complete only when it also
        ends at the final chunk. Chunks planned from one disc alone are
        a prefix, not the whole file.
    """
    if chain[-1].volume_chunk is not None:
        count = chain[-1].volume_chunk_count
        chunks = [version.volume_chunk for version in chain]
        complete_chunks = (
            count is not None
            and len(chunks) == count
            and chunks == list(range(count))
        )
        return complete_chunks and all(version.verified for version in chain)
    starts = chain[0].kind == "full"
    return starts and all(version.verified for version in chain)


def _older_base_disc(catalog: ArchiveCatalog, first: PathVersion, lowest: int,
                     source: int, missing: set[int], learned: set[int]) -> int | None:
    """Input: catalog, first version, disc bounds, missing and learned discs. Output: older base disc."""
    for index in range(min(lowest, source) - 1, -1, -1):
        if index in missing or index in learned:
            continue
        disc = catalog.discs.get(index)
        if (disc is not None and disc.first_session is not None
                and disc.first_session >= first.session):
            continue
        return index
    return None


def _base_candidate(catalog: ArchiveCatalog, chain: list[PathVersion],
                    missing: set[int], learned: set[int]) -> int | None:
    """
    Input: catalog, chain, missing discs, discs learned in this run
    Output: next older disc that can hold the chain base, or None
    Details: A chain that starts at a delta or a later chunk needs an older
        disc. The search starts below the oldest known disc of the chain.
        It skips missing and learned discs, and discs whose range starts
        at or after the first chain session.
    """
    first = chain[0]
    if (first.volume_chunk == 0 if chain[-1].volume_chunk is not None
            else first.kind == "full"):
        return None
    source = catalog.source_volume_index
    if source is None:
        return None
    lowest = min(
        (index for index in (_version_disc(catalog, version) for version in chain)
         if index is not None),
        default=source,
    )
    return _older_base_disc(catalog, first, lowest, source, missing, learned)


def _history_through_session(history: list[PathVersion], session: int) -> list[PathVersion]:
    """Input: path history and target session. Output: versions through the session."""
    in_range = []
    seen_verified = set()
    for version in history:
        if version.session > session:
            continue
        key = (version.session, version.volume_chunk)
        if version.verified and key in seen_verified:
            continue
        in_range.append(version)
        if version.verified:
            seen_verified.add(key)
    return in_range


def _history_before_missing_gap(
    catalog: ArchiveCatalog, path: str, in_range: list[PathVersion],
    missing: set[int], prior_checksums: dict[str, tuple[int, str]] | None,
) -> list[PathVersion]:
    """Input: catalog, path history, missing discs and prior checksums. Output: usable history."""
    if prior_checksums is not None and path in prior_checksums:
        origin, checksum = prior_checksums[path]
        source = catalog.source_volume_index
        if source is not None and any(
            v.session == origin and v.verified and v.result_checksum != checksum
            and v.volume_index is not None
            and any(v.volume_index < index < source for index in missing)
            for v in in_range
        ):
            in_range = [v for v in in_range if v.session <= origin]
    return in_range


def _chain_discs_possible(
    catalog: ArchiveCatalog, chain: list[PathVersion], missing: set[int],
) -> bool:
    """Input: catalog, chain and missing discs. Output: whether each disc is available."""
    return all(
        (_version_disc(catalog, v) not in missing
         if _version_disc(catalog, v) is not None
         else _unknown_candidate(catalog, v.blob_session if v.blob_session is not None
                                 else v.session, missing) is not None)
        for v in chain
    )


def _chain_base_possible(
    catalog: ArchiveCatalog, chain: list[PathVersion],
    in_range: list[PathVersion], missing: set[int], learned: set[int],
) -> bool:
    """Input: catalog, chain, history and disc sets. Output: whether a base can be found."""
    return not (
        not _chain_complete(chain)
        and _base_candidate(catalog, chain, missing, learned) is None
        and not any(
            _unknown_candidate(catalog, version.session, missing) is not None
            for version in in_range
        )
    )


def _chain_hints_possible(
    catalog: ArchiveCatalog, chain: list[PathVersion], missing: set[int],
) -> bool:
    """Input: catalog, chain and missing discs. Output: whether unresolved versions have hints."""
    return not (
        any(not version.verified for version in chain) and not any(
            _version_disc(catalog, version) is not None
            or _unknown_candidate(catalog, version.session, missing) is not None
            for version in chain if not version.verified
        )
    )


def _usable_plan_chain(
    catalog: ArchiveCatalog, chain: list[PathVersion], in_range: list[PathVersion],
    missing: set[int], learned: set[int],
) -> bool:
    """Input: catalog, chain, history and disc sets. Output: whether the chain is usable."""
    return (
        _chain_discs_possible(catalog, chain, missing)
        and _chain_base_possible(catalog, chain, in_range, missing, learned)
        and _chain_hints_possible(catalog, chain, missing)
    )


def _choose_plan_chain(
    catalog: ArchiveCatalog, in_range: list[PathVersion],
    missing: set[int], learned: set[int],
) -> list[PathVersion] | None:
    """Input: catalog, history and disc sets. Output: newest usable chain or None."""
    live = [
        i for i, v in enumerate(in_range)
        if v.kind != "deleted" and (
            v.volume_chunk is None
            or v.volume_chunk_count is not None
            and v.volume_chunk + 1 == v.volume_chunk_count
        )
    ]
    for index in reversed(live):
        chain = _chain(in_range, index)
        if not chain:
            continue
        if not _usable_plan_chain(catalog, chain, in_range, missing, learned):
            continue
        return chain
    return None


def _plan_path_selected(path: str, paths: list[str] | None) -> bool:
    """Input: path and requested paths. Output: whether the path is requested."""
    return paths is None or any(
        path == selected or path.startswith(selected.rstrip('/') + '/')
        for selected in paths
    )


def _plan_candidate(
    catalog: ArchiveCatalog, path: str, history: list[PathVersion], session: int,
    missing: set[int], prior_checksums: dict[str, tuple[int, str]] | None,
    learned: set[int], paths: list[str] | None,
) -> tuple[list[PathVersion] | None, int | None]:
    """Input: path history, requested paths and disc state. Output: chain and newest session."""
    if not _plan_path_selected(path, paths):
        return None, None
    in_range = _history_through_session(history, session)
    if not in_range or in_range[-1].kind == "deleted":
        return None, None
    newest_session = in_range[-1].session
    in_range = _history_before_missing_gap(
        catalog, path, in_range, missing, prior_checksums,
    )
    return _choose_plan_chain(catalog, in_range, missing, learned), newest_session


def _record_plan_choice(
    path: str, chosen: list[PathVersion] | None, newest_session: int,
    selected: dict[str, list[PathVersion]], stale: list[tuple[str, int]],
    absent: list[str],
) -> None:
    """Input: path, chain, newest session and result collections. Output: updated collections."""
    if chosen is None:
        absent.append(path)
    else:
        selected[path] = chosen
        if chosen[-1].session != newest_session:
            stale.append((path, chosen[-1].session))


def _plan(catalog: ArchiveCatalog, paths: list[str] | None, session: int,
          missing: set[int], prior_checksums: dict[str, tuple[int, str]] | None = None,
          only: set[str] | None = None, learned: set[int] | None = None,
          ) -> tuple[dict[str, list[PathVersion]], list[tuple[str, int]], list[str]]:
    """
    Input: catalog, path filter, target session, missing disc indexes
           only - paths to plan, or None for all paths
           learned - discs learned in this run
    Output: available chains, stale files and missing files
    Details: An incomplete chain requests older discs but cannot restore a
        file. The plan keeps it while an older disc can still hold its base.
    """
    learned = learned if learned is not None else set()
    selected = {}
    stale = []
    absent = []
    candidates = (
        ((path, catalog.history[path]) for path in sorted(only) if path in catalog.history)
        if only is not None else sorted(catalog.history.items())
    )
    for path, history in candidates:
        chosen, newest_session = _plan_candidate(
            catalog, path, history, session, missing, prior_checksums, learned, paths,
        )
        if newest_session is not None:
            _record_plan_choice(path, chosen, newest_session, selected, stale, absent)
    return selected, stale, absent


def _replan_changed(catalog: ArchiveCatalog, paths: list[str] | None,
                    session: int, missing: set[int],
                    prior_checksums: dict[str, tuple[int, str]],
                    changed: set[str], chains: dict[str, list[PathVersion]],
                    stale: list[tuple[str, int]], absent: list[str],
                    learned: set[int],
                    ) -> tuple[dict[str, list[PathVersion]], list[tuple[str, int]], list[str]]:
    """
    Input: changed paths, current plans, discs learned in this run
    Output: plans with only the changed paths planned again
    Details: A learned disc changes the history of the changed paths only.
        The restore loop checks each other chain again before it restores.
    """
    if not changed:
        return chains, stale, absent
    updates, new_stale, new_absent = _plan(
        catalog, paths, session, missing, prior_checksums, only=changed, learned=learned,
    )
    chains.update(updates)
    for path in changed - updates.keys():
        chains.pop(path, None)
    return (
        chains,
        sorted([item for item in stale if item[0] not in changed] + new_stale),
        sorted((set(absent) - changed).union(new_absent)),
    )


def _entry_key(path: str, session: int, chunk: int | None = None) -> str:
    """
    Input: path, session, and volume chunk index (None for one blob)
    Output: stable state key
    Details:
        Keep events distinct. A volume-split file can hold several chunks
        in one session, so the chunk index is part of the key. Older state
        files used "session:path"; a miss only makes the disc read again.
    """
    return f"{session}:{chunk if chunk is not None else 0}:{path}"


def _blob(entry: ManifestEntry) -> str:
    """Input: manifest entry. Output: encrypted blob path. Details: Validate before reading."""
    path = entry.delta_file if entry.type == 'delta' else entry.file
    validate_blob_path(path)
    return path


def _check_entry(entry: ManifestEntry, version: PathVersion, disc: int, path: str) -> None:
    """Input: disc entry and catalog version. Output: None. Details: Refuse a changed version."""
    if (entry.type != version.kind or entry.result_checksum != version.result_checksum
            or (version.full_size_bytes and entry.full_size_bytes != version.full_size_bytes)):
        raise RuntimeError(
            f"Disc {disc} manifest entry for {safe_display(path)} at session {version.session} "
            "does not match the catalog checksum, size, or kind."
        )


def _dest_digest(dest: Path, path: str) -> str | None:
    """Input: dest root and relative path. Output: sha256 or None."""
    from oddarchiver.restore import HASH_CHUNK_SIZE
    from oddarchiver.manifest import safe_join_under
    try:
        dest_file = safe_join_under(dest, path)
    except ValueError:
        _log.warning("Refused destination path %s", path)
        return None
    if not dest_file.is_file():
        return None
    return hash_file(dest_file, HASH_CHUNK_SIZE).hexdigest()


def _dest_matches(dest: Path, path: str, checksum: str, digest: str | None = None) -> bool:
    """Input: dest file, catalog checksum, optional digest. Output: True when dest matches."""
    if digest is None:
        digest = _dest_digest(dest, path)
    return digest == checksum


def _spool_dir(spool: Path, create: bool = False) -> bool:
    """Input: spool path and create flag. Output: existence. Details: Reject unsafe state paths."""
    if create and not spool.exists() and not spool.is_symlink():
        spool.mkdir(mode=0o700)
    try:
        info = spool.lstat()
    except FileNotFoundError:
        _log.warning("Restore spool directory is absent: %s", spool)
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Unsafe restore spool directory: {spool}")
    spool.chmod(0o700)
    return True


def _spool_parent(spool: Path, parts: tuple[str, ...], create: bool) -> Path | None:
    """Input: spool, intermediate components and create flag. Output: safe parent or None."""
    target = spool
    for name in parts:
        target = target / name
        if create and not target.exists() and not target.is_symlink():
            target.mkdir(mode=0o700)
        try:
            info = target.lstat()
        except FileNotFoundError:
            _log.warning("Restore spool path is absent: %s", target)
            return None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError(f"Unsafe restore spool directory: {target}")
    return target


def _spool_blob(spool: Path, blob: str, create: bool = False) -> Path:
    """Input: spool and blob path. Output: safe blob target. Details: Reject links and foreign files."""
    _spool_dir(spool, create=create)
    parent = _spool_parent(spool, Path(blob).parts[:-1], create)
    target = parent / Path(blob).parts[-1] if parent is not None else spool / blob
    if parent is None:
        return target
    try:
        info = target.lstat()
    except FileNotFoundError:
        _log.warning("Restore spool blob is absent: %s", target)
        return target
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Unsafe restore spool blob: {target}")
    return target


def _store_blob(source: Path, target: Path) -> None:
    """Input: source and exclusive target. Output: stored blob. Details: Move locally or copy across disks."""
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"Restore blob target already exists: {target}")
    expected = source.stat().st_size
    try:
        os.rename(source, target)
    except OSError as exc:
        _log.warning("Cannot move restore blob %s to %s: %s", source, target, exc)
        if exc.errno != errno.EXDEV:
            raise
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output, source.open("rb") as inp:
                shutil.copyfileobj(inp, output)
            if target.stat().st_size != expected:
                raise RuntimeError(f"Restore blob copy size differs: {target}")
            source.unlink()
        except (OSError, RuntimeError):
            _log.exception("Failed to copy restore blob to %s", target)
            target.unlink(missing_ok=True)
            raise
    if target.stat().st_size != expected:
        raise RuntimeError(f"Restore blob size differs: {target}")
    os.chmod(target, 0o600)


def _ready_state(
    dest: Path, path: str, target: PathVersion, state: RestoreState,
    digests: dict[str, str | None],
) -> tuple[FileReplay | None, str | None, bool]:
    """Input: destination, path, target, state and digests. Output: prior replay, digest and skip."""
    previous = state.files.get(path)
    if path not in digests:
        digests[path] = _dest_digest(dest, path)
    digest = digests[path]
    if previous is not None and previous.last_session > target.session:
        if digest == previous.checksum:
            return previous, digest, True
        del state.files[path]
        previous = None
    if state.done_files.get(path) == target.session:
        if digest == target.result_checksum:
            return previous, digest, True
        del state.done_files[path]
    return previous, digest, False


def _collected_chain_entries(
    path: str, versions: list[PathVersion], state: RestoreState,
    temporary: Path, spool: Path,
) -> list[tuple[int, ManifestEntry]] | None:
    """Input: path, versions, state and blob roots. Output: complete collected entries or None."""
    entries = []
    for version in versions:
        raw = state.collected_entries.get(
            _entry_key(path, version.session, version.volume_chunk)
        )
        if raw is None:
            return None
        entry = ManifestEntry(**raw)
        blob = _blob(entry)
        if not (temporary / blob).is_file() and not _spool_blob(spool, blob).is_file():
            return None
        entries.append((version.session, entry))
    return entries


def _replay_root(
    entries: list[tuple[int, ManifestEntry]], spool: Path, temporary: Path,
) -> Path:
    """Input: collected entries and blob roots. Output: root containing the replay chain."""
    root = spool if all(_spool_blob(spool, _blob(e)).is_file() for _, e in entries) else temporary
    if root == temporary:
        for _, entry in entries:
            blob = _blob(entry)
            if not (temporary / blob).is_file():
                dst = temporary / blob
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_spool_blob(spool, blob), dst)
    return root


def _finish_ready_replay(
    dest_file: Path, path: str, target: PathVersion, updated: dict[str, FileReplay],
    state: RestoreState, digests: dict[str, str | None], count: int,
    failures: int, unresolved: list[str],
) -> tuple[int, int]:
    """Input: replay result and state. Output: restored and failed counts after checksum validation."""
    from oddarchiver.restore import HASH_CHUNK_SIZE

    if not failures and not unresolved:
        if not dest_file.is_file() or hash_file(dest_file, HASH_CHUNK_SIZE).hexdigest() != target.result_checksum:
            _log.warning("Restored file %s does not match the catalog checksum", path)
            dest_file.unlink(missing_ok=True)
            return 0, failures + len(unresolved) + 1
        state.files[path] = updated.get(
            path, FileReplay(target.result_checksum, target.session, True),
        )
        state.done_files[path] = target.session
        digests[path] = target.result_checksum
    return count, failures + len(unresolved)


def _skip_previous_replay(
    previous: FileReplay | None, target: PathVersion, force: bool,
    digest: str | None, state: RestoreState, path: str,
) -> bool:
    """Input: prior replay and target state. Output: whether replay can be skipped."""
    if previous is not None and previous.last_session == target.session and not force:
        if digest == target.result_checksum:
            state.done_files[path] = target.session
            return True
    return False


def _ready_chain_complete(path: str, versions: list[PathVersion]) -> bool:
    """Input: path and planned versions. Output: readiness with warning for incomplete chains."""
    if not _chain_complete(versions):
        _log.warning("Skipping incomplete or unverified chain for %s", path)
        return False
    return True


def _restore_ready(dest: Path, crypto: CryptoBackend, chains: dict[str, list[PathVersion]],
                   state: RestoreState, spool: Path, temporary: Path, force: bool,
                   dest_digests: dict[str, str | None] | None = None
                   ) -> tuple[int, int]:
    """
    Input: plans, collected encrypted blobs and state
    Output: restored and failed file counts
    Details: Reuse the checksum-checked replay engine with a local session source.
    """
    from oddarchiver.restore import run_session_replay
    from oddarchiver.manifest import safe_join_under

    class CollectedSource:
        """Read collected encrypted blobs from one local root."""
        def __init__(self, root: Path):
            """Input: local root. Output: None. Details: Retain the blob directory."""
            self.root = root

        def open_session(self, session_n: int) -> Path:
            """Input: session number. Output: blob root. Details: All blobs are local."""
            return self.root

    def _replay_ready_file(
        path: str, entries: list[tuple[int, ManifestEntry]], root: Path,
        target: PathVersion, previous: FileReplay | None, digest: str | None,
    ) -> tuple[int, int]:
        """Input: collected chain and prior state. Output: restored and failed counts."""
        dest_file = safe_join_under(dest, path)
        if _skip_previous_replay(previous, target, force, digest, state, path):
            return 0, 0
        count, failures, updated, _, unresolved, _, _ = run_session_replay(
            dest, CollectedSource(root), crypto, {path: entries}, {}, target.session,
            force, None, {}, False, dest_digests={path: digest},
        )
        return _finish_ready_replay(
            dest_file, path, target, updated, state, digests, count, failures, unresolved,
        )

    restored = failed = 0
    digests = dest_digests if dest_digests is not None else {}
    for path, versions in chains.items():
        if not _ready_chain_complete(path, versions):
            continue
        target = versions[-1]
        previous, digest, skip = _ready_state(dest, path, target, state, digests)
        if skip:
            continue
        entries = _collected_chain_entries(path, versions, state, temporary, spool)
        if entries is None:
            continue
        root = _replay_root(entries, spool, temporary)
        count, errors = _replay_ready_file(path, entries, root, target, previous, digest)
        restored += count
        failed += errors
    return restored, failed


def _marker_holds_session(marker, session: int) -> bool:
    """Input: volume marker and session. Output: whether this disc can hold it."""
    ranges = getattr(marker, "session_ranges", None)
    if ranges:
        return any(first <= session <= last for first, last in ranges)
    first = getattr(marker, "first_session", None)
    return first is not None and first <= session



def _note_current_suspects(
    inserted: object, inserted_manifests: object,
    marker: object, index: int | None, target_session: int,
) -> tuple[int, bool]:
    """Input: inserted disc state. Output: suspect failure count and whether counted."""
    if inserted is not None and inserted_manifests is not None:
        return _note_suspect_manifests(
            inserted_manifests, marker, index, target_session,
        ), True
    return 0, False


def _manual_disc_choice(
    answer: str, needed: dict[int, int],
) -> tuple[int | None, str | None]:
    """Input: m N answer and needed indexes. Output: index or error message."""
    number = answer[2:].strip()
    if not number.isdecimal() or len(number) > 6:
        return None, "Enter m and a disc number with at most six digits."
    index = int(number)
    if index not in needed:
        return None, f"Disc {index} is not in this restore plan."
    return index, None


def _unneeded_disc_message(
    marker: object, needed: dict[int, int], catalog: ArchiveCatalog,
) -> str | None:
    """Input: marker and restore plan. Output: refusal message or None."""
    unresolved = [
        session for session in _unresolved_sessions(catalog)
        if _unknown_candidate(catalog, session, set()) in needed
    ]
    if (
        marker.volume_index not in needed and not unresolved
        and marker.volume_index != catalog.source_volume_index
    ):
        name = disc_name(catalog, marker.volume_index)
        if marker.volume_index in getattr(catalog, "discs", {}):
            return f"That is {name}, already read or not in this restore plan."
        return f"That is {name}, not needed for this restore plan."
    return None


class DiscAcquisition:
    """Get a needed disc from a drive or sibling ISO. Tests can replace this object."""

    def __init__(self, initial: BurnBackend, image: Path | None = None,
                 ask: Callable[[str], str] | None = None):
        """Input: first backend, ISO path, input function. Output: disc source."""
        self.initial = initial
        self.image = Path(image) if image is not None else None
        self.ask = ask if ask is not None else input
        self.last_attempt: int | None = None
        self._siblings: dict[int, tuple[BurnBackend, object]] | None = None
        self._sibling_stem: str | None = None
        self._opened: set[str] = set()
        self._scanned = False

    def _sibling_path(self, index: int) -> Path:
        """Input: disc index. Output: conventional sibling ISO path for that index."""
        suffix = f".vol{index}" if index else ""
        return self.image.with_name(f"{self._sibling_stem}{suffix}.iso")

    def _open_sibling(self, path: Path, archive_id: str) -> None:
        """
        Input: sibling ISO path and archive identity
        Output: None
        Details: Read the marker of each path one time per restore. Keep
            the backend when the marker names this archive.
        """
        from oddarchiver.disc import ISOBackend

        if path.name in self._opened or not path.is_file():
            return
        self._opened.add(path.name)
        sibling = ISOBackend(path)
        try:
            marker = read_volume_marker(sibling)
        except (ValueError, RuntimeError, OSError) as exc:
            _log.warning("Cannot read volume.json of sibling ISO %s: %s", path, exc)
            return
        if (marker is not None and marker.archive_id == archive_id
                and marker.volume_index not in self._siblings):
            self._siblings[marker.volume_index] = (sibling, marker)

    def _scan_siblings(self, archive_id: str) -> None:
        """Input: archive identity. Output: None. Details: Read all sibling markers one time."""
        import re

        if self._scanned:
            return
        self._scanned = True
        pattern = rf"{re.escape(self._sibling_stem)}(?:\.vol\d+)?\.iso"
        for path in sorted(self.image.parent.glob(f"{glob_escape(self._sibling_stem)}*.iso")):
            if re.fullmatch(pattern, path.name):
                self._open_sibling(path, archive_id)

    def _iso_candidates(self, needed: dict[int, int], catalog: ArchiveCatalog,
                        archive_id: str) -> list[int]:
        """
        Input: needed indexes, catalog, archive identity
        Output: ordered candidate disc indexes
        """
        import re

        if self._siblings is None:
            self._sibling_stem = re.sub(r'\.vol\d+$', '', self.image.stem)
            self._siblings = {}
            self._opened.add(self.image.name)
            initial_marker = read_volume_marker(self.initial)
            if initial_marker is not None and initial_marker.archive_id == archive_id:
                self._siblings[initial_marker.volume_index] = (self.initial, initial_marker)
        unresolved = [
            session for session in _unresolved_sessions(catalog)
            if _unknown_candidate(catalog, session, set()) in needed
        ]
        candidates = sorted(needed)
        if unresolved:
            session = unresolved[0]
            self._scan_siblings(archive_id)
            holders = [
                (sibling_marker.first_session, index)
                for index, (_sibling, sibling_marker) in self._siblings.items()
                if _marker_holds_session(sibling_marker, session)
            ]
            if holders:
                holder = max(holders)[1]
                if holder in needed:
                    candidates.remove(holder)
                    candidates.insert(0, holder)
        return candidates

    def _acquire_iso(self, needed: dict[int, int], catalog: ArchiveCatalog,
                     archive_id: str) -> tuple[BurnBackend | None, int | None, bool]:
        """
        Input: needed indexes, catalog, archive identity
        Output: ISO backend, rejected index, or quit flag
        """
        candidates = self._iso_candidates(needed, catalog, archive_id)
        for index in candidates:
            self.last_attempt = index
            if index not in self._siblings:
                self._open_sibling(self._sibling_path(index), archive_id)
            sibling = self._siblings.get(index)
            if sibling is not None:
                return sibling[0], None, False
            path = self._sibling_path(index)
            if path.is_file():
                print(f"Rejected disc {index}: ISO file {path} is from another "
                      "archive or has a bad marker.", file=sys.stderr)
            else:
                print(f"Missing disc {index}: ISO file {path} does not exist.",
                      file=sys.stderr)
            return None, index, False

    def _drive_response(self, answer: str, needed: dict[int, int],
                        catalog: ArchiveCatalog, archive_id: str
                        ) -> tuple[BurnBackend | None, int | None, bool]:
        """
        Input: prompt answer, needed indexes, catalog, archive identity
        Output: drive backend, missing index, or quit flag
        """
        if answer.lower() == 'q':
            return None, None, True
        if answer.startswith('m '):
            index, error = _manual_disc_choice(answer, needed)
            if error is not None:
                print(error, file=sys.stderr)
                return None, None, False
            return None, index, False
        if answer:
            print("Press Enter after you insert a disc, or type m N or q.", file=sys.stderr)
            return None, None, False
        try:
            marker = read_volume_marker(self.initial)
        except (ValueError, RuntimeError, OSError) as exc:
            _log.warning("Cannot read volume.json of the inserted disc: %s", exc)
            print(f"Refuse this disc (unknown disc): {safe_display(str(exc))}", file=sys.stderr)
            return None, None, False
        if marker is None:
            print(
                "No readable disc in the drive: the inserted disc has no "
                "archive volume marker. Insert a disc from the list.",
                file=sys.stderr,
            )
            return None, None, False
        if marker.archive_id != archive_id:
            print(
                "That disc is from another archive. Insert a disc in the list.",
                file=sys.stderr,
            )
            return None, None, False
        message = _unneeded_disc_message(marker, needed, catalog)
        if message is not None:
            print(message, file=sys.stderr)
            return None, None, False
        return self.initial, None, False

    def acquire(self, needed: dict[int, int], catalog: ArchiveCatalog,
                archive_id: str) -> tuple[BurnBackend | None, int | None, bool]:
        """
        Input: needed disc indexes, catalog, archive identity
        Output: backend, newly missing index, or quit flag
        Details: An ISO resolves siblings. The code opens the conventional
            name of a known index directly. It reads all sibling markers
            one time, and only when a session has no known disc. A rejected
            ISO sibling counts as missing after one try. A sibling ISO whose
            marker cannot be read counts as rejected. A drive requests a
            disc at a prompt and refuses a disc whose marker cannot be read.
        """
        if self.image is not None:
            return self._acquire_iso(needed, catalog, archive_id)
        unresolved = [
            session for session in _unresolved_sessions(catalog)
            if _unknown_candidate(catalog, session, set()) in needed
        ]
        if unresolved:
            source_disc = catalog.discs.get(catalog.source_volume_index)
            label = source_disc.label if source_disc is not None and source_disc.label else archive_id
            print(
                f"Insert the disc that holds session {min(unresolved)} "
                f"of archive {safe_display(label)}."
            )
        else:
            print("Insert one of these discs, then press Enter:")
        for index, count in sorted(needed.items()):
            print(f"  {_needed_name(catalog, index)}: {_disc_request_reason(count)}")
        print("Type m N if disc N is missing. Type q to stop and save progress.")
        from oddarchiver.drive import open_tray
        open_tray(getattr(self.initial, "device", None))
        try:
            answer = self.ask('> ').strip()
        except EOFError:
            _log.warning("No input for the next disc. Saving restore progress.")
            return None, None, True
        return self._drive_response(answer, needed, catalog, archive_id)


def _disc_authenticated(catalog: ArchiveCatalog, crypto: CryptoBackend,
                        marker, manifests: list) -> bool:
    """Input: disc and loaded manifests. Output: whether this disc can be trusted."""
    from oddarchiver.crypto import NullCrypto

    encrypted = not isinstance(crypto, NullCrypto)
    # scbc ignore[is-true-false-strict] Only explicit True records known encryption; DiscRecord.encrypted is bool | None.
    known_encrypted = any(disc.encrypted is True for disc in catalog.discs.values())
    if catalog.archive_id is not None:
        known_encrypted |= remembered_encryption_mode(catalog.archive_id) in (
            "passphrase", "keyfile",
        )
    if known_encrypted and not encrypted:
        _log.warning("Refusing disc %s: this archive requires an encryption key",
                     marker.volume_index)
        return False
    if encrypted and not any(
        not manifest.suspect and type(manifest.session) is int
        and manifest.session >= 0
        for manifest in manifests
    ):
        _log.warning("Refusing disc %s: no authenticated session manifest",
                     marker.volume_index)
        return False
    return True


def _count_suspects_in_range(disc_manifests: list, first: int, index: int | None,
                             target_session: int) -> int:
    """
    Input: disc manifests, first session, disc index, restore session cutoff
    Output: count of SUSPECT manifests within the cutoff
    """
    extra = 0
    for position, m in enumerate(disc_manifests):
        session_n = m.session if m.session >= 0 else first + position
        if m.suspect and session_n <= target_session:
            print(
                f"Disc {index} has a SUSPECT manifest at session {session_n}.",
                file=sys.stderr,
            )
            extra += 1
    return extra


def _note_suspect_manifests(disc_manifests, marker, index, target_session) -> int:
    """
    Input: disc manifests, marker, disc index, restore session cutoff
    Output: failure count for SUSPECT manifests in range
    """
    first = marker.first_session if marker else 0
    extra = _count_suspects_in_range(disc_manifests, first, index, target_session)
    if disc_manifests and all(m.suspect for m in disc_manifests):
        print(
            "Every manifest on this disc is unreadable. The passphrase or "
            "key is wrong, or the disc is from another archive. The disc "
            "may also be damaged.",
            file=sys.stderr,
        )
        if extra == 0:
            extra = 1
    return extra


def _initial_missing_prompt(dest: Path, state: RestoreState, catalog: ArchiveCatalog,
                            target_session: int, acquisition: DiscAcquisition,
                            missing: set[int], skip_volumes: list[int] | None) -> bool:
    """
    Input: restore destination, state, catalog, selection and disc availability
    Output: False on EOF, True after a valid response or when no prompt is needed
    """
    if not skip_volumes and not state.missing_discs and acquisition.image is None and sys.stdin.isatty():
        while True:
            try:
                answer = acquisition.ask("Which discs do you have? Press Enter if you have all discs. Or type the numbers of the missing discs, for example: 1 3\n> ").strip()
            except EOFError:
                _log.warning("No input for available discs. Saving restore progress.")
                state.missing_discs = sorted(missing)
                write_restore_state(dest, state)
                needed_discs = {
                    index for history in catalog.history.values() for version in history
                    if version.kind != "deleted" and version.session <= target_session
                    for index in (
                        _version_disc(catalog, version)
                        if _version_disc(catalog, version) is not None
                        else _unknown_candidate(
                            catalog, version.blob_session if version.blob_session is not None
                            else version.session, missing,
                        ),
                    ) if index is not None and index not in missing
                }
                print("Restore paused. Discs still needed: " + ", ".join(
                    _needed_name(catalog, index, missing) for index in sorted(needed_discs)
                ) + ".", file=sys.stderr)
                return False
            if not answer:
                break
            parts = answer.split()
            if all(p.isdecimal() and len(p) <= 6 for p in parts):
                missing.update(map(int, parts))
                break
            print("Enter disc numbers with at most six digits, separated by spaces.", file=sys.stderr)
    return True


def _restore_selection_state(dest: Path, archive_id: str, session: int | None,
                             target_session: int, paths: list[str] | None) -> RestoreState:
    """
    Input: destination, archive identity, requested and effective selection
    Output: restore state for the selected paths and session
    """
    state = load_restore_state(dest, archive_id)
    if state is None:
        state = new_restore_state(dest, archive_id, session)
    selection = sorted(paths) if paths is not None else None
    if state.plan_paths is not None or state.plan_session is not None or state.read_discs:
        if state.plan_paths != selection or state.plan_session != target_session:
            raise RuntimeError("This destination has a different restore selection. Use another destination.")
    state.plan_paths = selection
    state.plan_session = target_session
    return state


def _initial_manifests(backend: BurnBackend, crypto: CryptoBackend,
                       catalog: ArchiveCatalog, manifests: list | None) -> tuple[object, list]:
    """
    Input: starting disc, crypto backend, catalog and optional loaded manifests
    Output: starting volume marker and authenticated manifests
    """
    marker = read_volume_marker(backend)
    inserted_manifests = manifests
    if inserted_manifests is None:
        inserted_manifests = load_session_manifests(
            backend, crypto, start_session=marker.first_session, marker=marker,
            elsewhere_sessions=elsewhere_sessions_for_disc(
                catalog, marker.volume_index if marker is not None else None,
            ),
        )
    if not _disc_authenticated(catalog, crypto, marker, inserted_manifests):
        first = marker.first_session if marker is not None else 0
        _note_suspect_manifests(
            inserted_manifests, marker,
            marker.volume_index if marker is not None else None,
            max(catalog.last_session, first),
        )

        raise RuntimeError(
            "The inserted disc has no authenticated manifest. "
            "Use the correct key for this archive."
        )
    return marker, inserted_manifests



def _pending_chains(dest: Path, chains: dict[str, list[PathVersion]],
                    failed_paths: set[str], state: RestoreState,
                    dest_digests: dict[str, str | None]) -> dict[str, list[PathVersion]]:
    """
    Input: destination, planned chains, failed paths, saved state and digest cache
    Output: chains still requiring restore
    """
    pending = {}
    for path, chain in chains.items():
        if path in failed_paths:
            continue
        if path not in dest_digests:
            dest_digests[path] = _dest_digest(dest, path)
        digest = dest_digests[path]
        if path in state.files and state.files[path].last_session > chain[-1].session:
            if digest == state.files[path].checksum:
                continue
            del state.files[path]
        if (state.done_files.get(path) == chain[-1].session
                and digest == chain[-1].result_checksum):
            continue
        state.done_files.pop(path, None)
        pending[path] = chain
    return pending


def _needed_for_pending(catalog: ArchiveCatalog, pending: dict[str, list[PathVersion]],
                        missing: set[int], learned: set[int], state: RestoreState,
                        spool: Path) -> dict[int, int]:
    """
    Input: catalog, pending chains, missing and learned discs, state and spool
    Output: number of pending paths requiring each disc
    """
    needed: dict[int, int] = {}
    for path, chain in pending.items():
        indices = set()
        for version in chain:
            index = _version_disc(catalog, version)
            if index is None:
                session_n = version.blob_session if version.blob_session is not None else version.session
                index = _unknown_candidate(catalog, session_n, missing)
            if index is not None:
                indices.add(index)
        for index in indices:
            if any(
                (_version_disc(catalog, v) is None
                 or _entry_key(path, v.session, v.volume_chunk)
                 not in state.collected_entries
                 or not _spool_blob(spool, _blob(ManifestEntry(
                     **state.collected_entries[
                         _entry_key(path, v.session, v.volume_chunk)
                     ]
                 ))).is_file())
                for v in chain
                if (_version_disc(catalog, v) == index
                    or _version_disc(catalog, v) is None and
                    _unknown_candidate(
                        catalog, v.blob_session if v.blob_session is not None
                        else v.session, missing,
                    ) == index)
            ):
                needed[index] = needed.get(index, 0) + 1
        if not _chain_complete(chain):
            base = _base_candidate(catalog, chain, missing, learned)
            if base is not None and base not in indices:
                needed[base] = needed.get(base, 0) + 1
    return needed


def _add_discovery_discs(catalog: ArchiveCatalog, pending: dict[str, list[PathVersion]],
                         previous_checksums: dict[str, tuple[int, str]], missing: set[int],
                         state: RestoreState, read_this_run: set[int],
                         needed: dict[int, int]) -> set[str]:
    """
    Input: catalog, pending paths, prior checksums and disc availability
    Output: pending paths needing discovery; add their candidate discs to needed
    """
    discovery_paths = set()
    source_index = catalog.source_volume_index
    if source_index is not None:
        for path in pending:
            prior = previous_checksums.get(path)
            if prior is None:
                continue
            origin, _checksum = prior
            for index in range(source_index - 1, -1, -1):
                disc = catalog.discs.get(index)
                if index in missing or index in state.read_discs or index in read_this_run:
                    continue
                if disc is not None and disc.first_session is not None and disc.last_session is not None:
                    if disc.last_session < origin:
                        continue
                needed[index] = needed.get(index, 0) + 1
                discovery_paths.add(path)
                break
    return discovery_paths


def _finish_without_needed(
    dest: Path, crypto: CryptoBackend, pending: dict[str, list[PathVersion]],
    state: RestoreState, spool: Path, temporary: Path, force: bool,
    dest_digests: dict[str, str | None], stale: list[tuple[str, int]],
    absent: list[str],
) -> tuple[int, int, list[tuple[str, int]], list[str]]:
    """Input: pending chains and restore state. Output: replay counts and unresolved paths."""
    incomplete = {path for path, chain in pending.items()
                  if not _chain_complete(chain)}
    if incomplete:
        stale = [item for item in stale if item[0] not in incomplete]
        absent = sorted(set(absent) | incomplete)
    count, errors = _restore_ready(dest, crypto, pending, state, spool,
                                   temporary, force, dest_digests)
    write_restore_state(dest, state)
    if not errors and _spool_dir(spool) and not incomplete:
        shutil.rmtree(spool)
    return count, errors, stale, absent


def _record_missing_disc(dest: Path, state: RestoreState, missing: set[int],
                         index: int) -> None:
    """Input: destination, state, missing discs and index. Output: persisted missing disc."""
    missing.add(index)
    state.missing_discs = sorted(missing)
    write_restore_state(dest, state)


def _acquired_marker(source: BurnBackend) -> tuple[object | None, str | None]:
    """Input: acquired disc. Output: volume marker or logged read error."""
    try:
        return read_volume_marker(source), None
    except (ValueError, RuntimeError, OSError) as exc:
        _log.warning("Cannot read volume.json on the acquired disc: %s", exc)
        return None, str(exc)


def _read_acquired_manifests(
    inserted: BurnBackend, crypto: CryptoBackend, catalog: ArchiveCatalog,
    marker: object, index: int | None, inserted_manifests: list | None,
) -> tuple[list | None, str | None]:
    """Input: disc, marker and optional manifests. Output: authenticated manifests or refusal."""
    disc_manifests = inserted_manifests
    refusal = None
    if disc_manifests is None:
        # The backend caches the root listing. A disc swap reuses the same
        # backend object, so a stale listing would report the previous
        # medium's sessions and fail the gap check.
        if hasattr(inserted, "invalidate_session_list"):
            inserted.invalidate_session_list()
        try:
            disc_manifests = load_session_manifests(
                inserted, crypto,
                start_session=marker.first_session, marker=marker,
                elsewhere_sessions=elsewhere_sessions_for_disc(
                    catalog, getattr(marker, "volume_index", None),
                ),
            )
        except (ValueError, RuntimeError, OSError) as exc:
            _log.warning("Cannot load manifests of acquired disc %s: %s", index, exc)
            refusal = str(exc)
    if refusal is None and not _disc_authenticated(catalog, crypto, marker, disc_manifests):
        refusal = ("It has no authenticated manifest. The archive can be "
                   "encrypted, or the disc is from a different archive.")
    return disc_manifests, refusal


def _requested_for_disc(
    catalog: ArchiveCatalog, pending: dict[str, list[PathVersion]],
    disc_manifests: list, index: int,
) -> tuple[list[tuple[str, int, ManifestEntry]], set[str]]:
    """Input: catalog, pending chains and disc manifests. Output: requested entries and bad paths."""
    by_event = {
        (m.session, e.path, e.volume_chunk): e
        for m in disc_manifests if not m.suspect for e in m.entries
    }
    requested = []
    bad_paths = set()
    for path, chain in pending.items():
        for version in chain:
            if _version_disc(catalog, version) != index:
                continue
            entry = by_event.get((version.session, path, version.volume_chunk))
            if entry is None:
                print(
                    f"Disc {index} has no manifest entry for "
                    f"{safe_display(path)} at session {version.session}.",
                    file=sys.stderr,
                )
                bad_paths.add(path)
                break
            try:
                _check_entry(entry, version, index, path)
            except RuntimeError as exc:
                _log.warning("Refusing changed disc entry: %s", exc)
                print(str(exc), file=sys.stderr)
                bad_paths.add(path)
                break
            requested.append((path, version.session, entry))
    requested = [item for item in requested if item[0] not in bad_paths]
    return requested, bad_paths


def _ready_for_disc(
    catalog: ArchiveCatalog, pending: dict[str, list[PathVersion]],
    bad_paths: set[str], discovery_paths: set[str],
) -> tuple[dict[str, list[PathVersion]], bool]:
    """Input: pending chains and excluded paths. Output: ready chains and spool requirement."""
    ready = {path: chain for path, chain in pending.items()
             if path not in bad_paths and path not in discovery_paths
             and _chain_complete(chain)}
    across_discs = bool(discovery_paths) or len(ready) < len(pending) - len(bad_paths) or any(
        len({_version_disc(catalog, v) for v in chain}) > 1
        for chain in ready.values()
    )
    return ready, across_discs


def _selected_disc_manifests(
    disc_manifests: list, requested: list[tuple[str, int, ManifestEntry]],
) -> list:
    """Input: manifests and validated requests. Output: copies containing only requested blobs."""
    requested_by_session: dict[int, set[tuple[str, str]]] = {}
    for path, session_n, entry in requested:
        requested_by_session.setdefault(session_n, set()).add((path, _blob(entry)))
    return [
        dataclasses.replace(m, entries=[
            e for e in m.entries
            if (e.path, _blob(e)) in requested_by_session.get(m.session, set())
        ])
        for m in disc_manifests if m.session in requested_by_session
    ]


def _request_read_mode(read_mode: str | None, declared: int, available: int) -> str:
    """Input: requested mode and space estimates. Output: selected read mode."""
    if read_mode is None:
        return "full-copy" if declared < available else "sequential"
    if read_mode in READ_MODES:
        return read_mode
    raise ValueError(
        f"Unknown --read-mode {read_mode!r}; expected one of {READ_MODES}"
    )


def _store_session_blobs(
    source: object, selected_manifests: list, index: int, available: int,
    spool: Path, storage: Path, across_discs: bool,
) -> None:
    """Input: session source, manifests and storage paths. Output: validated stored blobs."""
    actual_total = 0
    for manifest in selected_manifests:
        root = source.open_session(manifest.session)
        session_blobs = {_blob(entry) for entry in manifest.entries}
        actual_sizes = {}
        for blob in session_blobs:
            extracted = root / blob
            if not extracted.is_file():
                raise RuntimeError(f"Disc {index} has no encrypted blob {blob}.")
            actual_sizes[blob] = extracted.stat().st_size
        actual_total += sum(actual_sizes.values())
        if actual_total > available:
            raise RuntimeError(f"Not enough local space to read disc {index}.")
        for blob, expected in actual_sizes.items():
            target = (_spool_blob(spool, blob, create=True) if across_discs
                      else storage / blob)
            if target.is_symlink():
                raise RuntimeError(f"Unsafe restore blob target: {target}")
            if target.exists():
                if across_discs:
                    _spool_blob(spool, blob)
                if not target.is_file() or target.stat().st_size != expected:
                    raise RuntimeError(f"Restore blob target already exists: {target}")
                continue
            if not across_discs:
                target.parent.mkdir(parents=True, exist_ok=True)
            _store_blob(root / blob, target)


def _record_requested_entries(
    requested: list[tuple[str, int, ManifestEntry]], across_discs: bool,
    spool: Path, storage: Path, state: RestoreState, index: int,
) -> None:
    """Input: requested entries, blob roots and restore state. Output: verified recorded entries."""
    for path, session_n, entry in requested:
        if not (_spool_blob(spool, _blob(entry)) if across_discs
                else storage / _blob(entry)).is_file():
            raise RuntimeError(f"Disc {index} has no encrypted blob for {path} at session {session_n}.")
        state.collected_entries[
            _entry_key(path, session_n, entry.volume_chunk)
        ] = dataclasses.asdict(entry)
    if index not in state.read_discs:
        state.read_discs.append(index)


def _disc_request_reason(count: int) -> str:
    """Input: pending file count. Output: prompt text for that disc."""
    if count:
        return f"{count} file(s)"
    return "manifest check (no blobs needed)"


def _volume_indexes_from_marker(marker: object) -> set[int]:
    """Input: volume marker. Output: this disc and earlier discs it names."""
    if marker is None:
        return set()
    index = getattr(marker, "volume_index", None)
    # scbc ignore[type-equality] Reject bool as a volume index
    if type(index) is not int or index < 0:
        return set()
    known = {index}
    known.update(range(index))
    if getattr(marker, "previous_volume_label", "") and index > 0:
        known.add(index - 1)
    return known


def _volume_indexes_from_records(archive_id: str | None) -> set[int]:
    """Input: archive id. Output: discs from local records and highest index."""
    if not archive_id:
        return set()
    known = set(remembered_discs(archive_id))
    highest = highest_seen_volume(archive_id)
    if highest is not None:
        known.update(range(highest + 1))
    return known


def _known_restore_volumes(catalog: ArchiveCatalog, marker: object) -> set[int]:
    """
    Input: catalog and a volume marker
    Output: volume indexes known from catalog, records, labels, and ranges
    """
    known = set(catalog.discs)
    source = catalog.source_volume_index
    if source is not None and source >= 0:
        known.add(source)
        known.update(range(source))
    known |= _volume_indexes_from_marker(marker)
    known |= _volume_indexes_from_records(catalog.archive_id)
    return known


def _queue_manifest_checks(
    needed: dict[int, int], catalog: ArchiveCatalog, marker: object,
    missing: set[int], checked: set[int],
) -> None:
    """Input: empty needed map and known discs. Output: unread volumes queued."""
    if needed:
        return
    for index in sorted(_known_restore_volumes(catalog, marker)):
        if index not in missing and index not in checked:
            needed[index] = 0


def _report_unchecked_volumes(
    catalog: ArchiveCatalog, marker: object, checked: set[int], missing: set[int],
) -> None:
    """Input: known and checked discs. Output: stderr list of unread volumes."""
    unchecked = sorted(
        index for index in _known_restore_volumes(catalog, marker)
        if index not in checked
    )
    if not unchecked:
        return
    names = ", ".join(
        _needed_name(catalog, index, missing) for index in unchecked
    )
    print(
        f"Unchecked volumes: {names}. The restore result may be incomplete.",
        file=sys.stderr,
    )

def _latest_plan_session(
    session: int | None, catalog: ArchiveCatalog, state: RestoreState,
    current: int,
) -> int:
    """Input: requested session and catalog. Output: plan session when unbounded."""
    if session is None:
        current = catalog_latest_session(catalog)
        state.plan_session = current
    return current


def _record_learned_disc(
    index: int, learned: set[int], checked: set[int], state: RestoreState,
) -> bool:
    """Input: disc index and restore tracking sets. Output: whether this is the first learn."""
    first_learn = index not in learned
    learned.add(index)
    checked.add(index)
    if index not in state.read_discs:
        state.read_discs.append(index)
    return first_learn


def _queue_unlearned_sessions(
    needed: dict[int, int], catalog: ArchiveCatalog,
    learned: set[int], missing: set[int], read_this_run: set[int],
) -> None:
    """Input: needed map and learn state. Output: unread unlearned discs queued."""
    for extra in discs_with_unlearned_sessions(catalog, learned, missing):
        if extra not in read_this_run:
            needed[extra] = needed.get(extra, 0) + 1


def _disc_already_served(
    index: int | None, needed: dict[int, int], read_this_run: set[int],
) -> bool:
    """Input: current disc and needed map. Output: whether this disc was already used."""
    return index not in needed or index in read_this_run


def _current_restore_disc(
    inserted: object, catalog: ArchiveCatalog,
) -> tuple[object | None, int | None]:
    """Input: inserted backend and catalog. Output: marker and archive volume index."""
    marker = read_volume_marker(inserted) if inserted is not None else None
    index = (
        marker.volume_index
        if marker and marker.archive_id == catalog.archive_id
        else None
    )
    return marker, index


def _complete_when_blobs_ready(
    needed: dict[int, int], catalog: ArchiveCatalog, marker: object,
    missing: set[int], checked: set[int], dest: Path, crypto: CryptoBackend,
    pending: dict[str, list[PathVersion]], state: RestoreState, spool: Path,
    temporary: Path, force: bool, dest_digests: dict[str, str | None],
    stale: list[tuple[str, int]], absent: list[str],
) -> tuple[int, int, list[tuple[str, int]], list[str]] | None:
    """Input: needed discs after blob plan. Output: finish counts or None to keep reading."""
    if needed:
        return None
    _queue_manifest_checks(needed, catalog, marker, missing, checked)
    if needed:
        return None
    return _finish_without_needed(
        dest, crypto, pending, state, spool, temporary, force,
        dest_digests, stale, absent,
    )



def restore_archive(dest: Path, backend: BurnBackend, crypto: CryptoBackend,
                    catalog: ArchiveCatalog, paths: list[str] | None = None,
                    session: int | None = None, force: bool = False,
                    skip_volumes: list[int] | None = None,
                    unskip_volumes: list[int] | None = None,
                    read_mode: str | None = None,
                    manifests: list | None = None,
                    acquisition: DiscAcquisition | None = None):
    """
    Input: inserted disc, archive catalog, selection and acquisition policy
            read_mode - sequential, full-copy, or None to choose from free space
    Output: RestoreResult, with saved work after a quit
    Details: Collect one batch per needed disc and reconstruct completed chains.
        Honor --read-mode. Count real blob bytes during extract. A load
        error on the starting disc stops the restore. A load error on an
        acquired disc refuses only that disc. After blob collection, read
        manifests of every known volume that is not missing.
    """
    from oddarchiver.restore import RestoreResult, _mkdir_private
    if catalog.archive_id is None:
        raise ValueError("--all-disks needs a disc with volume.json.")
    dest = Path(dest)
    _mkdir_private(dest)
    target_session = session if session is not None else catalog_latest_session(catalog)
    unmatched = [
        path for path in (paths or [])
        if not any(
            name == path or name.startswith(path.rstrip('/') + '/')
            for name in catalog.history
        )
    ]
    for path in unmatched:
        print(
            f"warning: no archive entry matches '{safe_display(path)}' "
            f"as of session {target_session}",
            flush=True,
        )
    state = _restore_selection_state(dest, catalog.archive_id, session, target_session, paths)
    missing = set(state.missing_discs)
    missing.update(skip_volumes or ())
    missing.difference_update(unskip_volumes or ())
    acquisition = acquisition or DiscAcquisition(backend)
    if not _initial_missing_prompt(dest, state, catalog, target_session, acquisition,
                                   missing, skip_volumes):
        return RestoreResult(0, 0, paused=True)
    state.missing_discs = sorted(missing)
    write_restore_state(dest, state)
    spool = state_path(dest, catalog.archive_id).with_suffix('.spool')
    restored = 0
    failures = int(bool(paths and len(unmatched) == len(paths)))
    read_this_run: set[int] = set()
    stale: list[tuple[str, int]] = []
    absent: list[str] = []
    inserted = backend
    marker, inserted_manifests = _initial_manifests(backend, crypto, catalog, manifests)
    previous_checksums = {
        path: (version.session, version.result_checksum)
        for path, versions in catalog.history.items() for version in versions
        if not version.verified
    }
    learn_disc(catalog, marker, backend.mediainfo().label, inserted_manifests)
    learned: set[int] = {marker.volume_index}
    checked_manifests: set[int] = {marker.volume_index, *state.read_discs}
    start_marker = marker
    paused = False
    failed_paths: set[str] = set()
    dest_digests: dict[str, str | None] = {}

    def replan() -> tuple[dict[str, list[PathVersion]], list[tuple[str, int]], list[str]]:
        """Input: None. Output: a full plan for the current catalog and missing discs."""
        nonlocal target_session
        target_session = _latest_plan_session(
            session, catalog, state, target_session,
        )
        return _plan(catalog, paths, target_session, missing, previous_checksums,
                     learned=learned)


    def refuse(disc: int | None, reason: str) -> None:
        """
        Input: refused disc index or None, and the reason
        Output: None
        Details: Tell the user that the restore does not use this disc. An
            ISO sibling counts as missing and the plan changes. A drive
            asks for a disc again.
        """
        nonlocal chains, stale, absent
        _log.warning("Refusing disc %s: %s", disc, reason)
        name = f"disc {disc}" if disc is not None else "unknown disc"
        print(f"Refuse this disc ({name}): {safe_display(reason)}", file=sys.stderr)
        if acquisition.image is not None and disc is not None:
            _record_missing_disc(dest, state, missing, disc)
            chains, stale, absent = replan()

    def reject_unneeded(index: int | None) -> None:
        """Input: unwanted disc index. Output: refusal and persisted ISO absence."""
        nonlocal chains, stale, absent
        print('This disc is from another archive or is not needed.', file=sys.stderr)
        if acquisition.image is not None:
            lost_index = getattr(acquisition, "last_attempt", None)
            if lost_index is None:
                lost_index = index
            if lost_index is not None:
                _record_missing_disc(dest, state, missing, lost_index)
                chains, stale, absent = replan()

    def acquire_next(
        needed: dict[int, int], pending: dict[str, list[PathVersion]],
    ) -> tuple[str, BurnBackend | None, object | None, int | None]:
        """Input: needed discs and pending chains. Output: acquisition status and disc marker."""
        nonlocal chains, stale, absent
        source, lost, quit_now = acquisition.acquire(needed, catalog, catalog.archive_id)
        if quit_now:
            print('Restore paused. Discs still needed: ' + ', '.join(
                _needed_name(catalog, n, missing) for n in sorted(needed)) + '.', file=sys.stderr)
            write_restore_state(dest, state)
            return "paused", None, None, None
        if lost is not None:
            print(
                f"Disc {lost} is missing. Files that need it will be stale or missing.",
                file=sys.stderr,
            )
            _record_missing_disc(dest, state, missing, lost)
            chains, stale, absent = replan()
            return "retry", None, None, None
        if source is None:
            return "retry", None, None, None
        acquired_marker, marker_error = _acquired_marker(source)
        if marker_error is not None:
            refuse(getattr(acquisition, "last_attempt", None)
                   if acquisition.image is not None else None, marker_error)
            return "retry", None, None, None
        acquired_index = (acquired_marker.volume_index
                          if acquired_marker and acquired_marker.archive_id == catalog.archive_id
                          else None)
        discovering = (
            acquired_index is not None and acquired_index not in missing
            and acquired_index not in learned
            and acquired_index < catalog.source_volume_index
            and any(not _chain_complete(chain) for chain in pending.values())
        )
        if acquired_index not in needed and not discovering:
            reject_unneeded(acquired_index)
            return "retry", None, None, None
        return "ready", source, acquired_marker, acquired_index

    def learn_current_disc(index: int, marker: object, inserted: BurnBackend,
                           disc_manifests: list) -> bool:
        """Input: current disc and manifests. Output: whether its catalog change needs replanning."""
        nonlocal chains, stale, absent, target_session
        previous_disc = catalog.discs.get(index)
        changed_paths = learn_disc(
            catalog, marker, inserted.mediainfo().label, disc_manifests,
        )
        first_learn = _record_learned_disc(
            index, learned, checked_manifests, state,
        )
        target_session = _latest_plan_session(
            session, catalog, state, target_session,
        )
        if changed_paths or previous_disc != catalog.discs.get(index) or first_learn:
            changed_paths |= {
                path for path, chain in chains.items() if not _chain_complete(chain)
            }
            chains, stale, absent = _replan_changed(
                catalog, paths, target_session, missing, previous_checksums,
                changed_paths, chains, stale, absent, learned,
            )
            return True
        return False

    def process_current_disc(
        disc_manifests: list, marker: object, index: int, counted_suspects: bool,
        pending: dict[str, list[PathVersion]], discovery_paths: set[str], temporary: Path,
    ) -> tuple[int, int, int, bool]:
        """Input: current disc, plan and temporary root. Output: restored, failures, replay errors and suspect status."""
        added_failures = 0
        if not counted_suspects:
            added_failures += _note_suspect_manifests(
                disc_manifests, marker, index, target_session,
            )
        if disc_manifests and all(m.suspect for m in disc_manifests):
            failed_paths.update(
                path for path, chain in pending.items()
                if any(_version_disc(catalog, version) == index for version in chain)
            )
            read_this_run.add(index)
            return 0, added_failures, 0, True
        requested, bad_paths = _requested_for_disc(catalog, pending, disc_manifests, index)
        added_failures += len(bad_paths)
        failed_paths.update(bad_paths)
        ready, across_discs = _ready_for_disc(catalog, pending, bad_paths, discovery_paths)
        if across_discs:
            _spool_dir(spool, create=True)
        selected_manifests = _selected_disc_manifests(disc_manifests, requested)
        storage = spool if across_discs else temporary
        if requested:
            declared = sum(
                entry.full_size_bytes if entry.type == "full" else entry.delta_size_bytes
                for _, _, entry in requested
            )
            available = int(shutil.disk_usage(storage).free * SPACE_SAFETY_MARGIN)
            mode = _request_read_mode(read_mode, declared, available)
            source = make_session_source(
                inserted, selected_manifests, temporary / "source",
                mode=mode, max_bytes=available,
            )
            _store_session_blobs(
                source, selected_manifests, index, available, spool, storage, across_discs,
            )
        _record_requested_entries(requested, across_discs, spool, storage, state, index)
        read_this_run.add(index)
        count, errors = _restore_ready(dest, crypto, ready, state, spool,
                                       temporary, force, dest_digests)
        write_restore_state(dest, state)
        return count, added_failures + errors, errors, False


    def run_restore_iteration(temporary: Path) -> str:
        """Input: temporary root for one disc iteration. Output: again, finished, paused or failed."""
        nonlocal inserted, inserted_manifests, chains, stale, absent, restored, failures
        # Replan after learning an acquired disc or marking a disc missing.
        pending = _pending_chains(dest, chains, failed_paths, state, dest_digests)
        needed = _needed_for_pending(catalog, pending, missing, learned, state, spool)
        _queue_unlearned_sessions(
            needed, catalog, learned, missing, read_this_run,
        )
        discovery_paths = _add_discovery_discs(
            catalog, pending, previous_checksums, missing, state, read_this_run, needed,
        )
        marker, index = _current_restore_disc(inserted, catalog)
        suspect_failures, counted_suspects = _note_current_suspects(
            inserted, inserted_manifests, marker, index, target_session,
        )
        failures += suspect_failures
        finished = _complete_when_blobs_ready(
            needed, catalog, marker or start_marker, missing, checked_manifests,
            dest, crypto, pending, state, spool, temporary, force, dest_digests,
            stale, absent,
        )
        if finished is not None:
            count, errors, stale, absent = finished
            restored += count
            failures += errors
            return "finished"
        if _disc_already_served(index, needed, read_this_run):
            outcome, inserted, marker, index = acquire_next(needed, pending)
            if outcome == "paused":
                return "paused"
            if outcome == "retry":
                return "again"
            inserted_manifests = None
            counted_suspects = False
        disc_manifests, refusal = _read_acquired_manifests(
            inserted, crypto, catalog, marker, index, inserted_manifests,
        )
        inserted_manifests = None
        if refusal is not None:
            refuse(index, refusal)
            inserted = None
            return "again"
        if learn_current_disc(index, marker, inserted, disc_manifests):
            inserted_manifests = disc_manifests
            return "again"
        count, added_failures, replay_errors, all_suspect = process_current_disc(
            disc_manifests, marker, index, counted_suspects, pending,
            discovery_paths, temporary,
        )
        restored += count
        failures += added_failures
        if all_suspect:
            inserted = None
            return "again"
        if replay_errors:
            return "failed"
        inserted = None
        return "again"


    chains, stale, absent = replan()
    while True:
        with tempfile.TemporaryDirectory(prefix='oddarchiver_restore_') as tmp:
            outcome = run_restore_iteration(Path(tmp))
            if outcome == "again":
                continue
            paused = outcome == "paused"
            break
    _report_unchecked_volumes(
        catalog, start_marker, checked_manifests, missing,
    )
    return RestoreResult(restored, failures, stale=stale, missing=absent, paused=paused)
