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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.manifest import Manifest, TreeEntry
from oddarchiver.restore_state import ledger_dir
from oddarchiver.volume import (
    BaselineReadError, PathVersion, VolumeBaseline, VolumeMarker, VolumeRecord,
    build_volume_baseline, compact_session_ranges, parse_blob_session,
    parse_session_ranges, read_volume_baseline, session_ranges_of,
)

if TYPE_CHECKING:
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.disc import BurnBackend

# Globals
logger = logging.getLogger(__name__)
_log = logger
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


_CATALOG_RECORD_KEYS = frozenset({"discs", "highest_volume_index", "encryption_mode"})


def _known_fields(cls, data: dict) -> dict:
    names = {item.name for item in dataclasses.fields(cls)}
    extra = [key for key in data if key not in names]
    if extra:
        logger.debug("Ignoring unknown %s keys: %s", cls.__name__, extra)
    return {key: value for key, value in data.items() if key in names}


def _log_unknown_keys(kind: str, data: dict, known: frozenset[str]) -> None:
    extra = [key for key in data if key not in known]
    if extra:
        logger.debug("Ignoring unknown %s keys: %s", kind, extra)


@dataclass
class CatalogDisc:
    """Record one known disc and its session ranges."""

    volume_index: int | None
    label: str
    first_session: int | None
    last_session: int | None
    encrypted: bool | None = field(default=None, compare=False)
    session_ranges: list[list[int]] | None = None
    used_bytes: int | None = field(default=None, compare=False)
    remaining_bytes: int | None = field(default=None, compare=False)
    appendable: bool | None = field(default=None, compare=False)
    measured_at: str | None = field(default=None, compare=False)
    file_count: int | None = field(default=None, compare=False)
    appendable_reason: str | None = field(default=None, compare=False)



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
def _disc_ranges(disc: CatalogDisc) -> list[tuple[int, int]]:
    """Input: disc record. Output: inclusive session pairs for that disc."""
    return session_ranges_of(disc.first_session, disc.last_session, disc.session_ranges)


def _stored_session_ranges(sessions: set[int]) -> list[list[int]] | None:
    """Input: sessions on one disc. Output: gapped pairs, or None when contiguous."""
    ranges = compact_session_ranges(sessions)
    if len(ranges) > 1:
        return ranges
    return None


def _range_holes(ranges: list[tuple[int, int]]) -> set[int]:
    """Input: inclusive pairs. Output: sessions between min and max that are absent."""
    covered: set[int] = set()
    for first, last in ranges:
        covered.update(range(first, last + 1))
    if not covered:
        return set()
    return set(range(min(covered), max(covered) + 1)) - covered


def _disc_sessions(disc: CatalogDisc) -> set[int]:
    """Input: disc record. Output: sessions in its stored ranges."""
    sessions: set[int] = set()
    for first, last in _disc_ranges(disc):
        sessions.update(range(first, last + 1))
    return sessions


def _explicit_range_sessions(disc: CatalogDisc) -> set[int]:
    """Input: disc record. Output: sessions from session_ranges only."""
    sessions: set[int] = set()
    for first, last in session_ranges_of(None, None, disc.session_ranges):
        sessions.update(range(first, last + 1))
    return sessions


def _merge_disc_sessions(old: CatalogDisc, new: CatalogDisc) -> set[int]:
    """Input: two records for one disc. Output: union that keeps explicit holes."""
    if old.session_ranges or new.session_ranges:
        old_part = (
            _explicit_range_sessions(old) if old.session_ranges else _disc_sessions(old)
        )
        new_part = (
            _explicit_range_sessions(new) if new.session_ranges else _disc_sessions(new)
        )
        return old_part | new_part
    return _disc_sessions(old) | _disc_sessions(new)


def elsewhere_sessions_for_disc(
    catalog: ArchiveCatalog, volume_index: int | None,
) -> set[int]:
    """
    Input:  catalog and the disc being loaded
    Output: sessions that do not belong on that disc
    """
    elsewhere: set[int] = set()
    for index, disc in catalog.discs.items():
        if index == volume_index:
            elsewhere |= _range_holes(_disc_ranges(disc))
            continue
        elsewhere |= _disc_sessions(disc)
    return elsewhere


