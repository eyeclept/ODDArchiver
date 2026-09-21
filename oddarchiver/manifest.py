"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Session manifest read, write, and disc-state reconstruction.
    The manifest schema matches the DesignDoc JSON structure.
"""
# Imports
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.disc import BurnBackend

# Globals
MANIFEST_VERSION = 1
_SESSION_PROBE_LIMIT = 1000
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
    streamed: bool = False
    volume_chunk: int | None = None
    volume_chunk_count: int | None = None


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


@dataclass
class TreeEntry:
    """One path in the reconstructed archive tree as of a given session."""
    path: str
    size: int
    origin_session: int
    last_modified: str
    deleted: bool = False
    deleted_session: int | None = None


def write_manifest(
    staging_path: Path,
    manifest: Manifest,
    crypto: "CryptoBackend | None" = None,
) -> None:
    """
    Input:  staging_path - directory where the manifest will be written
            manifest     - Manifest dataclass instance
            crypto       - if provided (and not NullCrypto), write manifest.enc
                           and enc_mode.json. Otherwise write manifest.json
    Output: None
    Details:
        Writes atomically. It first writes a .tmp file and then calls
        rename. The code sets manifest_checksum over plaintext JSON
        before encryption. It removes the opposite format file when
        that file exists (enc and json swap). enc_mode.json includes a
        "rescue" object when crypto carries _rescue_block or
        manifest.encryption contains one.
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
        # Write a tiny plaintext mode indicator so _crypto_for_disc can determine
        # the encryption mode without first decrypting the manifest.
        from oddarchiver.crypto import PassphraseCrypto, KeyfileCrypto
        if isinstance(crypto, PassphraseCrypto):
            mode = "passphrase"
        elif isinstance(crypto, KeyfileCrypto):
            mode = "keyfile"
        else:
            mode = "none"
        enc_payload: dict = {"mode": mode}
        rescue = getattr(crypto, "_rescue_block", None)
        if not rescue:
            rescue = d.get("encryption", {}).get("rescue")
        if rescue:
            enc_payload["rescue"] = rescue
        (staging_path / "enc_mode.json").write_text(
            json.dumps(enc_payload), encoding="utf-8"
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
    Input:  path   - path to manifest.json (or manifest.enc)
            crypto - required when the manifest is encrypted. Ignored otherwise
    Output: Manifest - parsed and checksum-validated manifest
    Details:
        If path is manifest.json but the file does not exist, the code
        falls back to manifest.enc in the same directory. This keeps
        backward compat with encrypted sessions. The function validates
        manifest_checksum over plaintext JSON. A read, parse, or
        decrypt failure gives a blank suspect manifest. A checksum
        mismatch gives the parsed manifest with suspect=True.
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


def load_session_manifests(
    backend: "BurnBackend",
    crypto: "CryptoBackend | None" = None,
    start_session: int = 0,
    raw: dict[str, bytes] | None = None,
) -> list[Manifest]:
    """
    Input:  backend       - BurnBackend to read from
            crypto        - CryptoBackend for encrypted manifests (None means plaintext only)
            start_session - first session number to probe (default 0)
            raw           - optional already-fetched control-file bytes from
                            session_control_paths(); skips a second read_paths()
    Output: list of Manifest objects in ascending session order (may be empty)
    Details:
        Batch-extracts session_*/manifest.* and session_*/enc_mode.json
        through one backend.read_paths() call (unless raw is provided).
        Per session, it prefers manifest.enc over manifest.json. It
        stops at the first missing session at or after start_session.
        A session missing at start_session counts exactly like any
        other gap, not as a silent skip. A torn or failed read of the
        archive's first session therefore never counts as "nothing
        here yet." start_session exists only so a multi-volume disc
        whose real first session is not 0 (per its
        VolumeMarker.first_session) can tell this function where its
        own sessions legitimately begin. Callers must never pass it to
        cover an unexplained gap. The function does not use
        dvd+rw-mediainfo session count. On BD-R SRM+POW that count is
        always 1.
    """
    if raw is None:
        raw = backend.read_paths(session_control_paths())
    manifests: list[Manifest] = []
    with tempfile.TemporaryDirectory(prefix="oddarchiver_manifests_") as tmp:
        tmp_path = Path(tmp)
        s = start_session
        while s < _SESSION_PROBE_LIMIT:
            enc_path = f"session_{s:03d}/manifest.enc"
            json_path = f"session_{s:03d}/manifest.json"
            if enc_path in raw:
                data = raw[enc_path]
                suffix = ".enc"
            elif json_path in raw:
                data = raw[json_path]
                suffix = ".json"
            else:
                break
            tmp_file = tmp_path / f"manifest_{s:03d}{suffix}"
            tmp_file.write_bytes(data)
            manifests.append(read_manifest(tmp_file, crypto=crypto))
            s += 1
    return manifests


def session_control_paths() -> list[str]:
    """
    Input:  None
    Output: list of disc paths covering enc_mode.json and every session
            manifest.enc/manifest.json up to _SESSION_PROBE_LIMIT
    Details:
        Over-requests the 3-digit session namespace so
        load_session_manifests and cli._crypto_for_disc can share one
        read_paths() call without knowing the session count.
        BurnBackend.read_paths() omits missing paths. The list
        includes enc_mode.json for every session so a later volume
        whose first_session is not 0 still has its mode file in the
        same batch.
    """
    paths: list[str] = []
    for s in range(_SESSION_PROBE_LIMIT):
        paths.append(f"session_{s:03d}/enc_mode.json")
        paths.append(f"session_{s:03d}/manifest.enc")
        paths.append(f"session_{s:03d}/manifest.json")
    return paths


_session_control_paths = session_control_paths



def _suspect_manifest() -> Manifest:
    """Return a blank SUSPECT manifest for error paths."""
    return Manifest(
        version=0, session=-1, timestamp="", source="", label="",
        based_on_session=None, encryption={}, entries=[], deleted=[],
        manifest_checksum="", suspect=True,
    )


def build_disc_state(
    manifests: list[Manifest],
    *,
    seed: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    Input:  manifests - list of Manifest objects in ascending session order
            seed      - optional starting {path: checksum}; None treated as {}
    Output: dict mapping relative path -> result_checksum
    Details:
        Replays manifests in order, starting from a copy of seed (or
        {}). Each entry overwrites prior state. The code removes
        deleted paths from state and skips SUSPECT manifests. See
        build_disc_origin() below for the companion path -> session
        map (which session last wrote each path). session.build_staging()
        needs that map to fetch the correct cache-fallback blob for a
        changed file.
    """
    state: dict[str, str] = dict(seed) if seed is not None else {}
    for m in manifests:
        if m.suspect:
            _log.warning("Skipping SUSPECT manifest for session %d", m.session)
            continue
        for entry in m.entries:
            state[entry.path] = entry.result_checksum
        for deleted_path in m.deleted:
            state.pop(deleted_path, None)
    return state


def build_disc_origin(
    manifests: list[Manifest],
    *,
    seed: dict[str, int] | None = None,
) -> dict[str, int]:
    """
    Input:  manifests - list of Manifest objects in ascending session order
            seed      - optional starting {path: session}; None treated as {}
    Output: dict mapping relative path -> session number that last wrote it
    Details:
        Uses the same replay, skip-suspect, and pop-on-delete logic as
        build_disc_state(), starting from a copy of seed (or {}). It
        records which manifest's session most recently set each path,
        instead of the checksum it set. A file left unchanged across
        several syncs keeps the session number of its last real write,
        not the immediately preceding session. session.build_staging()
        uses this to pick the correct cache session for a changed
        file's prior blob. That blob may live several sessions back if
        the file was untouched in between.
    """
    origin: dict[str, int] = dict(seed) if seed is not None else {}
    for m in manifests:
        if m.suspect:
            _log.warning("Skipping SUSPECT manifest for session %d", m.session)
            continue
        for entry in m.entries:
            origin[entry.path] = m.session
        for deleted_path in m.deleted:
            origin.pop(deleted_path, None)
    return origin


def build_disc_tree(
    manifests: list[Manifest],
    at_session: int | None = None,
) -> list[TreeEntry]:
    """
    Input:  manifests  - session manifests in ascending order
            at_session - stop replay at this session index (None = latest)
    Output: TreeEntry list sorted by path, including tombstoned paths
    Details:
        Replays like build_disc_state/build_disc_origin, but a delete
        marks the path deleted=True instead of dropping it. BD-R is
        write-once, so the blob is still on the disc. last_modified on
        a tombstone is the deleting manifest's timestamp.
        deleted_session is that manifest's session number. The code
        skips SUSPECT manifests with a WARNING. origin_session and
        size come from the last write.
    """
    if at_session is not None:
        manifests = manifests[: at_session + 1]

    tree: dict[str, TreeEntry] = {}
    for m in manifests:
        if m.suspect:
            _log.warning("Skipping SUSPECT manifest for session %d", m.session)
            continue
        for entry in m.entries:
            tree[entry.path] = TreeEntry(
                path=entry.path,
                size=entry.full_size_bytes,
                origin_session=m.session,
                last_modified=m.timestamp,
                deleted=False,
                deleted_session=None,
            )
        for deleted_path in m.deleted:
            existing = tree.get(deleted_path)
            if existing is None:
                tree[deleted_path] = TreeEntry(
                    path=deleted_path,
                    size=0,
                    origin_session=m.session,
                    last_modified=m.timestamp,
                    deleted=True,
                    deleted_session=m.session,
                )
            else:
                tree[deleted_path] = TreeEntry(
                    path=existing.path,
                    size=existing.size,
                    origin_session=existing.origin_session,
                    last_modified=m.timestamp,
                    deleted=True,
                    deleted_session=m.session,
                )
    return sorted(tree.values(), key=lambda e: e.path)


def _compute_checksum(manifest_dict: dict[str, Any]) -> str:
    """
    Input:  manifest_dict - manifest as dict with manifest_checksum set to ""
    Output: hex sha256 digest
    Details:
        Canonical form: JSON with sorted keys, no trailing whitespace.
    """
    canonical = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def manifest_checksum_ok(manifest: Manifest) -> bool:
    """
    Input:  manifest - Manifest dataclass with a stored manifest_checksum
    Output: bool - True if a recomputed checksum matches the stored value
    Details:
        Recomputes the canonical checksum over the in-memory object.
        Post-burn fast verify uses this so the code does not re-read a
        still-settling drive. Manifests written before the streamed
        field (or the later volume_chunk/volume_chunk_count fields)
        existed omit those keys. A second pass strips default-valued
        streamed/volume_chunk/volume_chunk_count fields so those still
        verify.
    """
    d = dataclasses.asdict(manifest)
    d.pop("suspect", None)
    stored = d.get("manifest_checksum", "")
    d["manifest_checksum"] = ""
    if _compute_checksum(d) == stored:
        return True
    for entry in d.get("entries", []):
        if not entry.get("streamed"):
            entry.pop("streamed", None)
        if entry.get("volume_chunk") is None:
            entry.pop("volume_chunk", None)
        if entry.get("volume_chunk_count") is None:
            entry.pop("volume_chunk_count", None)
    return _compute_checksum(d) == stored


# Path validators. Reject traversal in manifest-supplied paths.

_BLOB_PATH_RE = re.compile(
    r"^session_\d{3}/(full|deltas)/[0-9a-f]{64}$"
)
_MANIFEST_PATH_RE = re.compile(
    r"^(?:session_\d{3}/(?:manifest\.enc|manifest\.json|enc_mode\.json)|volume\.json|volume_baseline\.json)$"
)


def validate_blob_path(path: str) -> str:
    """Return path if it is a legal blob path. Raise ValueError otherwise."""
    if not _BLOB_PATH_RE.fullmatch(path):
        raise ValueError(
            f"Refusing to read manifest-supplied blob path: {path!r}"
        )
    return path


def validate_disc_read_path(path: str) -> str:
    """Permit a blob path or a known manifest/control path. Raise ValueError otherwise."""
    if _BLOB_PATH_RE.fullmatch(path) or _MANIFEST_PATH_RE.fullmatch(path):
        return path
    raise ValueError(
        f"Refusing disc read of unrecognized path: {path!r}"
    )


def safe_join_under(root: Path, rel_path: str) -> Path:
    """Join root / rel_path and keep the result under root.

    Rejects absolute paths, paths containing '..' segments, and any
    joined path whose resolved form escapes root. Raises ValueError
    on rejection.
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
