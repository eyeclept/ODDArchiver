"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    The archive catalog joins the baseline of the inserted disc with the
    manifests of that disc. It names the disc that holds each version and
    keeps a local record of the discs that this host has seen.
"""
# Imports
from __future__ import annotations
from bisect import bisect_left, bisect_right
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.manifest import Manifest, TreeEntry
from oddarchiver.restore_state import ledger_dir
from oddarchiver.volume import (
    BaselineReadError, PathVersion, VolumeBaseline, VolumeMarker, build_volume_baseline,
    parse_blob_session, read_volume_baseline,
)

if TYPE_CHECKING:
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.disc import BurnBackend

# Globals
logger = logging.getLogger(__name__)
_log = logger
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class CatalogDisc:
    """Record one known disc and its session range."""

    volume_index: int | None
    label: str
    first_session: int | None
    last_session: int | None
    encrypted: bool | None = field(default=None, compare=False)


@dataclass
class ArchiveCatalog:
    """Store path events from earlier discs and the inserted disc."""

    archive_id: str | None
    source_volume_index: int | None
    last_session: int
    discs: dict[int, CatalogDisc]
    history: dict[str, list[PathVersion]]
    complete: bool
    _ranges: list[tuple[int, int, int]] = field(default_factory=list, repr=False, compare=False)
    _range_starts: list[int] = field(default_factory=list, repr=False, compare=False)
    _unknown: dict[int, set[str]] = field(default_factory=dict, repr=False, compare=False)
    _unknown_sessions: list[int] = field(default_factory=list, repr=False, compare=False)
    _indexed: bool = field(default=False, repr=False, compare=False)


# Functions
def _index_catalog(catalog: ArchiveCatalog) -> None:
    """Input: catalog. Output: indexed ranges and unknown events."""
    catalog._ranges = sorted(
        (disc.first_session, disc.last_session, index)
        for index, disc in catalog.discs.items()
        if disc.first_session is not None and disc.last_session is not None
    )
    catalog._range_starts = [first for first, _last, _index in catalog._ranges]
    catalog._unknown = {}
    for path, versions in catalog.history.items():
        for version in versions:
            if version.volume_index is None:
                catalog._unknown.setdefault(version.session, set()).add(path)
    catalog._unknown_sessions = sorted(catalog._unknown)
    catalog._indexed = True


def _set_disc_range(catalog: ArchiveCatalog, index: int, record: CatalogDisc) -> None:
    """Input: catalog and disc. Output: updated range index."""
    old = catalog.discs.get(index)
    if old is not None and old.first_session is not None and old.last_session is not None:
        position = bisect_left(catalog._ranges, (old.first_session, old.last_session, index))
        if position < len(catalog._ranges) and catalog._ranges[position] == (
            old.first_session, old.last_session, index
        ):
            catalog._ranges.pop(position)
            catalog._range_starts.pop(position)
    catalog.discs[index] = record
    if record.first_session is not None and record.last_session is not None:
        position = bisect_left(
            catalog._ranges, (record.first_session, record.last_session, index)
        )
        catalog._ranges.insert(position, (record.first_session, record.last_session, index))
        catalog._range_starts.insert(position, record.first_session)


def _memory_path(archive_id: str) -> Path:
    """
    Input:  archive_id - ID from volume.json
    Output: Path to the local disc record
    Details:
        Hash an ID that cannot safely form a file name.
    """
    safe = archive_id if len(archive_id) <= 64 and _SAFE_ID.fullmatch(archive_id) else hashlib.sha256(
        archive_id.encode("utf-8")
    ).hexdigest()
    return ledger_dir() / f"archive-{safe}.json"


def _parse_memory_record(raw: object, path: Path) -> tuple[dict[int, CatalogDisc], int | None, str | None]:
    """
    Input:  raw - decoded JSON record
            path - record file path, for error messages
    Output: Known discs, highest volume index, and recorded encryption mode
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    try:
        if not isinstance(raw, dict) or not isinstance(raw["discs"], dict):
            raise ValueError("invalid disc records")
        discs = {}
        for key, data in raw["discs"].items():
            index = int(key)
            disc = CatalogDisc(**data)
            if index < 0 or disc.volume_index != index or not isinstance(disc.label, str):
                raise ValueError("invalid disc index or label")
            if any(n is not None and (not isinstance(n, int) or n < 0)
                   for n in (disc.first_session, disc.last_session)):
                raise ValueError("invalid session range")
            discs[index] = disc
        highest = raw["highest_volume_index"]
        if not isinstance(highest, int) or highest < 0:
            raise ValueError("invalid highest volume index")
        mode = raw.get("encryption_mode")
        if mode is not None and mode not in ("none", "passphrase", "keyfile"):
            raise ValueError("invalid encryption mode")
        return discs, max(highest, *discs) if discs else highest, mode
    except (KeyError, TypeError, ValueError) as exc:
        _log.warning("Cannot parse local disc record at %s: %s", path, exc)
        raise RuntimeError(
            f"Cannot parse local disc record {path}: {exc}. "
            f"Move or delete this file: {path}"
        ) from exc