def later_sessions_by_disc(
    catalog: ArchiveCatalog, through_session: int,
) -> list[tuple[int, int]]:
    """
    Input: catalog and the last session on the starting disc
    Output: (volume_index, last session) for discs that hold a later session
    """
    found: list[tuple[int, int]] = []
    for index, disc in sorted(catalog.discs.items()):
        extra = [
            last for _first, last in _disc_ranges(disc)
            if last > through_session
        ]
        if extra:
            found.append((index, max(extra)))
    return found


def discs_with_unlearned_sessions(
    catalog: ArchiveCatalog, learned: set[int], missing: set[int],
) -> list[int]:
    """
    Input: catalog, discs already learned, and missing discs
    Output: discs whose stored ranges include a session with no history event
    """
    known = {
        version.session
        for versions in catalog.history.values()
        for version in versions
    }
    extra: list[int] = []
    for index, disc in catalog.discs.items():
        if index in learned or index in missing:
            continue
        if any(
            session not in known
            for first, last in _disc_ranges(disc)
            for session in range(first, last + 1)
        ):
            extra.append(index)
    return extra


def catalog_latest_session(catalog: ArchiveCatalog) -> int:
    """Input: catalog. Output: max session in history, manifests, or disc ranges."""
    latest = catalog.last_session
    for disc in catalog.discs.values():
        for _first, last in _disc_ranges(disc):
            if last > latest:
                latest = last
    return latest




def _index_disc_ranges(catalog: ArchiveCatalog) -> None:
    """Input: catalog. Output: sorted range index rebuilt in place."""
    catalog._ranges = sorted(
        (first, last, index)
        for index, disc in catalog.discs.items()
        for first, last in _disc_ranges(disc)
    )
    catalog._range_starts = [first for first, _last, _index in catalog._ranges]


def _index_catalog(catalog: ArchiveCatalog) -> None:
    """Input: catalog. Output: indexed ranges and unknown events."""
    _index_disc_ranges(catalog)
    catalog._unknown = {}
    for path, versions in catalog.history.items():
        for version in versions:
            if version.volume_index is None:
                catalog._unknown.setdefault(version.session, set()).add(path)
    catalog._unknown_sessions = sorted(catalog._unknown)
    catalog._indexed = True


def _set_disc_range(catalog: ArchiveCatalog, index: int, record: CatalogDisc) -> None:
    """Input: catalog and disc. Output: updated range index."""
    catalog.discs[index] = record
    if catalog._indexed:
        _index_disc_ranges(catalog)


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


def archive_sync_lock_path(archive_id: str) -> Path:
    """
    Input:  archive_id - ID from volume.json
    Output: Path of the exclusive sync lock for this archive
    """
    return _memory_path(archive_id).with_suffix(".sync.lock")


def _validate_catalog_disc(disc: CatalogDisc) -> None:
    """Input: CatalogDisc built from JSON. Output: None; raises on invalid fields."""
    for name in ("used_bytes", "remaining_bytes", "file_count"):
        value = getattr(disc, name)
        # scbc ignore[type-equality] Reject bool as a byte or file count
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"invalid {name}")
    # scbc ignore[type-equality] Require a JSON boolean
    if disc.appendable is not None and type(disc.appendable) is not bool:
        raise ValueError("invalid appendable")
    if disc.appendable_reason is not None and not isinstance(disc.appendable_reason, str):
        raise ValueError("invalid appendable_reason")
    if disc.measured_at is not None and not isinstance(disc.measured_at, str):
        raise ValueError("invalid measured_at")


def _catalog_disc_from_data(data: object) -> CatalogDisc:
    """Input: one JSON disc object. Output: CatalogDisc."""
    # scbc boundary: Reject malformed disc or record fields before using them.
    if not isinstance(data, dict):
        raise ValueError("invalid disc record")
    filtered = _known_fields(CatalogDisc, data)
    if "session_ranges" in filtered:
        filtered["session_ranges"] = parse_session_ranges(filtered["session_ranges"])
    disc = CatalogDisc(**filtered)
    _validate_catalog_disc(disc)
    return disc


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
        _log_unknown_keys("archive catalog", raw, _CATALOG_RECORD_KEYS)
        discs = {}
        for key, data in raw["discs"].items():
            index = int(key)
            disc = _catalog_disc_from_data(data)
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


