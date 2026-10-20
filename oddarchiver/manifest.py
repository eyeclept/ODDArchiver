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
import sys
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.disc import BurnBackend
    from oddarchiver.volume import VolumeMarker

# Globals
MANIFEST_VERSION = 3
_SESSION_PROBE_LIMIT = 1000
logger = logging.getLogger(__name__)
_log = logger

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
    blob_origin_session: int | None = None


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
    baseline_checksum: str = ""
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
    volume_index: int | None = None
    volume_label: str = ""


def _write_encrypted_manifest(
    staging_path: Path,
    manifest: Manifest,
    crypto: CryptoBackend,
    d: dict,
    json_bytes: bytes,
) -> None:
    """
    Input:  staging_path - directory that receives the encrypted manifest
            manifest - manifest supplying the session and version
            crypto - backend that encrypts the JSON bytes
            d - serialized manifest fields for rescue data
            json_bytes - plaintext manifest bytes
    Output: None
    """
    from oddarchiver.crypto import manifest_aad
    payload = crypto.encrypt(
        json_bytes, aad=manifest_aad(manifest.session) if manifest.version >= 3 else None,
    )
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
    tmp.write_bytes(payload)
    os.replace(tmp, dest)


def write_manifest(
    staging_path: Path,
    manifest: Manifest,
    crypto: "CryptoBackend | None" = None,
) -> None:
    """
    Input:  staging_path - directory that receives the manifest
            manifest     - Manifest dataclass instance
            crypto       - optional CryptoBackend. When set and not
                           NullCrypto, write manifest.enc and
                           enc_mode.json. Otherwise write manifest.json
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
        _write_encrypted_manifest(staging_path, manifest, crypto, d, json_bytes)
        return

    dest = staging_path / "manifest.json"
    tmp = staging_path / "manifest.json.tmp"
    (staging_path / "manifest.enc").unlink(missing_ok=True)
    (staging_path / "enc_mode.json").unlink(missing_ok=True)

    tmp.write_bytes(json_bytes)
    os.replace(tmp, dest)


def _decrypt_manifest_bytes(
    actual: Path,
    raw_bytes: bytes,
    crypto: CryptoBackend | None,
    slot: int | None,
    path: Path,
) -> tuple[bytes, bool] | None:
    """
    Input:  actual - path of the manifest being decrypted
            raw_bytes - encrypted manifest bytes
            crypto - optional backend for decryption
            slot - session slot for bound ciphertext
            path - original path for warnings
    Output: decrypted bytes and binding state, or None on failure
    """
    if crypto is None:
        _log.warning("SUSPECT manifest at %s: encrypted but no crypto provided", path)
        return None
    try:
        from oddarchiver.crypto import is_bound_ciphertext, manifest_aad
        bound = is_bound_ciphertext(raw_bytes)
        if bound and slot is None:
            match = re.search(r"(?:session_|manifest_)(\d{3})", str(actual))
            if match is None:
                raise ValueError("A bound manifest requires its session slot.")
            slot = int(match.group(1))
        raw_bytes = crypto.decrypt(
            raw_bytes, aad=manifest_aad(slot) if bound and slot is not None else None,
        )
    except Exception as exc:
        _log.warning("SUSPECT manifest at %s: decryption failed: %s", path, exc)
        return None
    return raw_bytes, bound


def _parse_manifest_bytes(raw_bytes: bytes, actual: Path, bound: bool, path: Path) -> Manifest:
    """
    Input:  raw_bytes - plaintext manifest bytes
            actual - path used for schema errors and envelope format
            bound - whether encrypted bytes were bound to a session
            path - original path for warnings
    Output: parsed manifest or a blank SUSPECT manifest for invalid JSON
    """
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("SUSPECT manifest at %s: cannot parse JSON: %s", path, exc)
        return _suspect_manifest()

    if actual.suffix == ".enc" and (raw.get("version", 0) >= 3) != bound:
        _log.warning("SUSPECT manifest at %s: envelope version mismatch", path)
        return _suspect_manifest()
    entries_raw = raw.get("entries", [])
    if not isinstance(entries_raw, list) or len(entries_raw) > 1_000_000:
        raise ValueError(f"Invalid {actual}: entries must be a list of at most 1000000")
    stored_checksum = raw.get("manifest_checksum", "")
    check_dict = {k: v for k, v in raw.items() if k != "suspect"}
    check_dict["manifest_checksum"] = ""
    computed = _compute_checksum(check_dict)
    suspect = computed != stored_checksum

    entries = [ManifestEntry(**e) for e in entries_raw]
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
        baseline_checksum=raw.get("baseline_checksum", ""),
        suspect=suspect,
    )
    if suspect:
        _log.warning("SUSPECT manifest at %s: checksum mismatch", path)
    return manifest


def _read_manifest_source(
    path: Path,
    crypto: CryptoBackend | None,
    data: bytes | None,
) -> tuple[Path, bytes | None]:
    """
    Input:  path - requested manifest path
            crypto - optional backend for encrypted manifests
            data - optional bytes already read from the disc
    Output: actual path and its bytes, or None bytes on read failure
    """
    from oddarchiver.crypto import NullCrypto

    actual = path
    if data is None and path.suffix == ".json" and not path.exists():
        enc_candidate = path.with_name("manifest.enc")
        if enc_candidate.exists():
            actual = enc_candidate
    encrypted = crypto is not None and not isinstance(crypto, NullCrypto)
    if actual.suffix == ".json" and encrypted:
        raise ValueError(
            f"This encrypted disc has a plaintext {actual}. "
            "Do not trust this disc."
        )
    if data is None:
        try:
            data = actual.read_bytes()
        except OSError as exc:
            _log.warning("SUSPECT manifest at %s: cannot read: %s", actual, exc)
    return actual, data


def read_manifest(
    path: Path,
    crypto: "CryptoBackend | None" = None,
    *,
    format_2: bool = False,
    encrypted_mode: bool = False,
    data: bytes | None = None,
    slot: int | None = None,
) -> Manifest:
    """
    Input:  path - manifest path, used to identify its format and report errors
            crypto - key for encrypted manifests
            format_2 - whether this disc uses format 2
            encrypted_mode - whether enc_mode.json declares encryption
            data - optional manifest bytes already read from a disc
            slot - session directory number for encrypted manifest AAD
    Output: parsed and checksum-checked manifest
    Details:
        Refuses every plaintext manifest when a real key is supplied.
        Other read, decrypt, or parse failures give a blank SUSPECT
        manifest. A checksum mismatch gives the parsed manifest with
        suspect=True.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    actual, raw_bytes = _read_manifest_source(path, crypto, data)
    if raw_bytes is None:
        return _suspect_manifest()

    bound = False
    if actual.suffix == ".enc":
        decrypted = _decrypt_manifest_bytes(actual, raw_bytes, crypto, slot, path)
        if decrypted is None:
            return _suspect_manifest()
        raw_bytes, bound = decrypted
    return _parse_manifest_bytes(raw_bytes, actual, bound, path)