def _read_memory(archive_id: str) -> tuple[dict[int, CatalogDisc], int | None, str | None]:
    """
    Input:  archive_id - ID from volume.json
    Output: Known discs, highest volume index, and recorded encryption mode
    Details:
        Only a missing record is empty. Reject invalid records to protect them.
        The error names the file to move.
    """
    path = _memory_path(archive_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None, None
    except (OSError, ValueError, UnicodeError) as exc:
        _log.warning("Cannot read local disc record at %s: %s", path, exc)
        raise RuntimeError(
            f"Cannot read local disc record {path}: {exc}. "
            f"Move or delete this file: {path}"
        ) from exc
    # scbc boundary: Reject malformed disc or record fields before using them.
    return _parse_memory_record(raw, path)


def _merge_disc(old: CatalogDisc, new: CatalogDisc) -> CatalogDisc:
    """
    Input:  old, new - records for the same disc
    Output: Record with all known fields
    Details:
        Prefer the new label and fill unknown session bounds from the old record.
    """
    return CatalogDisc(
        new.volume_index,
        new.label or old.label,
        new.first_session if new.first_session is not None else old.first_session,
        new.last_session if new.last_session is not None else old.last_session,
        new.encrypted if new.encrypted is not None else old.encrypted,
    )


def _valid_writable_session_bounds(disc: CatalogDisc) -> bool:
    """
    Input:  disc - candidate local disc record
    Output: True when both session bounds are valid and ordered
    """
    for n in (disc.first_session, disc.last_session):
        if n is not None and (type(n) is not int or n < 0):
            return False
    if (disc.first_session is not None and disc.last_session is not None
            and disc.last_session < disc.first_session):
        return False
    return True


def _writable_disc_record(disc: CatalogDisc) -> bool:
    """
    Input:  disc - candidate local disc record
    Output: True when the record has a valid index and session range
    """
    if disc.volume_index is None or disc.volume_index < 0:
        return False
    if not isinstance(disc.label, str):
        return False
    return _valid_writable_session_bounds(disc)


def archive_memory_path(archive_id: str) -> Path:
    """
    Input:  archive_id - ID from volume.json
    Output: Path of the local disc record
    """
    return _memory_path(archive_id)


def remembered_encryption_mode(archive_id: str) -> str | None:
    """
    Input:  archive_id - ID from volume.json
    Output: none, passphrase, or keyfile when this host recorded the mode
    """
    _discs, _highest, mode = _read_memory(archive_id)
    return mode


def _write_disc_memory(path: Path, payload: str) -> None:
    """
    Input:  path - destination for local disc record
            payload - serialized record
    Output: None
    """
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(tmp, path)
    except OSError:
        _log.exception("Cannot write local disc record at %s", path)
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)
        raise


def remember_encryption_mode(archive_id: str, mode: str) -> None:
    """
    Input:  archive_id - ID from volume.json
            mode - none, passphrase, or keyfile
    Output: None
    Details:
        Write the mode the first time. Do not overwrite a recorded mode.
    """
    if mode not in ("none", "passphrase", "keyfile"):
        raise ValueError(f"Invalid encryption mode: {mode!r}")
    saved, highest, existing = _read_memory(archive_id)
    if existing is not None:
        if existing != mode:
            path = _memory_path(archive_id)
            raise ValueError(
                f"Local disc record {path} says this archive uses {existing} "
                f"encryption. This disc reports {mode}. Move {path} aside "
                "if you trust this disc."
            )
        return
    path = _memory_path(archive_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "highest_volume_index": highest if highest is not None else 0,
        "encryption_mode": mode,
        "discs": {str(index): dataclasses.asdict(record)
                  for index, record in sorted(saved.items())},
    }, indent=2)
    _write_disc_memory(path, payload)