def _prefer_known(new_value, old_value):
    """Input: new and old field values. Output: new when set, else old."""
    return new_value if new_value is not None else old_value


def _newer_measurement(left: CatalogDisc, right: CatalogDisc) -> CatalogDisc:
    """Input: two disc records. Output: the record with the later measured_at."""
    left_t = left.measured_at
    right_t = right.measured_at
    if left_t and right_t:
        return left if left_t >= right_t else right
    if left_t:
        return left
    if right_t:
        return right
    return right


def _merged_space_fields(
    old: CatalogDisc, new: CatalogDisc,
) -> tuple[int | None, int | None, bool | None, str | None, int | None, str | None]:
    """Input: two disc records. Output: space fields; newest measurement wins."""
    newer = _newer_measurement(old, new)
    older = old if newer is new else new
    appendable = _prefer_known(newer.appendable, older.appendable)
    if old.appendable is False or new.appendable is False:
        appendable = False
    reason = None
    if appendable is False:
        reason = _prefer_known(newer.appendable_reason, older.appendable_reason)
    return (
        _prefer_known(newer.used_bytes, older.used_bytes),
        _prefer_known(newer.remaining_bytes, older.remaining_bytes),
        appendable,
        _prefer_known(newer.measured_at, older.measured_at),
        _prefer_known(newer.file_count, older.file_count),
        reason,
    )


def _merge_disc(old: CatalogDisc, new: CatalogDisc) -> CatalogDisc:
    """
    Input:  old, new - records for the same disc
    Output: Record with all known fields
    Details:
        Prefer the new label. Union session membership so a fill range
        in local memory is not replaced by an older one-range baseline.
        Free-space fields use the newest measured_at value.
    """
    sessions = _merge_disc_sessions(old, new)
    first = min(sessions) if sessions else _prefer_known(
        new.first_session, old.first_session,
    )
    last = max(sessions) if sessions else _prefer_known(
        new.last_session, old.last_session,
    )
    used, remaining, appendable, measured_at, file_count, reason = _merged_space_fields(
        old, new,
    )
    return CatalogDisc(
        new.volume_index,
        new.label or old.label,
        first,
        last,
        new.encrypted if new.encrypted is not None else old.encrypted,
        session_ranges=_stored_session_ranges(sessions) if sessions else (
            new.session_ranges or old.session_ranges
        ),
        used_bytes=used,
        remaining_bytes=remaining,
        appendable=appendable,
        measured_at=measured_at,
        file_count=file_count,
        appendable_reason=reason,
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


def remembered_archive_label(archive_id: str) -> str | None:
    """
    Input:  archive_id - ID from volume.json
    Output: Label of the locally recorded highest disc, or None
    Details:
        The ledger migration reads this to bind a legacy label-keyed
        ledger row to the one archive that used that label.
    """
    discs, _highest, _mode = _read_memory(archive_id)
    for index in sorted(discs, reverse=True):
        if discs[index].label:
            return discs[index].label
    return None


def archive_ids_with_label(label: str) -> list[str]:
    """
    Input:  label - disc label to search for
    Output: Archive ids whose local disc records use that label
    Details:
        The init ledger check reads this. A blank disc shares only a
        label with past archives; a shared label alone must not block
        init of a new archive.
    """
    from oddarchiver.ledger import ledger_dir
    ids: list[str] = []
    try:
        files = sorted(ledger_dir().glob("archive-*.json"))
    except OSError:
        _log.warning("Cannot list archive records under the state directory")
        return ids
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            _log.warning("Cannot read local archive record %s: %s", path, exc)
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("discs"), dict):
            continue
        for record in raw["discs"].values():
            if isinstance(record, dict) and record.get("label") == label:
                ids.append(path.stem.removeprefix("archive-"))
                break
    return ids


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


