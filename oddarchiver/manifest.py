"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Session manifest read, write, merge, and disc-state reconstruction.
    Manifest schema matches DesignDoc JSON structure.
"""
# Imports
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Globals
MANIFEST_VERSION = 1

# Functions


@dataclass
class ManifestEntry:
    """Single file entry within a session manifest."""
    path: str
    type: str          # "full" or "delta"
    result_checksum: str
    full_size_bytes: int
    source_checksum: str = ""
    delta_file: str = ""
    delta_size_bytes: int = 0
    file: str = ""
    encrypted_dek: str = ""


@dataclass
class Manifest:
    """Full session manifest."""
    version: int
    session: int
    timestamp: str
    source: str
    label: str
    based_on_session: int | None
    encryption: dict[str, Any]
    entries: list[ManifestEntry]
    deleted: list[str]
    manifest_checksum: str
    suspect: bool = field(default=False, compare=False)


def write_manifest(staging_path: Path, manifest: Manifest) -> None:
    """
    Input:  staging_path — directory where manifest.json will be written
            manifest     — Manifest dataclass instance
    Output: None
    Details:
        Writes atomically via .tmp then rename.
        Sets manifest_checksum before writing.
    """
    d = dataclasses.asdict(manifest)
    d.pop("suspect", None)
    d["manifest_checksum"] = ""
    d["manifest_checksum"] = _compute_checksum(d)
    manifest.manifest_checksum = d["manifest_checksum"]

    dest = staging_path / "manifest.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, dest)


def read_manifest(path: Path) -> Manifest:
    """
    Input:  path — path to manifest.json
    Output: Manifest — parsed and checksum-validated manifest
    Details:
        Validates manifest_checksum (sha256 of manifest with that field set to "").
        Sets manifest.suspect = True on checksum mismatch; does not raise.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logging.warning("SUSPECT manifest at %s: cannot parse JSON: %s", path, exc)
        return Manifest(
            version=0, session=-1, timestamp="", source="", label="",
            based_on_session=None, encryption={}, entries=[], deleted=[],
            manifest_checksum="", suspect=True,
        )
    stored_checksum = raw.get("manifest_checksum", "")
    check_dict = {k: v for k, v in raw.items() if k != "suspect"}
    check_dict["manifest_checksum"] = ""
    computed = _compute_checksum(check_dict)
    suspect = computed != stored_checksum

    entries = [ManifestEntry(**e) for e in raw.get("entries", [])]
    manifest = Manifest(
        version=raw["version"],
        session=raw["session"],
        timestamp=raw["timestamp"],
        source=raw["source"],
        label=raw["label"],
        based_on_session=raw.get("based_on_session"),
        encryption=raw.get("encryption", {}),
        entries=entries,
        deleted=raw.get("deleted", []),
        manifest_checksum=stored_checksum,
        suspect=suspect,
    )
    if suspect:
        logging.warning("SUSPECT manifest at %s: checksum mismatch", path)
    return manifest


def build_disc_state(manifests: list[Manifest]) -> dict[str, str]:
    """
    Input:  manifests — list of Manifest objects in ascending session order
    Output: dict mapping relative path -> result_checksum
    Details:
        Replays manifests in order; each entry overwrites prior state.
        SUSPECT manifests are skipped (logged, not raised).
    """
    state: dict[str, str] = {}
    for m in manifests:
        if m.suspect:
            logging.warning("Skipping SUSPECT manifest for session %d", m.session)
            continue
        for entry in m.entries:
            state[entry.path] = entry.result_checksum
        for deleted_path in m.deleted:
            state.pop(deleted_path, None)
    return state


def _compute_checksum(manifest_dict: dict[str, Any]) -> str:
    """
    Input:  manifest_dict — manifest as dict with manifest_checksum set to ""
    Output: hex sha256 digest
    Details:
        Canonical form: JSON with sorted keys, no trailing whitespace.
    """
    canonical = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


if __name__ == "__main__":
    pass
