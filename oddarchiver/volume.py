"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Volume identity for multi-disc archives. A VolumeMarker is written as
    plaintext volume.json at the staging root (sibling to session_NNN/)
    so later waves can number sessions across physical discs. A parallel
    volume_baseline.json sidecar carries checksum/origin maps from earlier
    volumes so sync on the latest disc can classify untouched files.
"""
# Imports
from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend

# Globals
_log = logging.getLogger(__name__)


@dataclass
class VolumeMarker:
    """Identity record for one physical volume of a logical archive."""

    archive_id: str
    volume_index: int
    first_session: int
    created_at: str
    previous_volume_label: str


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
    payload = json.dumps(dataclasses.asdict(marker), indent=2).encode("utf-8")
    tmp.write_bytes(payload)
    os.replace(tmp, dest)


def read_volume_marker(backend: "BurnBackend") -> VolumeMarker | None:
    """
    Input:  backend - BurnBackend to read volume.json from
    Output: VolumeMarker, or None when the disc has no usable marker
    Details:
        Calls backend.read_paths(["volume.json"]). Missing paths are
        omitted by the backend, not raised; None is the signal for a
        legacy/single-volume archive. Callers apply archive_id=None,
        volume_index=0, first_session=0 themselves. Decode failures
        also return None. A genuine backend/transport failure is
        logged and re-raised.
    """
    try:
        found = backend.read_paths(["volume.json"])
    except Exception:
        _log.exception("Failed to read volume.json from backend")
        raise
    data = found.get("volume.json")
    if data is None:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("Ignoring unreadable volume.json: %s", exc)
        return None
    if not isinstance(raw, dict):
        _log.warning("Ignoring unreadable volume.json: not an object")
        return None
    try:
        return VolumeMarker(
            archive_id=str(raw["archive_id"]),
            volume_index=int(raw["volume_index"]),
            first_session=int(raw["first_session"]),
            created_at=str(raw["created_at"]),
            previous_volume_label=str(raw["previous_volume_label"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _log.warning("Ignoring unreadable volume.json: %s", exc)
        return None


def write_volume_baseline(
    staging: Path,
    state: dict[str, str],
    origin: dict[str, int],
) -> None:
    """
    Input:  staging - staging root (sibling of the session_NNN/ directory)
            state   - {path: result_checksum} cumulative as of the prior volume
            origin  - {path: session} last-write session for each path in state
    Output: None
    Details:
        Serializes {"state": state, "origin": origin} to JSON and writes it
        atomically at staging/volume_baseline.json via a .tmp file then
        os.replace, matching write_volume_marker().
    """
    dest = staging / "volume_baseline.json"
    tmp = staging / "volume_baseline.json.tmp"
    payload = json.dumps(
        {"state": state, "origin": origin}, indent=2,
    ).encode("utf-8")
    tmp.write_bytes(payload)
    os.replace(tmp, dest)


def read_volume_baseline(
    backend: "BurnBackend",
) -> tuple[dict[str, str], dict[str, int]] | None:
    """
    Input:  backend - BurnBackend to read volume_baseline.json from
    Output: (state, origin) tuple, or None when the disc has no usable sidecar
    Details:
        Calls backend.read_paths(["volume_baseline.json"]). Missing paths
        are omitted by the backend, not raised; None is the signal for a
        volume-0 or legacy disc that never received a baseline. Decode
        failures also return None and are logged at warning level only.
        A genuine backend/transport failure is logged and re-raised.
    """
    try:
        found = backend.read_paths(["volume_baseline.json"])
    except Exception:
        _log.exception("Failed to read volume_baseline.json from backend")
        raise
    data = found.get("volume_baseline.json")
    if data is None:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("Ignoring unreadable volume_baseline.json: %s", exc)
        return None
    if not isinstance(raw, dict):
        _log.warning("Ignoring unreadable volume_baseline.json: not an object")
        return None
    raw_state = raw.get("state")
    raw_origin = raw.get("origin")
    if not isinstance(raw_state, dict) or not isinstance(raw_origin, dict):
        _log.warning(
            "Ignoring unreadable volume_baseline.json: missing state/origin"
        )
        return None
    try:
        state = {str(k): str(v) for k, v in raw_state.items()}
        origin = {str(k): int(v) for k, v in raw_origin.items()}
    except (TypeError, ValueError) as exc:
        _log.warning("Ignoring unreadable volume_baseline.json: %s", exc)
        return None
    return state, origin


if __name__ == "__main__":
    pass