def _loaded_disc_sessions(
    marker: VolumeMarker | None, manifests: list[Manifest],
) -> set[int]:
    """Input: inserted marker and loaded manifests. Output: session numbers on the disc."""
    sessions: set[int] = set()
    for manifest in manifests:
        # scbc ignore[type-equality] Reject bool as a session number
        if not manifest.suspect and type(manifest.session) is int:
            sessions.add(manifest.session)
    if marker is not None and marker.session_ranges:
        for first, last in marker.session_ranges:
            sessions.update(range(first, last + 1))
    return sessions


def _membership_record(
    marker: VolumeMarker, sessions: set[int], old: CatalogDisc | None,
) -> CatalogDisc:
    """Input: marker, session set, prior record. Output: updated disc record."""
    return CatalogDisc(
        marker.volume_index,
        old.label if old is not None else "",
        marker.first_session,
        max(sessions),
        encrypted=old.encrypted if old is not None else None,
        session_ranges=_stored_session_ranges(sessions),
        used_bytes=old.used_bytes if old is not None else None,
        remaining_bytes=old.remaining_bytes if old is not None else None,
        appendable=old.appendable if old is not None else None,
        measured_at=old.measured_at if old is not None else None,
        file_count=old.file_count if old is not None else None,
    )


def _apply_loaded_disc_membership(
    discs: dict[int, CatalogDisc],
    marker: VolumeMarker | None,
    manifests: list[Manifest],
) -> None:
    """
    Input: disc map, inserted marker, loaded manifests
    Output: source disc ranges updated in place
    """
    if marker is None:
        return
    sessions = _loaded_disc_sessions(marker, manifests)
    if not sessions:
        return
    old = discs.get(marker.volume_index)
    record = _membership_record(marker, sessions, old)
    if old is not None:
        discs[marker.volume_index] = _merge_disc(old, record)
    else:
        discs[marker.volume_index] = record



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
    loaded = [
        m.session for m in manifests
        # scbc ignore[type-equality] Reject bool as a session number
        if type(m.session) is int and m.session >= 0
    ]

    if loaded:
        return max(loaded)
    return max(
        (version.session for versions in history.values() for version in versions),
        default=-1,
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
    _apply_loaded_disc_membership(discs, marker, manifests)
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


def _contiguous_run_sessions(first_session: int, seen: set[int]) -> set[int]:
    """Input: first session and loaded set. Output: prefix with no hole."""
    run: set[int] = set()
    number = first_session
    while number in seen:
        run.add(number)
        number += 1
    return run


def _unique_disc_manifests(
    marker: VolumeMarker, manifests: list[Manifest],
) -> tuple[list[Manifest], set[int]]:
    """Input: marker and manifests. Output: unique non-SUSPECT manifests and sessions."""
    usable: list[Manifest] = []
    seen: set[int] = set()
    for manifest in manifests:
        if manifest.suspect:
            continue
        # scbc ignore[type-equality] Reject bool as a session bound
        if type(manifest.session) is not int or manifest.session in seen:
            _log.warning("Ignoring unusable manifest on disc %s: %r",
                         marker.volume_index, manifest.session)
            continue
        seen.add(manifest.session)
        usable.append(manifest)
    return usable, seen


def _membership_and_elsewhere(
    marker: VolumeMarker, catalog: ArchiveCatalog | None,
) -> tuple[set[int], set[int]]:
    """Input: disc identity and catalog. Output: this-disc sessions and holes."""
    membership: set[int] = set()
    elsewhere: set[int] = set()
    if marker.session_ranges:
        for first, last in marker.session_ranges:
            membership.update(range(first, last + 1))
        elsewhere |= _range_holes([
            (pair[0], pair[1]) for pair in marker.session_ranges
        ])
    if catalog is not None:
        elsewhere |= elsewhere_sessions_for_disc(catalog, marker.volume_index)
        disc = catalog.discs.get(marker.volume_index)
        if disc is not None:
            membership |= _disc_sessions(disc)
    return membership, elsewhere


def _gap_explained(
    session: int, first_session: int, seen: set[int], elsewhere: set[int],
) -> bool:
    """Input: candidate session and known membership. Output: whether holes are elsewhere."""
    if session <= first_session:
        return False
    for number in range(first_session, session):
        if number not in seen and number not in elsewhere:
            return False
    return True


def _keep_disc_session(
    session: int, run: set[int], membership: set[int],
    first_session: int, seen: set[int], elsewhere: set[int],
) -> bool:
    """Input: session and membership sets. Output: whether this disc may keep it."""
    if session in run or session in membership:
        return True
    return _gap_explained(session, first_session, seen, elsewhere)



def _usable_disc_manifests(
    marker: VolumeMarker, manifests: list[Manifest],
    catalog: ArchiveCatalog | None = None,
) -> tuple[list[Manifest], set[int]]:
    """
    Input:  marker, manifests - disc identity and loaded manifests
            catalog - known ranges used to explain fill gaps
    Output: Usable manifests and their distinct sessions
    Details:
        Keep unique non-SUSPECT sessions in the contiguous run that
        starts at first_session. Also keep a later session when
        session_ranges or other discs explain every hole.
        A forged session past an unexplained hole stays out of run.
    """
    usable, seen = _unique_disc_manifests(marker, manifests)
    run = _contiguous_run_sessions(marker.first_session, seen)
    membership, elsewhere = _membership_and_elsewhere(marker, catalog)
    kept = []
    kept_seen: set[int] = set()
    for manifest in usable:
        if not _keep_disc_session(
            manifest.session, run, membership,
            marker.first_session, seen, elsewhere,
        ):
            _log.warning("Ignoring manifest outside disc %s session run: %r",
                         marker.volume_index, manifest.session)
            continue
        kept.append(manifest)
        kept_seen.add(manifest.session)
    return kept, kept_seen





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
    catalog: ArchiveCatalog, seen: set[int],
    changed: set[str],
) -> None:
    """
    Input:  catalog - history with unknown origins
            seen - sessions that this disc holds
            changed - path set to update
    Output: None; unknown origins and changed paths updated in place
    """
    for session_n in sorted(seen):
        if session_n not in catalog._unknown or not catalog._unknown[session_n]:
            continue
        index = volume_for_session(catalog, session_n)
        if index is None:
            continue
        for path in catalog._unknown[session_n]:
            for version in catalog.history[path]:
                if version.session == session_n and version.volume_index is None:
                    version.volume_index = index
                    changed.add(path)
        catalog._unknown[session_n].clear()