def _read_catalog_baseline(
    backend: BurnBackend, crypto: CryptoBackend | None,
    manifests: list[Manifest], marker: VolumeMarker | None, label: str | None,
) -> VolumeBaseline | None:
    """
    Input:  backend, crypto, manifests - source disc and its manifests
            marker, label - source identity and optional label
    Output: Source disc baseline, or None when unavailable
    """
    if marker:
        baseline = read_volume_baseline(
            backend, crypto, marker=marker, manifests=manifests,
        )
        if marker.volume_index > 0 and baseline is None:
            if marker.format >= 2 and label is not None:
                raise BaselineReadError(
                    f"Disc volume {marker.volume_index} has format 2 and no "
                    "volume baseline."
                )
            message = (
                "Warning: this old disc has no volume baseline. "
                "The program continues."
            )
            _log.warning(message)
            print(message, file=sys.stderr)
    else:
        baseline = None
    return baseline


def _catalog_disc_records(
    replay: VolumeBaseline, baseline: VolumeBaseline | None,
    marker: VolumeMarker | None,
) -> dict[int, CatalogDisc]:
    """
    Input:  replay, baseline - collected volume histories
            marker - source disc identity, or None for a legacy disc
    Output: Disc records, including a prior v1 placeholder when needed
    """
    discs = (
        {record.volume_index: CatalogDisc(**dataclasses.asdict(record))
         for record in replay.volumes if record.volume_index is not None}
        if marker else {}
    )
    if baseline is not None and baseline.version == 1 and marker is not None:
        if marker.volume_index > 0:
            previous_index = marker.volume_index - 1
            discs.setdefault(
                previous_index,
                CatalogDisc(previous_index, marker.previous_volume_label, None, None),
            )
    return discs


def _merge_saved_catalog_discs(
    discs: dict[int, CatalogDisc], archive_id: str | None,
) -> None:
    """
    Input:  discs - records to update
            archive_id - local disc record identity, if present
    Output: Updated disc records in place
    """
    if archive_id is not None:
        saved, _highest, _mode = _read_memory(archive_id)
        for index, record in saved.items():
            if index in discs:
                merged = _merge_disc(record, discs[index])
                # scbc ignore[is-true-false-strict] None means encryption is unknown
                if record.encrypted is True:
                    merged.encrypted = True
                discs[index] = merged
            else:
                discs[index] = record


def _resolve_catalog_versions(catalog: ArchiveCatalog) -> None:
    """
    Input:  catalog - path history and disc ranges to index
    Output: Resolved version volumes and sorted history in place
    """
    _index_catalog(catalog)
    for path, versions in catalog.history.items():
        for version in versions:
            if version.volume_index is None:
                version.volume_index = volume_for_session(catalog, version.session)
                if version.volume_index is not None:
                    catalog._unknown[version.session].discard(path)
        versions.sort(key=lambda version: version.session)


def _catalog_last_session(
    manifests: list[Manifest], history: dict[str, list[PathVersion]],
) -> int:
    """
    Input:  manifests, history - source and replayed path events
    Output: Last source session, or last replayed session when source is empty
    """
    return max(
        (m.session for m in manifests),
        default=max(
            (version.session for versions in history.values() for version in versions),
            default=-1,
        ),
    )


def _catalog_complete(
    catalog: ArchiveCatalog, marker: VolumeMarker | None,
    baseline: VolumeBaseline | None,
) -> bool:
    """
    Input:  catalog, marker, baseline - resolved disc history and source
    Output: Whether all required disc and path origins are known
    """
    if marker:
        return (
            (marker.volume_index == 0 or baseline is not None)
            and all(
                version.volume_index is not None
                for versions in catalog.history.values() for version in versions
            )
        )
    return True


