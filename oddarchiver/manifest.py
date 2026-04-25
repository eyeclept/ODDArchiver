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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.crypto import CryptoBackend

# Globals
MANIFEST_VERSION = 1
_log = logging.getLogger(__name__)

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
    drives: list[str] = field(default_factory=list)
    suspect: bool = field(default=False, compare=False)


def write_manifest(
    staging_path: Path,
    manifest: Manifest,
    crypto: "CryptoBackend | None" = None,
) -> None:
    """
    Input:  staging_path — directory where the manifest will be written
            manifest     — Manifest dataclass instance
            crypto       — if provided (and not NullCrypto), write manifest.enc
                           and enc_mode.json; otherwise write manifest.json
    Output: None
    Details:
        Writes atomically via .tmp then rename.
        Sets manifest_checksum over plaintext JSON before encrypting.
        Removes the opposite format file if it exists (enc ↔ json swap).
    """
    d = dataclasses.asdict(manifest)
    d.pop("suspect", None)
    d["manifest_checksum"] = ""
    d["manifest_checksum"] = _compute_checksum(d)
    manifest.manifest_checksum = d["manifest_checksum"]

    json_bytes = json.dumps(d, indent=2).encode("utf-8")

    # Lazy import to avoid circular dependency at module load time.
    use_crypto = False
    if crypto is not None:
        from oddarchiver.crypto import NullCrypto
        use_crypto = not isinstance(crypto, NullCrypto)

    if use_crypto:
        payload = crypto.encrypt(json_bytes)  # type: ignore[union-attr]
        dest = staging_path / "manifest.enc"
        tmp = staging_path / "manifest.enc.tmp"
        # Write tiny plaintext mode indicator so _crypto_for_disc can determine
        # the encryption mode without first decrypting the manifest.
        from oddarchiver.crypto import PassphraseCrypto, KeyfileCrypto
        if isinstance(crypto, PassphraseCrypto):
            mode = "passphrase"
        elif isinstance(crypto, KeyfileCrypto):
            mode = "keyfile"
        else:
            mode = "none"
        (staging_path / "enc_mode.json").write_text(
            json.dumps({"mode": mode}), encoding="utf-8"
        )
        (staging_path / "manifest.json").unlink(missing_ok=True)
    else:
        payload = json_bytes
        dest = staging_path / "manifest.json"
        tmp = staging_path / "manifest.json.tmp"
        (staging_path / "manifest.enc").unlink(missing_ok=True)
        (staging_path / "enc_mode.json").unlink(missing_ok=True)

    tmp.write_bytes(payload)
    os.replace(tmp, dest)


def read_manifest(
    path: Path,
    crypto: "CryptoBackend | None" = None,
) -> Manifest:
    """
    Input:  path   — path to manifest.json (or manifest.enc)
            crypto — required when the manifest is encrypted; ignored otherwise
    Output: Manifest — parsed and checksum-validated manifest
    Details:
        If path is manifest.json but does not exist, falls back to manifest.enc
        in the same directory (backward compat with encrypted sessions).
        Validates manifest_checksum over plaintext JSON.
        Sets manifest.suspect = True on any parse/decrypt/checksum failure.
    """
    actual = path
    if path.suffix == ".json" and not path.exists():
        enc_candidate = path.with_name("manifest.enc")
        if enc_candidate.exists():
            actual = enc_candidate

    try:
        raw_bytes = actual.read_bytes()
    except OSError as exc:
        _log.warning("SUSPECT manifest at %s: cannot read: %s", path, exc)
        return _suspect_manifest()

    if actual.suffix == ".enc":
        if crypto is None:
            _log.warning("SUSPECT manifest at %s: encrypted but no crypto provided", path)
            return _suspect_manifest()
        try:
            raw_bytes = crypto.decrypt(raw_bytes)
        except Exception as exc:
            _log.warning("SUSPECT manifest at %s: decryption failed: %s", path, exc)
            return _suspect_manifest()

    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("SUSPECT manifest at %s: cannot parse JSON: %s", path, exc)
        return _suspect_manifest()

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
        drives=raw.get("drives", []),
        suspect=suspect,
    )
    if suspect:
        _log.warning("SUSPECT manifest at %s: checksum mismatch", path)
    return manifest


def _suspect_manifest() -> Manifest:
    """Return a blank SUSPECT manifest for error paths."""
    return Manifest(
        version=0, session=-1, timestamp="", source="", label="",
        based_on_session=None, encryption={}, entries=[], deleted=[],
        manifest_checksum="", suspect=True,
    )


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
            _log.warning("Skipping SUSPECT manifest for session %d", m.session)
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


# Path validators — reject traversal in manifest-supplied paths.

_BLOB_PATH_RE = re.compile(
    r"^session_\d{3}/(full|deltas)/[0-9a-f]{64}$"
)
_MANIFEST_PATH_RE = re.compile(
    r"^session_\d{3}/(manifest\.enc|manifest\.json|enc_mode\.json)$"
)


def validate_blob_path(path: str) -> str:
    """Return path if it is a legal blob path; raise ValueError otherwise."""
    if not _BLOB_PATH_RE.fullmatch(path):
        raise ValueError(
            f"Refusing to read manifest-supplied blob path: {path!r}"
        )
    return path


def validate_disc_read_path(path: str) -> str:
    """Permit a blob path or a known manifest/control path; raise ValueError otherwise."""
    if _BLOB_PATH_RE.fullmatch(path) or _MANIFEST_PATH_RE.fullmatch(path):
        return path
    raise ValueError(
        f"Refusing disc read of unrecognized path: {path!r}"
    )


def safe_join_under(root: Path, rel_path: str) -> Path:
    """Join root / rel_path, ensuring the result stays under root.

    Rejects absolute paths, paths containing '..' segments, and any joined
    path whose resolved form escapes root. Raises ValueError on rejection.
    """
    if not rel_path or Path(rel_path).is_absolute():
        raise ValueError(f"Refusing absolute or empty path: {rel_path!r}")
    parts = Path(rel_path).parts
    if any(p == ".." for p in parts):
        raise ValueError(f"Refusing path with parent segment: {rel_path!r}")
    candidate = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValueError(
            f"Refusing path that escapes {root_resolved}: {rel_path!r}"
        )
    return candidate


if __name__ == "__main__":
    pass
