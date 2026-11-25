"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Volume markers identify discs. A baseline records earlier disc versions
    and the live checksum and origin maps needed by sync.
"""
# Imports
from __future__ import annotations

import dataclasses
import json
import hashlib
import logging
import os
import re
import sys
import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.manifest import Manifest, build_disc_origin, build_disc_state
if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals
logger = logging.getLogger(__name__)
_log = logger

_BLOB_SESSION_RE = re.compile(r"^session_(\d{3})/")
FILL_STUB_BLOCKS = 8144
FILL_STUB_BYTES = FILL_STUB_BLOCKS * 2048
FILL_OUTCOME_STATUSES = frozenset({"landed", "failed", "not_confirmed"})



def _known_fields(cls, data: dict) -> dict:
    names = {item.name for item in dataclasses.fields(cls)}
    extra = [key for key in data if key not in names]
    if extra:
        logger.debug("Ignoring unknown %s keys: %s", cls.__name__, extra)
    return {key: value for key, value in data.items() if key in names}



class BaselineReadError(ValueError):
    """Report an unreadable baseline without treating it as absent."""


def parse_blob_session(blob_path: str) -> int:
    """
    Input:  blob_path - blob path from a manifest
    Output: session number from the blob path
    Details: Rejects paths without a three-digit session prefix.
    """
    match = _BLOB_SESSION_RE.match(blob_path) if isinstance(blob_path, str) else None
    if match is None:
        raise ValueError(f"Invalid manifest blob path {blob_path!r}.")
    return int(match.group(1))


def compact_session_ranges(sessions: set[int]) -> list[list[int]]:
    """
    Input:  sessions - session numbers on one disc
    Output: inclusive [first, last] pairs in order
    """
    if not sessions:
        return []
    ordered = sorted(sessions)
    ranges: list[list[int]] = []
    start = prev = ordered[0]
    for number in ordered[1:]:
        if number == prev + 1:
            prev = number
            continue
        ranges.append([start, prev])
        start = prev = number
    ranges.append([start, prev])
    return ranges


def session_ranges_of(
    first_session: int | None,
    last_session: int | None,
    session_ranges: list[list[int]] | None = None,
) -> list[tuple[int, int]]:
    """
    Input:  first_session, last_session - bounds from an old record
            session_ranges - explicit inclusive pairs, if present
    Output: inclusive (first, last) pairs
    Details:
        A missing session_ranges field becomes one pair when both bounds
        exist.
    """
    if session_ranges:
        return [(pair[0], pair[1]) for pair in session_ranges]
    if first_session is not None and last_session is not None:
        return [(first_session, last_session)]
    return []


def _parse_session_range_pair(item: object) -> list[int]:
    """Input: one JSON session_ranges entry. Output: [first, last]."""
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        raise ValueError("session_ranges entries must be [first, last] pairs")
    first, last = item
    # scbc ignore[type-equality] Reject bool as a session bound
    if type(first) is not int or type(last) is not int or first < 0 or last < first:
        raise ValueError("session_ranges bounds must be ordered non-negative integers")
    return [first, last]


def parse_session_ranges(raw: object) -> list[list[int]] | None:
    """
    Input:  raw - JSON session_ranges value
    Output: inclusive pairs, or None when the field is absent
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError("session_ranges must be a non-empty list of pairs")
    return [_parse_session_range_pair(item) for item in raw]