def _session_mode_declared(raw: dict[str, bytes], mode_path: str) -> bool:
    """
    Input:  raw - control-file bytes indexed by path
            mode_path - session encryption-mode path
    Output: whether the session declares encryption
    """
    if mode_path not in raw:
        return False
    try:
        mode_data = json.loads(raw[mode_path].decode("utf-8"))
        if not isinstance(mode_data, dict) or mode_data.get("mode") not in (
            "none", "passphrase", "keyfile",
        ):
            raise ValueError("invalid mode")
        return mode_data["mode"] != "none"
    except (ValueError, UnicodeDecodeError) as exc:
        _log.warning("Cannot parse %s: %s", mode_path, exc)
        raise ValueError(
            f"Cannot read {mode_path} on this disc: {exc}"
        ) from exc


def _parse_session_slot(
    raw: dict[str, bytes],
    s: int,
    crypto: CryptoBackend | None,
    marker: VolumeMarker | None,
    encrypted: bool,
) -> tuple[Manifest, str, bool] | None:
    """
    Input:  raw - control-file bytes indexed by path
            s - directory slot being probed
            crypto - backend for encrypted manifests, if any
            marker - volume format marker, if any
            encrypted - whether a real encryption key is supplied
    Output: parsed manifest, selected path, and mode declaration, or None at a gap
    """
    enc_path = f"session_{s:03d}/manifest.enc"
    json_path = f"session_{s:03d}/manifest.json"
    mode_path = f"session_{s:03d}/enc_mode.json"
    name = enc_path if enc_path in raw else json_path
    if name not in raw:
        return None
    mode_declared = _session_mode_declared(raw, mode_path)
    if name == json_path and (mode_declared or encrypted):
        raise ValueError(
            f"This encrypted disc has a plaintext {json_path}. "
            "Do not trust this disc."
        )
    manifest = read_manifest(
        Path(name), crypto=crypto,
        format_2=marker is not None and marker.format >= 2,
        encrypted_mode=mode_declared,
        data=raw[name],
        slot=s,
    )
    if not manifest.suspect and manifest.session != s:
        raise ValueError(
            f"Invalid {name}: manifest session {manifest.session!r} "
            f"does not match directory slot {s}."
        )
    return manifest, name, mode_declared