def build_archive_catalog(
    backend: BurnBackend, crypto: CryptoBackend | None,
    manifests: list[Manifest], marker: VolumeMarker | None,
    *, label: str | None = None,
) -> ArchiveCatalog:
    """
    Input:  backend, crypto - source disc and its decryption backend
            manifests - source disc manifests
            marker - source disc identity, or None for a legacy disc
            label - disc label override
    Output: Catalog with earlier path events and source disc events
    Details:
        Reuse baseline replay for blob sessions and v1 placeholders.
        Local records supply missing session ranges and disc names.
        A format 2 later disc with no baseline is an error when the
        caller passes a label (the --all-disks path). Without a label
        (single-disc restore resolution) the code warns and continues.
    """
    from oddarchiver.crypto import NullCrypto

    baseline = _read_catalog_baseline(backend, crypto, manifests, marker, label)
    if label is None:
        label = backend.mediainfo().label
    enc_flag = None if crypto is None else (not isinstance(crypto, NullCrypto))
    replay = build_volume_baseline(
        baseline, manifests, marker, label, encrypted=enc_flag,
    )
    history = replay.history
    discs = _catalog_disc_records(replay, baseline, marker)
    archive_id = marker.archive_id if marker else None
    _merge_saved_catalog_discs(discs, archive_id)
    source_index = marker.volume_index if marker else None
    catalog = ArchiveCatalog(
        archive_id=archive_id,
        source_volume_index=source_index,
        last_session=_catalog_last_session(manifests, history),
        discs=discs,
        history=history,
        complete=True,
    )
    _resolve_catalog_versions(catalog)
    catalog.complete = _catalog_complete(catalog, marker, baseline)
    return catalog


def _catalog_tree_version(
    path: str, version: PathVersion, current: TreeEntry | None,
    label: CatalogDisc | None,
) -> TreeEntry:
    """
    Input:  path, version - path event to materialize
            current - preceding path entry, if any
            label - disc record for this version, if known
    Output: Tree entry after applying the path event
    """
    if version.kind == "deleted":
        return TreeEntry(
            path=path, size=current.size if current else 0,
            origin_session=current.origin_session if current else version.session,
            last_modified=version.timestamp, deleted=True,
            deleted_session=version.session, volume_index=version.volume_index,
            volume_label=label.label if label else "",
        )
    else:
        return TreeEntry(
            path=path, size=version.full_size_bytes,
            origin_session=version.session, last_modified=version.timestamp,
            volume_index=version.volume_index,
            volume_label=label.label if label else "",
        )


def catalog_tree(catalog: ArchiveCatalog, at_session: int | None = None) -> list[TreeEntry]:
    """
    Input:  catalog - path history
            at_session - last session to include, or None for all sessions
    Output: Path-sorted tree with live files and tombstones
    Details:
        Preserve the last live size and origin when a delete follows it.
    """
    tree = []
    for path, versions in catalog.history.items():
        current = None
        for version in versions:
            if at_session is not None and version.session > at_session:
                break
            label = catalog.discs.get(version.volume_index)
            current = _catalog_tree_version(path, version, current, label)
        if current is not None:
            tree.append(current)
    return sorted(tree, key=lambda entry: entry.path)


def volume_for_session(catalog: ArchiveCatalog, session: int) -> int | None:
    """
    Input:  catalog - known disc ranges
            session - session number to locate
    Output: Volume index, or None if the disc range is not known
    Details:
        Use only complete ranges to avoid a false match for a v1 baseline.
    """
    if not catalog._indexed:
        _index_catalog(catalog)
    position = bisect_right(catalog._range_starts, session)
    if position == 0:
        return None
    first, last, index = catalog._ranges[position - 1]
    if session > last:
        return None
    if any(other_last >= session for _first, other_last, _index
           in catalog._ranges[:position - 1]):
        return None
    return index


def _usable_disc_manifests(
    marker: VolumeMarker, manifests: list[Manifest],
) -> tuple[list[Manifest], set[int]]:
    """
    Input:  marker, manifests - disc run and loaded manifests
    Output: Usable manifests and their distinct sessions
    """
    run_end = marker.first_session + len(manifests)
    usable = []
    seen: set[int] = set()
    for manifest in manifests:
        if manifest.suspect:
            continue
        # scbc ignore[type-equality] Reject bool as a session bound
        if (type(manifest.session) is not int
                or not marker.first_session <= manifest.session < run_end
                or manifest.session in seen):
            _log.warning("Ignoring manifest outside disc %s session run: %r",
                         marker.volume_index, manifest.session)
            continue
        seen.add(manifest.session)
        usable.append(manifest)
    return usable, seen