def _learned_disc_sessions(
    marker: VolumeMarker, seen: set[int], old: CatalogDisc | None,
) -> set[int]:
    """Input: marker, loaded sessions, prior record. Output: union membership."""
    sessions = set(seen)
    if old is not None:
        sessions |= _disc_sessions(old)
    if marker.session_ranges:
        for first, last in marker.session_ranges:
            sessions.update(range(first, last + 1))
    return sessions


def _learned_disc_record(
    marker: VolumeMarker, label: str, sessions: set[int], old: CatalogDisc | None,
) -> CatalogDisc:
    """Input: disc identity and learned sessions. Output: updated disc record."""
    return CatalogDisc(
        marker.volume_index, label, marker.first_session, max(sessions),
        encrypted=old.encrypted if old is not None else None,
        session_ranges=_stored_session_ranges(sessions),
        used_bytes=old.used_bytes if old is not None else None,
        remaining_bytes=old.remaining_bytes if old is not None else None,
        appendable=old.appendable if old is not None else None,
        measured_at=old.measured_at if old is not None else None,
        file_count=old.file_count if old is not None else None,
    )


def learn_disc(
    catalog: ArchiveCatalog, marker: VolumeMarker, label: str,
    manifests: list[Manifest],
) -> set[str]:
    """
    Input: catalog - history to update
           marker, label, manifests - identity and events of a collected disc
    Output: Paths whose history changed
    Details:
        The disc holds the listed session numbers, which may be a set.
        Only a non-SUSPECT manifest gives verified events and the disc
        ranges. The code ignores a second manifest for the same session.
        A disc with no usable manifest changes nothing. An event that
        differs from a verified event at the same session is a conflict,
        and the code ignores it.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    if not catalog._indexed:
        _index_catalog(catalog)
    usable, seen = _usable_disc_manifests(marker, manifests, catalog)
    if not usable:
        _log.warning("Disc %s has no usable session manifest", marker.volume_index)
        return set()
    old = catalog.discs.get(marker.volume_index)
    sessions = _learned_disc_sessions(marker, seen, old)
    record = _learned_disc_record(marker, label, sessions, old)
    if old is not None:
        record = _merge_disc(old, record)
        record.label = label or old.label
    _set_disc_range(catalog, marker.volume_index, record)
    events = _disc_manifest_events(usable, marker)
    changed = _apply_disc_events(catalog, events)
    _resolve_learned_unknowns(catalog, sessions, changed)
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


def _contiguous_inserted_sessions(
    marker: VolumeMarker, manifests: list[Manifest],
) -> set[int]:
    """Input: marker and manifests. Output: contiguous sessions aligned with first_session."""
    seen: set[int] = set()
    for slot, manifest in enumerate(manifests, marker.first_session):
        # scbc ignore[type-equality] Reject bool as a manifest session
        if not manifest.suspect and type(manifest.session) is int and manifest.session == slot:
            seen.add(manifest.session)
    return seen


def _range_inserted_sessions(
    marker: VolumeMarker, manifests: list[Manifest],
) -> set[int]:
    """Input: marker and manifests. Output: sessions named by volume.json ranges."""
    membership: set[int] = set()
    for first, last in marker.session_ranges:
        membership.update(range(first, last + 1))
    seen: set[int] = set()
    for manifest in manifests:
        # scbc ignore[type-equality] Reject bool as a manifest session
        if (not manifest.suspect and type(manifest.session) is int
                and manifest.session in membership):
            seen.add(manifest.session)
    return seen


def _authenticated_inserted_sessions(
    marker: VolumeMarker, manifests: list[Manifest],
) -> set[int]:
    """
    Input: disc marker and loaded manifests
    Output: sessions that may be stored as this disc's range
    Details: Keep the contiguous run aligned with first_session.
        Also keep a later session when volume.json session_ranges names it
        and a non-SUSPECT manifest matches that session.
    """
    seen = _contiguous_inserted_sessions(marker, manifests)
    if not marker.session_ranges:
        return seen
    seen |= _range_inserted_sessions(marker, manifests)
    return seen


def remember_inserted_disc(
    marker: VolumeMarker | None, label: str, manifests: list[Manifest],
) -> None:
    """
    Input: marker, label, manifests - control data already read from this disc
    Output: None
    Details: Save the disc ranges without reading or replaying its baseline.
        Do not write a record from SUSPECT or empty manifests.
        Persist the marker-aligned contiguous run from first_session.
        Persist a gapped set only when volume.json session_ranges names it.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    if marker is None:
        return
    seen = _authenticated_inserted_sessions(marker, manifests)
    if not seen:
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
    last = max(seen)
    record = CatalogDisc(
        marker.volume_index, label, marker.first_session, last,
        session_ranges=_stored_session_ranges(seen),
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


def remembered_discs(archive_id: str) -> dict[int, CatalogDisc]:
    """
    Input:  archive_id - archive ID from volume.json
    Output: Local disc records keyed by volume index
    """
    discs, _highest, _mode = _read_memory(archive_id)
    return discs



def other_volume_sessions(archive_id: str, volume_index: int) -> set[int]:
    """
    Input:  archive_id, volume_index - inserted disc identity
    Output: Sessions local memory places on other volumes
    """
    discs, _highest, _mode = _read_memory(archive_id)
    elsewhere: set[int] = set()
    for index, disc in discs.items():
        if index == volume_index:
            continue
        for first, last in _disc_ranges(disc):
            elsewhere.update(range(first, last + 1))
    return elsewhere


def next_archive_session(
    archive_id: str | None,
    manifests: list[Manifest],
    first_session: int = 0,
) -> int:
    """
    Input:  archive_id - local record identity, if any
            manifests - sessions loaded from the inserted disc
            first_session - first session on the inserted disc when empty
    Output: One more than the highest known session
    """
    known: set[int] = set()
    for manifest in manifests:
        # scbc ignore[type-equality] Reject bool as a session number
        if not manifest.suspect and type(manifest.session) is int:
            known.add(manifest.session)
    if archive_id:
        discs, _highest, _mode = _read_memory(archive_id)
        for disc in discs.values():
            for first, last in _disc_ranges(disc):
                known.update(range(first, last + 1))
    if not known:
        return first_session
    return max(known) + 1


def remembered_volume_sessions(archive_id: str) -> dict[int, set[int]]:
    """
    Input:  archive_id - local record identity
    Output: Sessions per volume index from the local archive record
    """
    discs, _highest, _mode = _read_memory(archive_id)
    return {index: _disc_sessions(disc) for index, disc in discs.items()}


def disc_holding_highest_session(archive_id: str) -> CatalogDisc | None:
    """
    Input:  archive_id - local record identity
    Output: Disc record that holds the highest known session, or None
    """
    discs, _highest, _mode = _read_memory(archive_id)
    holder: CatalogDisc | None = None
    highest = -1
    for disc in discs.values():
        sessions = _disc_sessions(disc)
        if not sessions:
            continue
        last = max(sessions)
        if last > highest:
            highest = last
            holder = disc
    return holder



def _measure_identity(
    old: CatalogDisc | None,
    label: str,
    first_session: int | None,
    last_session: int | None,
    session_ranges: list[list[int]] | None,
    file_count: int | None,
) -> tuple[str, int | None, int | None, list[list[int]] | None, int | None]:
    """Input: old record and requested identity fields. Output: merged fields."""
    if old is None:
        return label, first_session, last_session, session_ranges, file_count
    return (
        label or old.label,
        first_session if first_session is not None else old.first_session,
        last_session if last_session is not None else old.last_session,
        session_ranges or old.session_ranges,
        file_count if file_count is not None else old.file_count,
    )


def _measure_record(
    volume_index: int,
    label: str,
    old: CatalogDisc | None,
    *,
    used_bytes: int,
    remaining_bytes: int,
    appendable: bool,
    first_session: int | None = None,
    last_session: int | None = None,
    session_ranges: list[list[int]] | None = None,
    file_count: int | None = None,
    measured_at: str,
    appendable_reason: str | None = None,
) -> CatalogDisc:
    """Input: volume identity and a capacity snapshot. Output: disc record."""
    label_, first_, last_, ranges_, count_ = _measure_identity(
        old, label, first_session, last_session, session_ranges, file_count,
    )
    return CatalogDisc(
        volume_index,
        label_,
        first_,
        last_,
        encrypted=old.encrypted if old is not None else None,
        session_ranges=ranges_,
        used_bytes=used_bytes,
        remaining_bytes=remaining_bytes,
        appendable=appendable,
        measured_at=measured_at,
        file_count=count_,
        appendable_reason=appendable_reason,
    )


def remember_volume_measure(
    archive_id: str,
    volume_index: int,
    label: str,
    *,
    used_bytes: int,
    remaining_bytes: int,
    appendable: bool,
    first_session: int | None = None,
    last_session: int | None = None,
    session_ranges: list[list[int]] | None = None,
    file_count: int | None = None,
) -> None:
    """
    Input:  archive and volume identity plus a capacity snapshot
    Output: None
    Details:
        Merge used_bytes and remaining_bytes into the local archive record
        after a successful measure or burn.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    saved, highest, enc_mode = _read_memory(archive_id)
    old = saved.get(volume_index)
    measured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed_reason = None
    if old is not None and old.appendable is False:
        appendable = False
        closed_reason = old.appendable_reason
    record = _measure_record(
        volume_index, label, old,
        used_bytes=used_bytes, remaining_bytes=remaining_bytes,
        appendable=appendable, first_session=first_session,
        last_session=last_session, session_ranges=session_ranges,
        file_count=file_count, measured_at=measured_at,
        appendable_reason=closed_reason,
    )
    if old is not None:
        record = _merge_disc(old, record)
        record.used_bytes = used_bytes
        record.remaining_bytes = remaining_bytes
        if old.appendable is False:
            record.appendable = False
            record.appendable_reason = old.appendable_reason
        else:
            record.appendable = appendable
        record.measured_at = measured_at
    saved[volume_index] = record

    new_highest = volume_index if highest is None else max(highest, volume_index)
    path = _memory_path(archive_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_obj: dict = {
        "highest_volume_index": new_highest,
        "discs": {str(index): dataclasses.asdict(item)
                  for index, item in sorted(saved.items())},
    }
    if enc_mode:
        payload_obj["encryption_mode"] = enc_mode
    _write_disc_memory(path, json.dumps(payload_obj, indent=2))


def remember_volume_not_appendable(
    archive_id: str,
    volume_index: int,
    reason: str,
) -> None:
    """
    Input:  archive and volume identity plus a close reason
    Output: None
    Details:
        Record the volume as not appendable in local memory. Later fills
        skip it and skip its stubs.
    """
    saved, highest, enc_mode = _read_memory(archive_id)
    old = saved.get(volume_index)
    if old is None:
        record = CatalogDisc(
            volume_index, "", None, None,
            appendable=False, appendable_reason=reason,
        )
    else:
        record = dataclasses.replace(
            old, appendable=False, appendable_reason=reason,
        )
    saved[volume_index] = record
    new_highest = volume_index if highest is None else max(highest, volume_index)
    path = _memory_path(archive_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_obj: dict = {
        "highest_volume_index": new_highest,
        "discs": {str(index): dataclasses.asdict(item)
                  for index, item in sorted(saved.items())},
    }
    if enc_mode:
        payload_obj["encryption_mode"] = enc_mode
    _write_disc_memory(path, json.dumps(payload_obj, indent=2))


def catalog_disc_from_record(record: VolumeRecord) -> CatalogDisc:
    """Input: on-disc volume record. Output: CatalogDisc with space fields."""
    return CatalogDisc(
        record.volume_index,
        record.label,
        record.first_session,
        record.last_session,
        encrypted=record.encrypted,
        session_ranges=record.session_ranges,
        used_bytes=record.used_bytes,
        remaining_bytes=record.remaining_bytes,
        appendable=record.appendable,
        measured_at=record.measured_at,
        file_count=record.file_count,
    )


def volume_record_from_disc(disc: CatalogDisc) -> VolumeRecord:
    """Input: CatalogDisc. Output: VolumeRecord for a catalog snapshot."""
    return VolumeRecord(
        disc.volume_index,
        disc.label,
        disc.first_session,
        disc.last_session,
        encrypted=disc.encrypted,
        session_ranges=disc.session_ranges,
        used_bytes=disc.used_bytes,
        remaining_bytes=disc.remaining_bytes,
        appendable=disc.appendable,
        measured_at=disc.measured_at,
        file_count=disc.file_count,
    )


def discs_from_baseline(baseline: VolumeBaseline | None) -> dict[int, CatalogDisc]:
    """Input: volume baseline or None. Output: volume_index to CatalogDisc."""
    discs: dict[int, CatalogDisc] = {}
    if baseline is None:
        return discs
    for record in baseline.volumes:
        if record.volume_index is None:
            continue
        discs[record.volume_index] = catalog_disc_from_record(record)
    return discs


def merge_snapshot_space(
    archive_id: str, baseline: VolumeBaseline | None,
) -> dict[int, CatalogDisc]:
    """
    Input: archive id and on-disc catalog snapshot
    Output: discs with local memory merged; newest measured_at wins
    """
    memory = remembered_discs(archive_id)
    on_disc = discs_from_baseline(baseline)
    merged: dict[int, CatalogDisc] = {}
    for index in set(memory) | set(on_disc):
        if index in memory and index in on_disc:
            merged[index] = _merge_disc(memory[index], on_disc[index])
        elif index in memory:
            merged[index] = memory[index]
        else:
            merged[index] = on_disc[index]
    return merged