def _check_session_gap_and_plaintext(
    raw: dict[str, bytes],
    listed: object,
    gap_at: int | None,
    encrypted: bool,
) -> None:
    """
    Input:  raw - control-file bytes indexed by path
            listed - session numbers reported by the backend
            gap_at - first missing directory slot, if any
            encrypted - whether plaintext manifests are forbidden
    Output: None; raises for a later session past a gap or plaintext on an encrypted disc
    """
    if gap_at is not None and isinstance(listed, list):
        if gap_at not in listed:
            # scbc ignore[type-equality] Reject bool in the disc session listing
            later = [n for n in listed if type(n) is int and n > gap_at]
            if later:
                raise ValueError(
                    f"Session {gap_at} is missing on this disc but session "
                    f"{min(later)} exists."
                )
    if encrypted:
        for name in raw:
            if re.fullmatch(r"session_\d{3}/manifest\.json", name):
                raise ValueError(
                    f"This encrypted disc has a plaintext {name}. "
                    "Do not trust this disc."
                )


def load_session_manifests(
    backend: "BurnBackend",
    crypto: "CryptoBackend | None" = None,
    start_session: int = 0,
    raw: dict[str, bytes] | None = None,
    marker: "VolumeMarker | None" = None,
) -> list[Manifest]:
    """
    Input:  backend       - BurnBackend to read from
            crypto        - CryptoBackend for encrypted manifests (None means plaintext only)
            start_session - first session number to probe (default 0)
            raw           - optional already-fetched control-file bytes from
                            session_control_paths(), which skips a second
                            read_paths()
    Output: list of Manifest objects in ascending session order (may be empty)
    Details:
        Batch-extracts session_*/manifest.* and session_*/enc_mode.json
        through one backend.read_paths() call (unless the caller passes
        raw). Per session, it prefers manifest.enc over manifest.json. It
        stops at the first missing session at or after start_session.
        When list_session_numbers returns a list and a later session
        directory exists past that gap, the function raises instead of
        returning a truncated list. A session missing at start_session
        counts exactly like any other gap, not as a silent skip. A torn
        or failed read of the first session of the archive therefore
        never counts as "nothing here yet." start_session exists only so
        a multi-volume disc whose real first session is not 0 (per its
        VolumeMarker.first_session) can tell this function where its
        own sessions start. Callers must never pass it to cover an
        unexplained gap. The function does not use dvd+rw-mediainfo
        session count. On BD-R SRM+POW that count is always 1.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    if raw is None:
        raw = backend.read_paths(session_control_paths(backend))
    from oddarchiver.crypto import NullCrypto
    encrypted = crypto is not None and not isinstance(crypto, NullCrypto)
    lister = getattr(backend, "list_session_numbers", None)
    try:
        listed = lister() if callable(lister) else None
    except (RuntimeError, OSError) as exc:
        _log.warning("Cannot list sessions on this disc: %s", exc)
        listed = None
    parsed: list[tuple[Manifest, str, bool]] = []
    s = start_session
    gap_at: int | None = None
    while s < _SESSION_PROBE_LIMIT:
        item = _parse_session_slot(raw, s, crypto, marker, encrypted)
        if item is None:
            gap_at = s
            break
        parsed.append(item)
        s += 1
    _check_session_gap_and_plaintext(raw, listed, gap_at, encrypted)
    return [m for m, _name, _declared in parsed]


def session_control_paths(backend: "BurnBackend | None" = None) -> list[str]:
    """
    Input:  backend - optional backend that can list its session directories
    Output: control-file paths for enc_mode.json and both manifest forms
    Details:
        A backend that lists its session_NNN directories gets paths for
        those sessions only. One read_paths() call still serves both
        cli._crypto_for_disc and load_session_manifests. Gap detection
        stays in load_session_manifests. If a drive listing fails, the
        function warns and probes the full 3-digit session namespace.
    """
    # scbc boundary: Reject malformed disc or record fields before using them.
    lister = getattr(backend, "list_session_numbers", None)
    try:
        listed = lister() if callable(lister) else None
    except RuntimeError as exc:
        from oddarchiver.disc import DiscBackend

        if not isinstance(backend, DiscBackend):
            _log.warning("Session listing failed on non-drive backend: %s", exc)
            raise
        _log.warning("Session listing failed: %s. Using the full session probe.", exc)
        listed = None
    sessions = listed if isinstance(listed, list) else range(_SESSION_PROBE_LIMIT)
    paths: list[str] = []
    for s in sessions:
        # scbc ignore[type-equality] Reject bool in the disc session listing
        if type(s) is not int or not 0 <= s < _SESSION_PROBE_LIMIT:
            raise ValueError(f"Invalid session number on disc: {s!r}")
        paths.extend((
            f"session_{s:03d}/enc_mode.json",
            f"session_{s:03d}/manifest.enc",
            f"session_{s:03d}/manifest.json",
        ))
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
            seed      - optional starting {path: checksum}. None means {}
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
            seed      - optional starting {path: session}. None means {}
    Output: dict mapping relative path -> session number that last wrote it
    Details:
        Uses the same replay, skip-suspect, and pop-on-delete logic as
        build_disc_state(), starting from a copy of seed (or {}). It
        records the session of the manifest that most recently set
        each path, instead of the checksum it set. A file left
        unchanged across several syncs keeps the session number of
        its last real write, not the immediately preceding session.
        session.build_staging() uses this to pick the correct cache
        session for the prior blob of a changed file. That blob may
        live several sessions back when no sync touched the file in
        between.
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


def _apply_tree_deletions(tree: dict[str, TreeEntry], manifest: Manifest) -> None:
    """Apply deletions while retaining the last live size and origin."""
    for deleted_path in manifest.deleted:
        # scbc ignore[conditional-same-target-subscript-assign] Branches retain different live-file fields
        existing = tree.get(deleted_path)
        if existing is None:
            tree[deleted_path] = TreeEntry(
                path=deleted_path,
                size=0,
                origin_session=manifest.session,
                last_modified=manifest.timestamp,
                deleted=True,
                deleted_session=manifest.session,
            )
        else:
            tree[deleted_path] = TreeEntry(
                path=existing.path,
                size=existing.size,
                origin_session=existing.origin_session,
                last_modified=manifest.timestamp,
                deleted=True,
                deleted_session=manifest.session,
            )

def build_disc_tree(
    manifests: list[Manifest],
    at_session: int | None = None,
) -> list[TreeEntry]:
    """
    Input:  manifests  - session manifests in ascending order
            at_session - stop replay at this session number (None = latest)
    Output: TreeEntry list sorted by path, including tombstoned paths
    Details:
        Replays like build_disc_state/build_disc_origin, but a delete
        marks the path deleted=True instead of dropping it. BD-R is
        write-once, so the blob is still on the disc. last_modified on
        a tombstone is the timestamp of the deleting manifest.
        deleted_session is the session number of that manifest. The code
        skips SUSPECT manifests with a WARNING. origin_session and
        size come from the last write.
    """
    if at_session is not None:
        manifests = [m for m in manifests if m.session <= at_session]

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
        _apply_tree_deletions(tree, m)

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


def _legacy_manifest_checksum_ok(d: dict, stored: str, version: int) -> bool:
    """Check checksums that predate current manifest fields."""
    if version < 3:
        d.pop("baseline_checksum", None)
        for entry in d.get("entries", []):
            entry.pop("blob_origin_session", None)
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
    return _legacy_manifest_checksum_ok(d, stored, manifest.version)



# Path validators. Reject traversal in manifest-supplied paths.

_BLOB_PATH_RE = re.compile(
    r"^session_\d{3}/(full|deltas)/[0-9a-f]{64}(?:_\d+)?$"
)
_MANIFEST_PATH_RE = re.compile(
    r"^(?:session_\d{3}/(?:manifest\.enc|manifest\.json|enc_mode\.json)|"
    r"volume\.json|volume_baseline\.(?:enc|json)|oddarchiver-software/VERSION)$"
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