def _disc_manifest_events(
    usable: list[Manifest], marker: VolumeMarker,
) -> dict[str, list[PathVersion]]:
    """
    Input:  usable, marker - validated disc manifests and identity
    Output: Path events from the disc
    """
    events: dict[str, list[PathVersion]] = {}
    for manifest in usable:
        for entry in manifest.entries:
            blob_path = entry.delta_file if entry.type == "delta" else entry.file
            try:
                blob_session = parse_blob_session(blob_path) if blob_path else None
            except ValueError:
                _log.warning("Ignoring invalid blob path for %s at session %s: %r",
                             entry.path, manifest.session, blob_path)
                continue
            events.setdefault(entry.path, []).append(PathVersion(
                session=manifest.session, volume_index=marker.volume_index,
                kind=entry.type, result_checksum=entry.result_checksum,
                full_size_bytes=entry.full_size_bytes, timestamp=manifest.timestamp,
                blob_session=blob_session,
                volume_chunk=entry.volume_chunk,
                volume_chunk_count=entry.volume_chunk_count,
            ))
        for path in manifest.deleted:
            events.setdefault(path, []).append(PathVersion(
                session=manifest.session, volume_index=marker.volume_index,
                kind="deleted", result_checksum="", full_size_bytes=0,
                timestamp=manifest.timestamp, blob_session=None,
            ))
    return events


def _apply_disc_events(
    catalog: ArchiveCatalog, events: dict[str, list[PathVersion]],
) -> set[str]:
    """
    Input:  catalog, events - existing history and new disc events
    Output: Paths whose history changed
    """
    changed = set()
    for path, versions in events.items():
        history = catalog.history.get(path, [])
        updated = list(history)
        for version in versions:
            if any(
                old_version.verified and old_version.session == version.session
                and old_version.result_checksum != version.result_checksum
                for old_version in updated
            ):
                _log.warning("Ignoring conflicting verified event for %s at session %s",
                             path, version.session)
                continue
            updated = [
                old_version for old_version in updated
                if old_version.verified or old_version.session != version.session
            ]
            if version not in updated:
                updated.append(version)
        updated.sort(key=lambda version: version.session)
        if updated != history:
            catalog.history[path] = updated
            changed.add(path)
    return changed


def _resolve_learned_unknowns(
    catalog: ArchiveCatalog, marker: VolumeMarker, last_session: int,
    changed: set[str],
) -> None:
    """
    Input:  catalog, marker, last_session - known disc range
            changed - path set to update
    Output: None; unknown origins and changed paths updated in place
    """
    if last_session is not None:
        start = bisect_left(catalog._unknown_sessions, marker.first_session)
        end = bisect_right(catalog._unknown_sessions, last_session)
        for session_n in catalog._unknown_sessions[start:end]:
            index = volume_for_session(catalog, session_n)
            if index is None:
                continue
            for path in catalog._unknown[session_n]:
                for version in catalog.history[path]:
                    if version.session == session_n and version.volume_index is None:
                        version.volume_index = index
                        changed.add(path)
            catalog._unknown[session_n].clear()