def _nonnegative(value: object, field: str, nullable: bool = False) -> int | None:
    """Check an integer field before using it as an index or session."""
    # scbc boundary: Reject malformed disc or record fields before using them.
    if nullable and value is None:
        return None
    # scbc ignore[type-equality] Reject bool as a session or index
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _text(value: object, field: str) -> str:
    """Check a string field without coercing a malformed value."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _optional_bool(value: object, field: str) -> bool | None:
    """Input: JSON value and field name. Output: bool or None."""
    if value is None:
        return None
    # scbc ignore[type-equality] Require a JSON boolean
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _optional_text(value: object, field: str) -> str | None:
    """Input: JSON value and field name. Output: string or None."""
    if value is None:
        return None
    return _text(value, field)


def _optional_dict(value: object) -> dict | None:
    """Input: JSON value. Output: object or None."""
    if isinstance(value, dict):
        return value
    return None



def _record(raw: object) -> VolumeRecord:
    """Decode and check an earlier disc record."""
    # scbc boundary: Reject malformed disc or record fields before using them.
    if not isinstance(raw, dict):
        raise ValueError("volume record must be an object")
    raw = _known_fields(VolumeRecord, raw)
    encrypted_raw = raw.get("encrypted")
    # scbc ignore[type-equality] Require a JSON boolean
    if encrypted_raw is not None and type(encrypted_raw) is not bool:
        raise ValueError("encrypted must be a boolean")
    ranges = parse_session_ranges(raw["session_ranges"]) if "session_ranges" in raw else None
    record = VolumeRecord(
        _nonnegative(raw["volume_index"], "volume_index", True),
        _text(raw["label"], "label"),
        _nonnegative(raw["first_session"], "first_session", True),
        _nonnegative(raw["last_session"], "last_session", True),
        encrypted_raw,
        ranges,
        _nonnegative(raw.get("used_bytes"), "used_bytes", True),
        _nonnegative(raw.get("remaining_bytes"), "remaining_bytes", True),
        _optional_bool(raw.get("appendable"), "appendable"),
        _optional_text(raw.get("measured_at"), "measured_at"),
        _nonnegative(raw.get("file_count"), "file_count", True),
    )
    if (record.first_session is not None and record.last_session is not None
            and record.last_session < record.first_session):
        raise ValueError("last_session must not precede first_session")
    return record



def _version(raw: object) -> PathVersion:
    """Decode and check a path history event."""
    # scbc boundary: Reject malformed disc or record fields before using them.
    if not isinstance(raw, dict):
        raise ValueError("path version must be an object")
    kind = _text(raw["kind"], "kind")
    if kind not in ("full", "delta", "deleted"):
        raise ValueError(f"unknown path version kind {kind!r}")
    version = PathVersion(
        _nonnegative(raw["session"], "session"),
        _nonnegative(raw["volume_index"], "volume_index", True),
        kind,
        _text(raw["result_checksum"], "result_checksum"),
        _nonnegative(raw["full_size_bytes"], "full_size_bytes"),
        _text(raw["timestamp"], "timestamp"),
        _nonnegative(raw["blob_session"], "blob_session", True),
        _nonnegative(raw.get("volume_chunk"), "volume_chunk", True),
        _nonnegative(raw.get("volume_chunk_count"), "volume_chunk_count", True),
        raw.get("verified", True),
    )
    # scbc ignore[type-equality] Require a JSON boolean
    if type(version.verified) is not bool:
        raise ValueError("verified must be a boolean")
    if version.volume_chunk_count is not None and (
        version.volume_chunk is None or version.volume_chunk >= version.volume_chunk_count
        or version.volume_chunk_count == 0
    ):
        raise ValueError("volume_chunk must be less than volume_chunk_count")
    return version

@dataclass
class VolumeMarker:
    """Identity record for one physical volume of a logical archive."""

    archive_id: str
    volume_index: int
    first_session: int
    created_at: str
    previous_volume_label: str
    # New markers are format 2. read_volume_marker maps an absent
    # field in an old volume.json to 1 for backward compatibility.
    format: int = 2
    session_ranges: list[list[int]] | None = None
    catalog_snapshot: dict | None = field(default=None, compare=False)
    catalog_snapshot_enc: str | None = field(default=None, compare=False)
    fill_intent: dict | None = field(default=None, compare=False)
    fill_intent_enc: str | None = field(default=None, compare=False)
    fill_outcome: dict | None = field(default=None, compare=False)
    fill_outcome_enc: str | None = field(default=None, compare=False)


@dataclass
class VolumeRecord:
    """Identify one earlier disc and its last measured free space."""

    volume_index: int | None
    label: str
    first_session: int | None
    last_session: int | None
    # Missing on old baselines. True or False after format 2 disc close.
    encrypted: bool | None = field(default=None, compare=False)
    session_ranges: list[list[int]] | None = None
    used_bytes: int | None = field(default=None, compare=False)
    remaining_bytes: int | None = field(default=None, compare=False)
    appendable: bool | None = field(default=None, compare=False)
    measured_at: str | None = field(default=None, compare=False)
    file_count: int | None = field(default=None, compare=False)

@dataclass
class PathVersion:
    """Record one path event from a manifest."""

    session: int
    volume_index: int | None
    kind: str
    result_checksum: str
    full_size_bytes: int
    timestamp: str
    blob_session: int | None
    volume_chunk: int | None = None
    volume_chunk_count: int | None = None
    verified: bool = True


@dataclass
class ArchiveLayout:
    """Record the archive layout after a fill or layout stub."""

    highest_session: int
    highest_session_volume: int
    session_ranges: dict[str, list[list[int]]]
    not_stubbed: list[int]


@dataclass
class VolumeBaseline:
    """Store earlier discs and the current live path state."""

    version: int
    volumes: list[VolumeRecord]
    history: dict[str, list[PathVersion]]
    state: dict[str, str]
    origin: dict[str, int]
    layout: ArchiveLayout | None = None

@dataclass
class FillFile:
    """One planned or landed fill path and its checksum."""

    path: str
    checksum: str


@dataclass
class FillIntent:
    """Record a fill attempt on the newest disc before the target burn."""

    target_volume_index: int
    target_label: str
    session: int
    files: list[FillFile]
    time: str
    attempted: bool = True


@dataclass
class FillOutcome:
    """Record whether a fill session landed, failed, or was not confirmed."""

    target_volume_index: int
    session: int
    status: str
    files: list[FillFile]
    time: str
    layout: ArchiveLayout | None = None




# Functions


def write_volume_marker(staging: Path, marker: VolumeMarker) -> None:
    """
    Input:  staging - staging root (sibling of the session_NNN/ directory)
            marker  - VolumeMarker to persist as volume.json
    Output: None
    Details:
        Serializes the marker to JSON and writes it atomically at
        staging/volume.json via a .tmp file then os.replace, matching
        write_manifest().
    """
    dest = staging / "volume.json"
    tmp = staging / "volume.json.tmp"
    data = dataclasses.asdict(marker)
    if data.get("session_ranges") is None:
        del data["session_ranges"]
    for key in (
        "catalog_snapshot", "catalog_snapshot_enc",
        "fill_intent", "fill_intent_enc",
        "fill_outcome", "fill_outcome_enc",
    ):
        if data.get(key) is None:
            del data[key]
    payload = json.dumps(data, indent=2).encode("utf-8")
    tmp.write_bytes(payload)
    os.replace(tmp, dest)


def _parse_marker_object(raw: dict) -> VolumeMarker | None:
    """Input: decoded volume.json object. Output: VolumeMarker or None."""
    try:
        raw = _known_fields(VolumeMarker, raw)
        marker = VolumeMarker(
            archive_id=_text(raw["archive_id"], "archive_id"),
            volume_index=_nonnegative(raw["volume_index"], "volume_index"),
            first_session=_nonnegative(raw["first_session"], "first_session"),
            created_at=_text(raw["created_at"], "created_at"),
            previous_volume_label=_text(
                raw["previous_volume_label"], "previous_volume_label",
            ),
            # scbc ignore[dict-get-default-inside-keyerror-try] Missing format means legacy format 1
            format=raw.get("format", 1),
            session_ranges=parse_session_ranges(raw.get("session_ranges")),
            catalog_snapshot=_optional_dict(raw.get("catalog_snapshot")),
            catalog_snapshot_enc=_optional_text(
                raw.get("catalog_snapshot_enc"), "catalog_snapshot_enc",
            ),
            fill_intent=_optional_dict(raw.get("fill_intent")),
            fill_intent_enc=_optional_text(
                raw.get("fill_intent_enc"), "fill_intent_enc",
            ),
            fill_outcome=_optional_dict(raw.get("fill_outcome")),
            fill_outcome_enc=_optional_text(
                raw.get("fill_outcome_enc"), "fill_outcome_enc",
            ),
        )
        # scbc ignore[type-equality] Reject bool as a marker format
        if type(marker.format) is not int or marker.format not in (1, 2):
            raise ValueError("format must be 1 or 2")
        if marker.volume_index >= 10_000:
            raise OverflowError("volume_index must be less than 10000")
        if marker.first_session >= 1000:
            raise OverflowError("first_session must be less than 1000")
        return marker
    except (KeyError, TypeError, ValueError) as exc:
        _log.warning("Ignoring unreadable volume.json: %s", exc)
        return None
    except OverflowError as exc:
        _log.warning("Invalid volume.json: %s", exc)
        raise ValueError(f"Invalid volume.json: {exc}") from exc


def parse_volume_marker_bytes(data: bytes | None) -> VolumeMarker | None:
    """
    Input:  data - volume.json bytes, or None when the file is absent
    Output: VolumeMarker, or None when the bytes are missing or unusable
    Details:
        Shared by read_volume_marker and the control-file batch parser.
        An absent format field maps to 1. Out-of-range indices raise.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    if data is None:
        return None
    if not isinstance(data, (bytes, bytearray)):
        _log.warning("Ignoring unreadable volume.json: not bytes")
        return None
    try:
        raw = json.loads(bytes(data).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("Ignoring unreadable volume.json: %s", exc)
        return None
    if not isinstance(raw, dict):
        _log.warning("Ignoring unreadable volume.json: not an object")
        return None
    return _parse_marker_object(raw)


def read_volume_marker(backend: "BurnBackend") -> VolumeMarker | None:
    """
    Input:  backend - BurnBackend to read volume.json from
    Output: VolumeMarker, or None when the disc has no usable marker
    Details:
        Calls backend.read_paths(["volume.json"]). The backend omits
        missing paths and does not raise. None signals a
        legacy/single-volume archive. Callers apply archive_id=None,
        volume_index=0, first_session=0 themselves. Decode failures
        also return None. The code logs a genuine backend/transport
        failure and re-raises it.
    """
    try:
        found = backend.read_paths(["volume.json"])
    except Exception:
        _log.exception("Failed to read volume.json from backend")
        raise
    return parse_volume_marker_bytes(found.get("volume.json"))


def _seed_baseline_history(
    prior: VolumeBaseline | None,
) -> tuple[list[VolumeRecord], dict[str, list[PathVersion]]]:
    """
    Input:  prior - baseline already on the closing disc, if present
    Output: copied version 2 volumes and history, or version 1 placeholders
    """
    volumes = list(prior.volumes) if prior and prior.version == 2 else []
    history = (
        {path: list(versions) for path, versions in prior.history.items()}
        if prior and prior.version == 2 else {}
    )
    if prior and prior.version == 1:
        for path, checksum in prior.state.items():
            origin = prior.origin.get(path)
            if origin is None:
                continue
            history[path] = [PathVersion(
                session=origin, volume_index=None, kind="full",
                result_checksum=checksum, full_size_bytes=0, timestamp="",
                blob_session=origin, verified=False,
            )]
    return volumes, history


def _append_manifest_history(
    history: dict[str, list[PathVersion]],
    usable: list[Manifest],
    marker: VolumeMarker | None,
) -> None:
    """
    Input:  history - path events to extend
            usable - non-suspect closing-disc manifests
            marker - closing-disc identity, if present
    Output: None
    """
    for manifest in sorted(usable, key=lambda m: m.session):
        for entry in manifest.entries:
            blob_path = entry.delta_file if entry.type == "delta" else entry.file
            blob_session = parse_blob_session(blob_path) if blob_path else None
            history.setdefault(entry.path, []).append(PathVersion(
                session=manifest.session,
                volume_index=marker.volume_index if marker else None,
                kind=entry.type,
                result_checksum=entry.result_checksum,
                full_size_bytes=entry.full_size_bytes,
                timestamp=manifest.timestamp,
                blob_session=blob_session,
                volume_chunk=entry.volume_chunk,
                volume_chunk_count=entry.volume_chunk_count,
            ))
        for path in manifest.deleted:
            history.setdefault(path, []).append(PathVersion(
                session=manifest.session,
                volume_index=marker.volume_index if marker else None,
                kind="deleted", result_checksum="", full_size_bytes=0,
                timestamp=manifest.timestamp, blob_session=None,
            ))


def _closing_volume_record(
    usable: list[Manifest],
    marker: VolumeMarker | None,
    label: str,
    encrypted: bool | None,
) -> VolumeRecord:
    """
    Input:  usable - non-suspect closing-disc manifests
            marker - closing-disc identity, if present
            label - closing-disc label
            encrypted - known archive crypto status
    Output: closing-disc volume record
    """
    return VolumeRecord(
        volume_index=marker.volume_index if marker else None,
        label=label,
        first_session=marker.first_session if marker else (
            min((m.session for m in usable), default=None)
        ),
        last_session=max((m.session for m in usable), default=None),
        encrypted=encrypted,
    )


def build_volume_baseline(
    prior: VolumeBaseline | None,
    manifests: list[Manifest],
    marker: VolumeMarker | None,
    label: str,
    encrypted: bool | None = None,
) -> VolumeBaseline:
    """
    Input:  prior - baseline already on the closing disc, if present
            manifests - manifests on the closing disc
            marker - identity of the closing disc, if present
            label - label of the closing disc
            encrypted - True or False when the archive crypto is known
    Output: version 2 baseline for the next disc
    Details:
        A version 1 baseline has no file sizes or blob locations. Each live
        path gets an unverified full placeholder with size 0, an empty
        timestamp, an unknown volume, and its origin session as its blob
        session. Suspect manifests do not add history events or session bounds.
    """
    volumes, history = _seed_baseline_history(prior)
    for manifest in manifests:
        if manifest.suspect:
            _log.warning("Skipping SUSPECT manifest for session %d", manifest.session)
    usable = [m for m in manifests if not m.suspect]
    volumes.append(_closing_volume_record(usable, marker, label, encrypted))
    _append_manifest_history(history, usable, marker)
    return VolumeBaseline(
        version=2, volumes=volumes, history=history,
        state=build_disc_state(manifests, seed=prior.state if prior else None),
        origin=build_disc_origin(manifests, seed=prior.origin if prior else None),
    )


def _drop_null_volume_fields(vol: dict) -> None:
    """Input: volume record dict. Output: None. Details: Drop unused optional keys."""
    for key in (
        "session_ranges", "used_bytes", "remaining_bytes",
        "appendable", "measured_at", "file_count",
    ):
        if vol.get(key) is None:
            vol.pop(key, None)


def _baseline_json_bytes(
    baseline: VolumeBaseline, crypto: "CryptoBackend | None",
    marker: VolumeMarker | None,
) -> tuple[bytes, bool]:
    """Input: baseline, crypto, marker. Output: payload bytes and encrypted flag."""
    from oddarchiver.crypto import NullCrypto

    encrypted = crypto is not None and not isinstance(crypto, NullCrypto)
    data = dataclasses.asdict(baseline)
    for vol in data["volumes"]:
        if vol.get("encrypted") is None:
            vol["encrypted"] = encrypted
        _drop_null_volume_fields(vol)
    if data.get("layout") is None:
        data.pop("layout", None)
    if marker is not None:
        data["archive_id"] = marker.archive_id
        data["volume_index"] = marker.volume_index
    payload = json.dumps(data, indent=2).encode("utf-8")
    if encrypted:
        from oddarchiver.crypto import baseline_aad
        payload = crypto.encrypt(
            payload,
            aad=baseline_aad(marker.archive_id, marker.volume_index) if marker else None,
        )
    return payload, encrypted


def _write_named_baseline(
    staging: Path, name: str, other: str, payload: bytes,
) -> None:
    """Input: staging root, filenames, payload. Output: None."""
    tmp = staging / f"{name}.tmp"
    tmp.write_bytes(payload)
    os.replace(tmp, staging / name)
    (staging / other).unlink(missing_ok=True)


def write_volume_baseline(
    staging: Path, baseline: VolumeBaseline, crypto: "CryptoBackend | None",
    marker: VolumeMarker | None = None,
) -> None:
    """
    Input:  staging - staging root
            baseline - cumulative version 2 baseline
            crypto - encryption backend or NullCrypto
    Output: None
    Details:
        Encrypts the JSON with the same crypto method as a manifest. Writes
        one format atomically and removes the other format. A missing
        encrypted field on a volume record is filled from this crypto.
    """
    payload, encrypted = _baseline_json_bytes(baseline, crypto, marker)
    name = "volume_baseline.enc" if encrypted else "volume_baseline.json"
    other = "volume_baseline.json" if encrypted else "volume_baseline.enc"
    _write_named_baseline(staging, name, other, payload)


def _marker_with_snapshot(
    marker: VolumeMarker, snapshot: VolumeBaseline, crypto: "CryptoBackend | None",
) -> VolumeMarker:
    """Input: marker and catalog snapshot. Output: marker with snapshot fields set."""
    payload, encrypted = _baseline_json_bytes(snapshot, crypto, marker)
    if encrypted:
        return dataclasses.replace(
            marker,
            catalog_snapshot=None,
            catalog_snapshot_enc=base64.b64encode(payload).decode("ascii"),
        )
    return dataclasses.replace(
        marker,
        catalog_snapshot=json.loads(payload.decode("utf-8")),
        catalog_snapshot_enc=None,
    )


def write_layout_stub(
    staging: Path, baseline: VolumeBaseline, crypto: "CryptoBackend | None",
    marker: VolumeMarker | None = None,
) -> None:
    """
    Input:  staging, baseline, crypto, marker
    Output: None
    Details:
        Stores the catalog snapshot in volume.json. That path is already
        a permitted disc read. It does not replace volume_baseline on a
        later volume, so the first-session checksum still matches.
    """
    if marker is None:
        raise ValueError("A layout stub needs volume.json identity.")
    write_volume_marker(staging, _marker_with_snapshot(marker, baseline, crypto))



def _parse_fill_file(item: object) -> FillFile:
    """Input: one files list entry. Output: FillFile."""
    if not isinstance(item, dict):
        raise ValueError("files entry must be an object")
    return FillFile(
        path=_text(item["path"], "path"),
        checksum=_text(item["checksum"], "checksum"),
    )


def _parse_fill_files(raw: object) -> list[FillFile]:
    """Input: files list from a fill stub. Output: FillFile list."""
    # scbc boundary: Reject malformed disc fill file lists before use.
    if not isinstance(raw, list):
        raise ValueError("files must be a list")
    return [_parse_fill_file(item) for item in raw]


def fill_files_payload(files: list[FillFile]) -> list[dict[str, str]]:
    """Input: FillFile list. Output: JSON objects with path and checksum."""
    return [{"path": item.path, "checksum": item.checksum} for item in files]


def _b64_payload(encoded: str, name: str) -> bytes:
    """Input: base64 blob and field name. Output: decoded bytes."""
    try:
        return base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:
        _log.warning("Cannot decode %s: %s", name, exc)
        raise BaselineReadError(f"Cannot decode {name}.") from exc


def _decode_fill_blob(
    plain: dict | None, encoded: str | None, crypto: "CryptoBackend | None",
    marker: VolumeMarker, name: str,
) -> dict | None:
    """Input: plaintext or encoded fill blob. Output: decoded object or None."""
    if encoded:
        data, _bound = _decrypt_baseline(
            _b64_payload(encoded, name), name, crypto, marker,
        )
        try:
            raw = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _log.warning("Cannot parse %s: %s", name, exc)
            raise BaselineReadError(f"Cannot parse {name}.") from exc
        if not isinstance(raw, dict):
            raise BaselineReadError(f"{name} must be an object.")
        return raw
    if plain is None:
        return None
    if not isinstance(plain, dict):
        raise ValueError(f"{name} must be an object")
    return plain



def parse_fill_intent(raw: dict) -> FillIntent:
    """Input: decoded fill_intent object. Output: FillIntent."""
    # scbc boundary: Reject malformed fill intent fields before use.
    attempted = raw.get("attempted", True)
    if type(attempted) is not bool:  # scbc ignore[type-equality] JSON bool only
        raise ValueError("attempted must be a bool")

    return FillIntent(
        target_volume_index=_nonnegative(
            raw["target_volume_index"], "target_volume_index",
        ),
        target_label=_text(raw["target_label"], "target_label"),
        session=_nonnegative(raw["session"], "session"),
        files=_parse_fill_files(raw["files"]),
        time=_text(raw["time"], "time"),
        attempted=attempted,
    )


def parse_fill_outcome(raw: dict) -> FillOutcome:
    """Input: decoded fill_outcome object. Output: FillOutcome."""
    # scbc boundary: Reject malformed fill outcome fields before use.
    status = _text(raw["status"], "status")
    if status not in FILL_OUTCOME_STATUSES:
        raise ValueError("invalid fill outcome status")
    layout = _parse_layout(raw.get("layout"))
    return FillOutcome(
        target_volume_index=_nonnegative(
            raw["target_volume_index"], "target_volume_index",
        ),
        session=_nonnegative(raw["session"], "session"),
        status=status,
        files=_parse_fill_files(raw["files"]),
        time=_text(raw["time"], "time"),
        layout=layout,
    )


def fill_intent_payload(intent: FillIntent) -> dict:
    """Input: FillIntent. Output: JSON object stored on volume.json."""
    return {
        "kind": "fill_intent",
        "attempted": intent.attempted,
        "target_volume_index": intent.target_volume_index,
        "target_label": intent.target_label,
        "session": intent.session,
        "files": fill_files_payload(intent.files),
        "time": intent.time,
    }


def fill_outcome_payload(outcome: FillOutcome) -> dict:
    """Input: FillOutcome. Output: JSON object stored on volume.json."""
    data: dict = {
        "kind": "fill_outcome",
        "target_volume_index": outcome.target_volume_index,
        "session": outcome.session,
        "status": outcome.status,
        "files": fill_files_payload(outcome.files),
        "time": outcome.time,
    }
    if outcome.layout is not None:
        data["layout"] = dataclasses.asdict(outcome.layout)
    return data


def _marker_with_fill_blob(
    marker: VolumeMarker, crypto: "CryptoBackend | None", payload: dict,
    plain_field: str, enc_field: str,
) -> VolumeMarker:
    """Input: marker and fill payload. Output: marker with that blob set."""
    from oddarchiver.crypto import NullCrypto, baseline_aad

    data = json.dumps(payload, indent=2).encode("utf-8")
    encrypted = crypto is not None and not isinstance(crypto, NullCrypto)
    if not encrypted:
        return dataclasses.replace(
            marker, **{plain_field: payload, enc_field: None},
        )
    blob = crypto.encrypt(
        data, aad=baseline_aad(marker.archive_id, marker.volume_index),
    )
    return dataclasses.replace(
        marker,
        **{
            plain_field: None,
            enc_field: base64.b64encode(blob).decode("ascii"),
        },
    )


def write_fill_stub(
    staging: Path,
    marker: VolumeMarker,
    crypto: "CryptoBackend | None",
    *,
    intent: FillIntent | None = None,
    outcome: FillOutcome | None = None,
    snapshot: VolumeBaseline | None = None,
) -> None:
    """
    Input: staging, marker, crypto, optional intent, outcome, snapshot
    Output: None
    Details: Writes volume.json. Intent and outcome live next to identity.
    """
    updated = marker
    if snapshot is not None:
        updated = _marker_with_snapshot(updated, snapshot, crypto)
    if intent is not None:
        updated = _marker_with_fill_blob(
            updated, crypto, fill_intent_payload(intent),
            "fill_intent", "fill_intent_enc",
        )
    if outcome is not None:
        updated = _marker_with_fill_blob(
            updated, crypto, fill_outcome_payload(outcome),
            "fill_outcome", "fill_outcome_enc",
        )
    write_volume_marker(staging, updated)


def _read_fill_record(
    backend: "BurnBackend", crypto: "CryptoBackend | None",
    marker: VolumeMarker | None, plain_field: str, enc_field: str, name: str,
    parser,
) -> FillIntent | FillOutcome | None:
    """Input: disc context and fill blob names. Output: parsed fill record or None."""
    # scbc boundary: Reject malformed fill records from disc before use.
    if marker is None:
        marker = read_volume_marker(backend)
    if marker is None:
        return None
    try:
        raw = _decode_fill_blob(
            getattr(marker, plain_field, None),
            getattr(marker, enc_field, None), crypto, marker, name,
        )
        if raw is None:
            return None
        return parser(raw)
    except (ValueError, TypeError, KeyError, BaselineReadError) as exc:
        _log.warning("Cannot read %s: %s", name, exc)
        return None


def read_fill_intent(
    backend: "BurnBackend", crypto: "CryptoBackend | None",
    marker: VolumeMarker | None = None,
) -> FillIntent | None:
    """Input: backend, crypto, marker. Output: unmatched-ready FillIntent or None."""
    # scbc boundary: Reject malformed fill intent from disc before use.
    return _read_fill_record(
        backend, crypto, marker, "fill_intent", "fill_intent_enc",
        "fill_intent", parse_fill_intent,
    )


def read_fill_outcome(
    backend: "BurnBackend", crypto: "CryptoBackend | None",
    marker: VolumeMarker | None = None,
) -> FillOutcome | None:
    """Input: backend, crypto, marker. Output: FillOutcome or None."""
    # scbc boundary: Reject malformed fill outcome from disc before use.
    return _read_fill_record(
        backend, crypto, marker, "fill_outcome", "fill_outcome_enc",
        "fill_outcome", parse_fill_outcome,
    )


def unmatched_fill_intent(
    backend: "BurnBackend", crypto: "CryptoBackend | None",
    marker: VolumeMarker | None = None,
) -> FillIntent | None:
    """Input: disc context. Output: intent with no matching outcome, or None."""
    intent = read_fill_intent(backend, crypto, marker)
    if intent is None:
        return None
    outcome = read_fill_outcome(backend, crypto, marker)
    if outcome is not None and outcome.session == intent.session:
        return None
    return intent




def _check_plaintext_baseline(
    backend: BurnBackend, crypto: CryptoBackend | None, found: dict[str, bytes],
    name: str, marker: VolumeMarker | None, manifests: list[Manifest] | None,
) -> tuple[VolumeMarker | None, list[Manifest] | None]:
    """
    Input:  backend - disc backend; crypto - disc encryption backend
            found - baseline files; name - selected baseline file
            marker - disc identity; manifests - decrypted manifests
    Output: marker and manifests loaded while checking plaintext metadata
    """
    if marker is None:
        marker = read_volume_marker(backend)
    if manifests is None:
        from oddarchiver.manifest import load_session_manifests
        manifests = load_session_manifests(
            backend, crypto, start_session=marker.first_session if marker else 0,
            marker=marker,
        )
    first = marker.first_session if marker is not None else None
    first_manifest = next(
        (m for m in manifests if m.session == first), None,
    )
    if first_manifest is None or first_manifest.suspect:
        _log.warning(
            "Cannot confirm baseline format from the first session manifest"
        )
    format_2 = marker is not None and marker.format >= 2
    if (
        not format_2
        and first_manifest is not None
        and not first_manifest.suspect
        and first_manifest.version >= 2
    ):
        encrypted_first = backend.read_paths(
            [f"session_{first:03d}/manifest.enc"]
        )
        format_2 = f"session_{first:03d}/manifest.enc" in encrypted_first
    if format_2:
        raise BaselineReadError(
            "This encrypted format 2 disc has a plaintext "
            "volume_baseline.json. Do not trust this disc."
        )
    if name == "volume_baseline.json":
        message = (
            "Warning: this disc is an old encrypted disc. It has plaintext "
            "metadata: volume_baseline.json."
        )
        _log.warning(message)
        print(message, file=sys.stderr)
    return marker, manifests


def _check_prior_baseline_checksum(
    backend: BurnBackend, crypto: CryptoBackend | None, found: dict[str, bytes],
    name: str, marker: VolumeMarker, manifests: list[Manifest] | None,
) -> list[Manifest] | None:
    """
    Input:  backend - disc backend; crypto - encryption backend
            found - baseline files; name - selected file
            marker - disc identity; manifests - decrypted manifests
    Output: manifests checked against the baseline checksum
    """
    if manifests is None:
        from oddarchiver.manifest import load_session_manifests
        manifests = load_session_manifests(
            backend, crypto, start_session=marker.first_session, marker=marker,
        )
    first_manifest = next((m for m in manifests if m.session == marker.first_session), None)
    if first_manifest is not None and first_manifest.suspect and first_manifest.version >= 3:
        raise BaselineReadError(
            "Cannot trust volume_baseline while the first session manifest is SUSPECT."
        )
    if first_manifest is not None and not first_manifest.suspect and first_manifest.version >= 3:
        expected = first_manifest.baseline_checksum
        if not expected or hashlib.sha256(found[name]).hexdigest() != expected:
            raise BaselineReadError("volume_baseline.enc checksum does not match the first session manifest.")
    return manifests


def _decrypt_baseline(
    data: bytes, name: str, crypto: CryptoBackend | None,
    marker: VolumeMarker | None,
) -> tuple[bytes, bool]:
    """
    Input:  data - encrypted baseline; name - disc filename
            crypto - disc encryption backend; marker - disc identity
    Output: decrypted baseline and bound-ciphertext indicator
    """
    from oddarchiver.crypto import NullCrypto

    if crypto is None or isinstance(crypto, NullCrypto):
        _log.warning("Cannot read %s: no key", name)
        raise BaselineReadError(
            f"Cannot read {name} on this disc. Give the archive key."
        )
    try:
        from oddarchiver.crypto import baseline_aad, is_bound_ciphertext
        bound = is_bound_ciphertext(data)
        if bound and marker is None:
            raise BaselineReadError("A bound volume_baseline needs volume.json.")
        data = crypto.decrypt(
            data,
            aad=baseline_aad(marker.archive_id, marker.volume_index) if bound else None,
        )
    except Exception as exc:
        _log.warning("Cannot decrypt %s: %s", name, exc)
        raise BaselineReadError(
            f"Cannot decrypt {name} on this disc. Make sure that the key "
            f"is correct and the disc is not damaged. Cause: {exc}"
        ) from exc
    return data, bound


def _decode_baseline_history(
    raw: dict, version: int,
) -> tuple[list[VolumeRecord], dict[str, list[PathVersion]]]:
    """
    Input:  raw - decoded baseline mapping; version - baseline format version
    Output: earlier disc records and path histories
    """
    if version == 2:
        if not isinstance(raw["volumes"], list) or not isinstance(raw["history"], dict):
            raise ValueError("volumes must be a list and history must be an object")
        if len(raw["history"]) > 2_000_000:
            raise ValueError("history has more than 2000000 paths")
        volumes = [_record(v) for v in raw["volumes"]]
        history = {}
        event_count = 0
        for path, versions in raw["history"].items():
            if not isinstance(versions, list):
                raise ValueError("path history must be a list")
            event_count += len(versions)
            if event_count > 2_000_000:
                raise ValueError("history has more than 2000000 events")
            history[_text(path, "history path")] = [_version(v) for v in versions]
    else:
        volumes, history = [], {}
    return volumes, history


def _baseline_header_version(
    raw: dict, name: str, marker: VolumeMarker | None, encrypted: bool, bound: bool,
) -> int:
    """
    Input:  raw - decoded baseline; name - disc filename
            marker - disc identity; encrypted - encryption in use
            bound - ciphertext bound to disc identity
    Output: validated baseline format version
    """
    if "archive_id" in raw or "volume_index" in raw:
        if marker is None or raw.get("archive_id") != marker.archive_id or raw.get("volume_index") != marker.volume_index:
            raise ValueError("volume_baseline identity does not match volume.json")
    elif name.endswith(".enc") and bound:
        raise ValueError("bound volume_baseline has no volume identity")
    version = raw.get("version", 1)
    # scbc ignore[type-equality] Reject bool as a baseline version
    if type(version) is not int or version not in (1, 2):
        raise ValueError(f"unsupported baseline version {version!r}")
    if encrypted and name.endswith(".json") and version != 1:
        raise ValueError("legacy plaintext baseline must have version 1")
    return version


def _layout_session_ranges(raw: object) -> dict[str, list[list[int]]]:
    """Input: layout session_ranges object. Output: volume to range pairs."""
    if not isinstance(raw, dict):
        raise ValueError("session_ranges must be an object")
    session_ranges: dict[str, list[list[int]]] = {}
    for key, value in raw.items():
        parsed = parse_session_ranges(value)
        if parsed is None:
            raise ValueError("invalid layout session_ranges")
        session_ranges[_text(str(key), "layout volume")] = parsed
    return session_ranges


def _not_stubbed_volumes(raw: object) -> list[int]:
    """Input: not_stubbed list. Output: volume indexes."""
    if not isinstance(raw, list):
        raise ValueError("not_stubbed must be a list")
    return [_nonnegative(item, "not_stubbed volume") for item in raw]


def _parse_layout(raw: object) -> ArchiveLayout | None:
    """Input: layout object or None. Output: ArchiveLayout or None."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("layout must be an object")
    session_ranges = _layout_session_ranges(raw["session_ranges"])
    stubbed = _not_stubbed_volumes(raw["not_stubbed"])
    return ArchiveLayout(
        highest_session=_nonnegative(raw["highest_session"], "highest_session"),
        highest_session_volume=_nonnegative(
            raw["highest_session_volume"], "highest_session_volume",
        ),
        session_ranges=session_ranges,
        not_stubbed=stubbed,
    )



def layout_covered_sessions(layout: ArchiveLayout | None) -> set[int]:
    """Input: layout or None. Output: sessions named by the layout ranges."""
    found: set[int] = set()
    if layout is None:
        return found
    for ranges in layout.session_ranges.values():
        for first, last in ranges:
            found.update(range(first, last + 1))
    found.add(layout.highest_session)
    return found


def baseline_covered_sessions(baseline: VolumeBaseline | None) -> set[int]:
    """Input: volume baseline or None. Output: sessions in origin, history, layout."""
    if baseline is None:
        return set()
    found: set[int] = set()
    origin = baseline.origin or {}
    found.update(origin.values())
    history = baseline.history or {}
    for versions in history.values():
        for version in versions:
            found.add(version.session)
    found |= layout_covered_sessions(baseline.layout)
    return found


def build_catalog_snapshot(
    prior: VolumeBaseline | None,
    manifests_by_volume: dict[int, list[Manifest]],
    volume_records: list[VolumeRecord],
    layout: ArchiveLayout,
) -> VolumeBaseline:
    """
    Input: prior baseline, manifests grouped by volume, volume records, layout
    Output: catalog snapshot with full change-set state
    """
    _volumes, history = _seed_baseline_history(prior)
    state = dict(prior.state) if prior else {}
    origin = dict(prior.origin) if prior else {}
    for index in sorted(manifests_by_volume):
        usable = [m for m in manifests_by_volume[index] if not m.suspect]
        _append_manifest_history(
            history,
            usable,
            VolumeMarker(
                archive_id="", volume_index=index, first_session=0,
                created_at="", previous_volume_label="",
            ),
        )
        state = build_disc_state(usable, seed=state)
        origin = build_disc_origin(usable, seed=origin)
    return VolumeBaseline(
        version=2, volumes=list(volume_records), history=history,
        state=state, origin=origin, layout=layout,
    )



def _decode_baseline_payload(
    data: bytes, name: str, marker: VolumeMarker | None, encrypted: bool, bound: bool,
) -> VolumeBaseline:
    """
    Input:  data - baseline payload; name - disc filename
            marker - disc identity; encrypted - encryption in use
            bound - ciphertext bound to disc identity
    Output: validated volume baseline
    """
    try:
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("baseline must be an object")
        version = _baseline_header_version(raw, name, marker, encrypted, bound)
        raw_state = raw["state"]
        raw_origin = raw["origin"]
        if not isinstance(raw_state, dict) or not isinstance(raw_origin, dict):
            raise ValueError("state and origin must be objects")
        state = {_text(k, "state path"): _text(v, "state checksum")
                 for k, v in raw_state.items()}
        origin = {_text(k, "origin path"): _nonnegative(v, "origin session")
                  for k, v in raw_origin.items()}
        volumes, history = _decode_baseline_history(raw, version)
        layout = _parse_layout(raw.get("layout")) if version == 2 else None
        return VolumeBaseline(version, volumes, history, state, origin, layout)
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, AttributeError) as exc:
        _log.warning("Cannot read %s: %s", name, exc)
        raise BaselineReadError(
            f"Cannot read {name} on this disc. Make sure that the key is "
            f"correct and the disc is not damaged. Cause: {exc}"
        ) from exc


def read_volume_baseline(
    backend: "BurnBackend", crypto: "CryptoBackend | None",
    marker: VolumeMarker | None = None,
    manifests: list[Manifest] | None = None,
) -> VolumeBaseline | None:
    """
    Input:  backend - disc backend
            crypto - disc encryption backend
            marker - disc identity, if known
            manifests - already decrypted disc manifests, if known
    Output: decoded baseline, or None only when both files are absent
    Details: Refuses plaintext metadata on encrypted format 2 discs.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    from oddarchiver.crypto import NullCrypto

    try:
        found = backend.read_paths(["volume_baseline.enc", "volume_baseline.json"])
    except Exception:
        _log.exception("Failed to read volume_baseline from backend")
        raise
    encrypted = crypto is not None and not isinstance(crypto, NullCrypto)
    if not found:
        return None
    name = "volume_baseline.enc" if "volume_baseline.enc" in found else "volume_baseline.json"
    if encrypted and "volume_baseline.json" in found:
        marker, manifests = _check_plaintext_baseline(
            backend, crypto, found, name, marker, manifests,
        )
    if marker is None:
        marker = read_volume_marker(backend)
    if marker is not None and marker.volume_index > 0:
        manifests = _check_prior_baseline_checksum(
            backend, crypto, found, name, marker, manifests,
        )
    data = found[name]
    bound = False
    if name.endswith(".enc"):
        data, bound = _decrypt_baseline(data, name, crypto, marker)
    return _decode_baseline_payload(data, name, marker, encrypted, bound)


def read_layout_stub(
    backend: "BurnBackend", crypto: "CryptoBackend | None",
    marker: VolumeMarker | None = None,
) -> VolumeBaseline | None:
    """
    Input:  backend, crypto, marker
    Output: catalog snapshot from volume.json, or None when absent
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    from oddarchiver.crypto import NullCrypto

    if marker is None:
        marker = read_volume_marker(backend)
    if marker is None:
        return None
    encrypted = crypto is not None and not isinstance(crypto, NullCrypto)
    if marker.catalog_snapshot_enc:
        data, bound = _decrypt_baseline(
            _b64_payload(marker.catalog_snapshot_enc, "catalog_snapshot_enc"),
            "volume.json", crypto, marker,
        )
        return _decode_baseline_payload(data, "volume.json", marker, encrypted, bound)

    if marker.catalog_snapshot is None:
        return None
    data = json.dumps(marker.catalog_snapshot).encode("utf-8")
    return _decode_baseline_payload(data, "volume.json", marker, False, False)



if __name__ == "__main__":
    pass
