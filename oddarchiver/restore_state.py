"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Restore-state file for one destination, for multi-volume restore.
    The file records which volumes of a marked archive the restore
    applied to dest. It also records the per-file checksums replayed
    from those volumes. A later invocation can then skip volumes it
    already applied, and can apply deltas that rest on the
    reconstructed bytes in dest.
"""
# Imports
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from oddarchiver.ledger import ledger_dir

# Globals
_log = logging.getLogger(__name__)

STATE_FILENAME = ".oddarchiver_restore_state.json"


@dataclass
class FileReplay:
    """Last fully reconstructed state of one path after the applied volumes."""

    checksum: str
    last_session: int
    has_full: bool
    volume_chunk: int | None = None
    volume_chunk_count: int | None = None


@dataclass
class VolumeRecord:
    """Identity of one volume that this restore has already seen."""

    volume_index: int
    first_session: int
    last_session: int
    label: str
    previous_volume_label: str


@dataclass
class RestoreState:
    """
    Contents of dest/.oddarchiver_restore_state.json.

    files is a compact replay dict (path -> last checksum / session /
    has_full), in the same spirit as manifest.build_disc_state. The
    state does not store blobs. dest holds the reconstructed bytes. A
    delta from a later volume applies on top of dest when dest matches the
    stored checksum. deleted maps path -> last deleting session.
    """

    archive_id: str
    dest: str
    session: int | None
    applied_volumes: list[int] = field(default_factory=list)
    volumes: dict[int, VolumeRecord] = field(default_factory=dict)
    files: dict[str, FileReplay] = field(default_factory=dict)
    deleted: dict[str, int] = field(default_factory=dict)


# Functions


def state_path(dest: Path, archive_id: str) -> Path:
    """
    Input:  dest       - restore destination directory
            archive_id - VolumeMarker.archive_id that this state belongs to
    Output: Path to the restore-state file under the shared oddarchiver
            state directory (ledger.ledger_dir()). The path is never
            inside dest.
    Details:
        The file lives outside dest. A restore of a marked archive
        then never adds a bookkeeping file to the reconstructed tree. dest
        stays a byte-for-byte mirror of the source. This matches the
        legacy (unmarked) restore path, and every existing restore
        test expects this. The key combines archive_id and the
        resolved path of dest, hashed for a stable filesystem-safe
        name. This mirrors the keying rationale of ledger_path()
        (label, backend_id).
    """
    key = f"{archive_id}:{Path(dest).resolve()}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return ledger_dir() / f"restore-{digest}.state.json"


def load_restore_state(dest: Path, archive_id: str) -> RestoreState | None:
    """
    Input:  dest       - restore destination directory
            archive_id - VolumeMarker.archive_id of the disc in restore now
    Output: RestoreState. None when no state file exists for this
            (archive_id, dest) pair.
    Details:
        A missing file means no prior multi-volume restore for this
        archive against this dest. A present but unreadable file is a
        hard error. If the code treated it as empty, it would process
        volumes again and hide a real failure.
    """
    path = state_path(dest, archive_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        _log.exception("Failed to read restore-state file %s", path)
        raise RuntimeError(f"Corrupt restore-state file: {path}") from None
    if not isinstance(raw, dict):
        _log.warning("Restore-state file %s is not a JSON object", path)
        raise RuntimeError(f"Corrupt restore-state file: {path}")
    try:
        return _parse_state(raw)
    except (KeyError, TypeError, ValueError):
        _log.exception("Failed to parse restore-state file %s", path)
        raise RuntimeError(f"Corrupt restore-state file: {path}") from None


def write_restore_state(dest: Path, state: RestoreState) -> None:
    """
    Input:  dest  - restore destination directory (used only for the key,
                    matching state_path())
            state - RestoreState to persist. state.archive_id supplies
                    the other half of the key
    Output: None
    Details:
        Writes atomically. It first writes a .tmp file, then calls
        os.replace. This matches manifest.write_manifest. The file
        lives under ledger_dir(), not dest. See state_path().
    """
    path = state_path(dest, state.archive_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(_state_to_dict(state), indent=2).encode("utf-8")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except OSError:
        _log.exception("Failed to write restore-state file %s", path)
        raise


def new_restore_state(
    dest: Path,
    archive_id: str,
    session: int | None,
) -> RestoreState:
    """
    Input:  dest       - restore destination directory
            archive_id - VolumeMarker.archive_id of the first marked volume
            session    - --session target, or None for the latest session
    Output: empty RestoreState for a new multi-volume restore
    """
    return RestoreState(
        archive_id=archive_id,
        dest=str(Path(dest).resolve()),
        session=session,
    )


def _parse_state(raw: dict) -> RestoreState:
    """
    Input:  raw - decoded JSON object
    Output: RestoreState
    Details:
        Raises KeyError/TypeError/ValueError for a malformed field.
    """
    session_raw = raw.get("session", None)
    session: int | None
    if session_raw is None:
        session = None
    else:
        session = int(session_raw)

    applied = [int(v) for v in raw.get("applied_volumes", [])]
    volumes: dict[int, VolumeRecord] = {}
    for item in raw.get("volumes", []):
        rec = VolumeRecord(
            volume_index=int(item["volume_index"]),
            first_session=int(item["first_session"]),
            last_session=int(item["last_session"]),
            label=str(item.get("label", "")),
            previous_volume_label=str(item.get("previous_volume_label", "")),
        )
        volumes[rec.volume_index] = rec

    files: dict[str, FileReplay] = {}
    for path, item in raw.get("files", {}).items():
        files[str(path)] = FileReplay(
            checksum=str(item["checksum"]),
            last_session=int(item["last_session"]),
            has_full=bool(item["has_full"]),
            volume_chunk=(
                int(item["volume_chunk"])
                if item.get("volume_chunk") is not None else None
            ),
            volume_chunk_count=(
                int(item["volume_chunk_count"])
                if item.get("volume_chunk_count") is not None else None
            ),
        )

    deleted = {str(p): int(s) for p, s in raw.get("deleted", {}).items()}

    return RestoreState(
        archive_id=str(raw["archive_id"]),
        dest=str(raw.get("dest", "")),
        session=session,
        applied_volumes=sorted(set(applied)),
        volumes=volumes,
        files=files,
        deleted=deleted,
    )


def _state_to_dict(state: RestoreState) -> dict:
    """
    Input:  state - RestoreState
    Output: JSON-serializable dict
    """
    volumes = [
        {
            "volume_index": rec.volume_index,
            "first_session": rec.first_session,
            "last_session": rec.last_session,
            "label": rec.label,
            "previous_volume_label": rec.previous_volume_label,
        }
        for rec in sorted(state.volumes.values(), key=lambda r: r.volume_index)
    ]
    files = {
        path: {
            "checksum": replay.checksum,
            "last_session": replay.last_session,
            "has_full": replay.has_full,
            "volume_chunk": replay.volume_chunk,
            "volume_chunk_count": replay.volume_chunk_count,
        }
        for path, replay in sorted(state.files.items())
    }
    deleted = {path: session_n for path, session_n in sorted(state.deleted.items())}
    return {
        "archive_id": state.archive_id,
        "dest": state.dest,
        "session": state.session,
        "applied_volumes": list(state.applied_volumes),
        "volumes": volumes,
        "files": files,
        "deleted": deleted,
    }


if __name__ == "__main__":
    pass