def learn_disc(
    catalog: ArchiveCatalog, marker: VolumeMarker, label: str,
    manifests: list[Manifest],
) -> set[str]:
    """
    Input: catalog - history to update
           marker, label, manifests - identity and events of a collected disc
    Output: Paths whose history changed
    Details:
        The disc run is marker.first_session up to the last loaded slot.
        Only a non-SUSPECT manifest with a session in the run gives
        verified events and the disc range. The code ignores a second
        manifest for the same session. A disc with no usable manifest
        changes nothing. An event that differs from a verified event at
        the same session is a conflict, and the code ignores it.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    if not catalog._indexed:
        _index_catalog(catalog)
    usable, seen = _usable_disc_manifests(marker, manifests)
    if not usable:
        _log.warning("Disc %s has no manifest in its session run", marker.volume_index)
        return set()
    last_session = max(seen)
    old = catalog.discs.get(marker.volume_index)
    _set_disc_range(catalog, marker.volume_index, CatalogDisc(
        marker.volume_index, label, marker.first_session, last_session,
        encrypted=old.encrypted if old is not None else None,
    ))
    events = _disc_manifest_events(usable, marker)
    changed = _apply_disc_events(catalog, events)
    _resolve_learned_unknowns(catalog, marker, last_session, changed)
    return changed


def disc_name(
    catalog: ArchiveCatalog, volume_index: int | None,
    session: int | None = None,
) -> str:
    """
    Input:  catalog - known disc records
            volume_index - disc index, if known
            session - event session, if known
    Output: Name that identifies the disc or explains missing disc data
    """
    disc = catalog.discs.get(volume_index) if volume_index is not None else None
    if disc and disc.label:
        return f"{safe_display(disc.label)} (disc {volume_index})"
    if volume_index is not None:
        return f"disc {volume_index}"
    if session is not None:
        return f"the disc that holds session {session}"
    return "an earlier disc"


def safe_display(text: str) -> str:
    """
    Input: text - untrusted disc label or archive path
    Output: Text with terminal control characters escaped
    Details: Keep printable Unicode while making controls visible.
    """
    return "".join(char if char.isprintable() else f"\\u{ord(char):04x}" for char in text)


def _merge_remembered_discs(
    catalog: ArchiveCatalog, saved: dict[int, CatalogDisc],
    before: dict[int, CatalogDisc],
) -> bool:
    """
    Input:  catalog - known discs
            saved, before - mutable saved records and original records
    Output: Whether any invalid disc record was skipped
    """
    skipped = False
    for index, record in catalog.discs.items():
        if not _writable_disc_record(record) or (
            index == catalog.source_volume_index
            and (record.first_session is None or record.last_session is None)
        ):
            _log.warning("Skipping invalid disc record for volume %s", index)
            skipped = True
            continue
        saved[index] = _merge_disc(saved[index], record) if index in saved else record
        # scbc ignore[is-true-false-strict] Preserve only a known encrypted value
        if index in before and before[index].encrypted is True:
            saved[index].encrypted = True
    return skipped


def _remembered_highest_index(
    saved: dict[int, CatalogDisc], highest: int | None,
    source_volume_index: int | None,
) -> int | None:
    """
    Input:  saved, highest, source_volume_index - known disc indices
    Output: Highest known index, or None when no disc is known
    """
    seen = [*saved]
    if highest is not None:
        seen.append(highest)
    if source_volume_index is not None:
        seen.append(source_volume_index)
    if not seen:
        return None
    return max(seen)


def remember_discs(catalog: ArchiveCatalog) -> None:
    """
    Input:  catalog - known disc records
    Output: None
    Details:
        Merge saved records and write a private JSON file atomically.
        A legacy disc has no archive ID, so it has no local record.
        Skip a record with an invalid session range and print a warning.
    """
    if catalog.archive_id is None:
        return
    saved, highest, enc_mode = _read_memory(catalog.archive_id)
    before = dict(saved)
    skipped = _merge_remembered_discs(catalog, saved, before)
    if skipped:
        print(
            "Warning: the program did not save a disc record. "
            "The manifests are unreadable or the session range is invalid.",
            file=sys.stderr,
        )
    path = _memory_path(catalog.archive_id)
    new_highest = _remembered_highest_index(
        saved, highest, catalog.source_volume_index,
    )
    if new_highest is None:
        return
    if path.exists() and saved == before and new_highest == highest:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_obj: dict = {
        "highest_volume_index": new_highest,
        "discs": {str(index): dataclasses.asdict(record)
                  for index, record in sorted(saved.items())},
    }
    if enc_mode:
        payload_obj["encryption_mode"] = enc_mode
    payload = json.dumps(payload_obj, indent=2)
    _write_disc_memory(path, payload)


def remember_inserted_disc(
    marker: VolumeMarker | None, label: str, manifests: list[Manifest],
) -> None:
    """
    Input: marker, label, manifests - control data already read from this disc
    Output: None
    Details: Save the disc range without reading or replaying its baseline.
        Do not write a record from SUSPECT or empty manifests.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    if marker is None:
        return
    usable = [
        m for slot, m in enumerate(manifests, marker.first_session)
        # scbc ignore[type-equality] Reject bool as a manifest session
        if not m.suspect and type(m.session) is int and m.session == slot
    ]
    if not usable:
        _log.warning(
            "Not saving a disc record for volume %s: manifests are unreadable",
            marker.volume_index,
        )
        print(
            "Warning: the program did not save a disc record. "
            "The manifests are unreadable or the session range is invalid.",
            file=sys.stderr,
        )
        return
    last = max(m.session for m in usable)
    record = CatalogDisc(
        marker.volume_index, label, marker.first_session, last,
    )
    if not _writable_disc_record(record):
        _log.warning(
            "Not saving a disc record for volume %s: invalid session range",
            marker.volume_index,
        )
        print(
            "Warning: the program did not save a disc record. "
            "The manifests are unreadable or the session range is invalid.",
            file=sys.stderr,
        )
        return
    remember_discs(ArchiveCatalog(
        marker.archive_id, marker.volume_index, last,
        {marker.volume_index: record}, {}, False,
    ))


def highest_seen_volume(archive_id: str) -> int | None:
    """
    Input:  archive_id - archive ID from volume.json
    Output: Highest locally seen volume index, or None if unknown
    """
    _discs, highest, _mode = _read_memory(archive_id)
    return highest
