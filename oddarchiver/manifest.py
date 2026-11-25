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
from oddarchiver.compress import CODEC_NONE


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
    # Payload bytes this chunk holds. A resume sums the sizes of chunks
    # 0..k to find the exact offset of chunk k+1 in the payload.
    volume_chunk_size: int | None = None
    blob_origin_session: int | None = None
    codec: str = "none"
    original_size: int | None = None



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
    yubikey = getattr(crypto, "_yubikey_block", None)
    if not yubikey:
        yubikey = d.get("encryption", {}).get("yubikey")
    if yubikey:
        enc_payload["yubikey"] = yubikey
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
        that file exists (enc and json swap). enc_mode.json includes
        rescue and YubiKey blocks from crypto or manifest.encryption.
    """
    d = dataclasses.asdict(manifest)
    d.pop("suspect", None)
    _omit_default_codec_fields(d)
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


def _omit_entry_codec_defaults(entry: dict[str, Any]) -> None:
    """Input: one serialized entry. Output: None. Drops default fields."""
    if entry.get("codec", CODEC_NONE) == CODEC_NONE:
        entry.pop("codec", None)
        entry.pop("original_size", None)
    if entry.get("volume_chunk_size") is None:
        entry.pop("volume_chunk_size", None)


def _omit_default_codec_fields(manifest_dict: dict[str, Any]) -> None:
    """Input: serialized manifest. Output: None. Drops codec none fields."""
    for entry in manifest_dict.get("entries", []):
        _omit_entry_codec_defaults(entry)


def _entry_from_raw(raw: object) -> ManifestEntry:
    """Input: one JSON entry. Output: ManifestEntry with codec defaults."""
    if not isinstance(raw, dict):
        raise TypeError("manifest entry must be an object")
    data = dict(raw)
    codec = data.get("codec") or CODEC_NONE
    data["codec"] = codec
    if data.get("original_size") is None:
        if data.get("type") == "delta":
            data["original_size"] = data.get("delta_size_bytes", 0)
        else:
            data["original_size"] = data.get("full_size_bytes")
    return ManifestEntry(**data)


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

    entries = [_entry_from_raw(e) for e in entries_raw]
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


def _sessions_from_raw(raw: dict[str, bytes]) -> set[int]:
    """Input: control-file bytes. Output: session numbers present in the keys."""
    found: set[int] = set()
    for name in raw:
        match = re.match(r"^session_(\d{3})/", name)
        if match is not None:
            found.add(int(match.group(1)))
    return found


def _disc_session_slots(
    listed: object,
    raw: dict[str, bytes],
    start_session: int,
) -> list[int]:
    """
    Input:  listed - backend session listing, if any
            raw - control-file bytes
            start_session - first session this disc may hold
    Output: session numbers to parse, in order
    """
    found = _sessions_from_raw(raw)
    if isinstance(listed, list):
        for number in listed:
            # scbc ignore[type-equality] Reject bool in the disc session listing
            if type(number) is not int or not 0 <= number < _SESSION_PROBE_LIMIT:
                raise ValueError(f"Invalid session number on disc: {number!r}")
            found.add(number)
    return sorted(
        number for number in found
        if start_session <= number < _SESSION_PROBE_LIMIT
    )


def _raise_if_torn_session_gap(
    slots: list[int],
    parsed_slots: set[int],
    elsewhere: set[int],
    start_session: int,
) -> None:
    """
    Input:  slots - directories on this disc
            parsed_slots - slots with a readable manifest
            elsewhere - sessions catalog membership places on other discs
            start_session - first session this disc may hold
    Output: None; raises when a missing directory has a later session
    Details:
        A listed directory with no readable manifest is a torn slot.
        Coverage checks handle that. A gap in the directory list with a
        later session on this disc is an error unless catalog membership
        places the missing session on another disc.
    """
    on_disc = set(slots) | parsed_slots
    if not on_disc:
        return
    last = max(on_disc)
    for number in range(start_session, last):
        if number in parsed_slots or number in elsewhere:
            continue
        if number in slots:
            continue
        later = [slot for slot in on_disc if slot > number]
        if later:
            raise ValueError(
                f"Session {number} is missing on this disc but session "
                f"{min(later)} exists."
            )



def _elsewhere_from_marker(marker: "VolumeMarker | None") -> set[int]:
    """Input: volume marker. Output: holes in session_ranges on that disc."""
    ranges = getattr(marker, "session_ranges", None) if marker is not None else None
    if not ranges:
        return set()
    covered: set[int] = set()
    for pair in ranges:
        covered.update(range(pair[0], pair[1] + 1))
    if not covered:
        return set()
    return set(range(min(covered), max(covered) + 1)) - covered



def _check_encrypted_plaintext(raw: dict[str, bytes], encrypted: bool) -> None:
    """Input: control files and encryption flag. Output: None; raises on plaintext."""
    if not encrypted:
        return
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
    elsewhere_sessions: set[int] | None = None,
) -> list[Manifest]:
    """
    Input:  backend       - BurnBackend to read from
            crypto        - CryptoBackend for encrypted manifests (None means plaintext only)
            start_session - first session number to probe (default 0)
            raw           - optional already-fetched control-file bytes from
                            session_control_paths(), which skips a second
                            read_paths()
            elsewhere_sessions - sessions catalog membership places on other discs
    Output: list of Manifest objects in ascending session order (may be empty)
    Details:
        Batch-extracts session_*/manifest.* and session_*/enc_mode.json
        through one backend.read_paths() call (unless the caller passes
        raw). Per session, it prefers manifest.enc over manifest.json. It
        loads listed session_NNN directories in order. A listed slot with
        no readable manifest is torn and ends the trusted prefix.
        Coverage checks handle that slot. A missing directory with a
        later directory on the same disc raises unless catalog membership
        or volume.json session_ranges says that slot lives elsewhere.
        A session missing at start_session counts exactly like any other
        gap, not as a silent skip. start_session exists only so a
        multi-volume disc whose real first session is not 0 (per its
        VolumeMarker.first_session) can tell this function where its own
        sessions start. Callers must never pass it to cover an unexplained
        gap. The function does not use dvd+rw-mediainfo session count.
        On BD-R SRM+POW that count is always 1.
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
    slots = _disc_session_slots(listed, raw, start_session)
    parsed: list[tuple[Manifest, str, bool]] = []
    parsed_slots: set[int] = set()
    for slot in slots:
        item = _parse_session_slot(raw, slot, crypto, marker, encrypted)
        if item is None:
            break
        parsed.append(item)
        parsed_slots.add(slot)

    elsewhere = set(elsewhere_sessions or ())
    elsewhere |= _elsewhere_from_marker(marker)
    _raise_if_torn_session_gap(slots, parsed_slots, elsewhere, start_session)
    _check_encrypted_plaintext(raw, encrypted)
    parsed.sort(key=lambda item: item[0].session if item[0].session >= 0 else 0)
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
            if _split_is_incomplete(entry):
                # The burn stopped after this chunk, so the file is not
                # fully on disc. Drop it from state so no caller reports
                # it as up to date. A resume may still stage only the
                # missing tail of a clean prefix.
                _log.warning(
                    "Session %d holds chunk %d of %d for %s; not up to date",
                    m.session, entry.volume_chunk, entry.volume_chunk_count,
                    entry.path,
                )
                state.pop(entry.path, None)
        for deleted_path in m.deleted:
            state.pop(deleted_path, None)
    return state


def _split_is_incomplete(entry: ManifestEntry) -> bool:
    """Input: manifest entry. Output: True when later chunks were never burned."""
    if entry.volume_chunk is None or entry.volume_chunk_count is None:
        return False
    return entry.volume_chunk + 1 < entry.volume_chunk_count


@dataclass(frozen=True)
class SplitPrefix:
    """Chunks 0..next_chunk-1 of an interrupted volume split."""
    path: str
    chunk_count: int
    next_chunk: int
    payload_offset: int
    result_checksum: str
    original_size: int
    codec: str
    last_session: int


def _prefix_from_first_chunk(
    entry: ManifestEntry, size: int, session_n: int,
) -> SplitPrefix | None:
    """Input: chunk 0 entry, its size, and its session. Output: prefix or None."""
    if entry.original_size is None or entry.volume_chunk_count is None:
        return None
    return SplitPrefix(
        path=entry.path,
        chunk_count=entry.volume_chunk_count,
        next_chunk=1,
        payload_offset=size,
        result_checksum=entry.result_checksum,
        original_size=entry.original_size,
        codec=entry.codec,
        last_session=session_n,
    )


def _extend_split_prefix(
    prefix: SplitPrefix | None, entry: ManifestEntry, size: int,
    session_n: int,
) -> SplitPrefix | None:
    """
    Input: current prefix, next chunk entry, its size, and its session.
    Output: extended prefix, or None when the chain is not a clean prefix.
    """
    if entry.volume_chunk == 0:
        return _prefix_from_first_chunk(entry, size, session_n)
    if prefix is None:
        return None
    if (
        entry.volume_chunk != prefix.next_chunk
        or entry.volume_chunk_count != prefix.chunk_count
        or entry.result_checksum != prefix.result_checksum
        or entry.original_size != prefix.original_size
        or entry.codec != prefix.codec
    ):
        return None
    return SplitPrefix(
        path=prefix.path,
        chunk_count=prefix.chunk_count,
        next_chunk=prefix.next_chunk + 1,
        payload_offset=prefix.payload_offset + size,
        result_checksum=prefix.result_checksum,
        original_size=prefix.original_size,
        codec=prefix.codec,
        last_session=session_n,
    )


def _readable_chunk_size(entry: ManifestEntry) -> int | None:
    """Input: split chunk entry. Output: its payload size, or None if unusable."""
    if entry.type != "full" or not entry.file:
        return None
    size = entry.volume_chunk_size
    if size is None or size <= 0:
        return None
    chunk = entry.volume_chunk
    count = entry.volume_chunk_count
    if chunk is None or count is None or chunk >= count:
        return None
    return size


def build_split_prefixes(manifests: list[Manifest]) -> dict[str, SplitPrefix]:
    """
    Input:  manifests - list of Manifest objects in ascending session order
    Output: {path: SplitPrefix} for interrupted volume splits
    Details:
        Replays manifests like build_disc_state(). A path whose newest
        entries are consecutive chunks 0..k of one count, with recorded
        chunk sizes and the same result checksum, original size, and
        codec, leaves a clean prefix. The resume stages only chunks
        k+1..n-1 at the recorded offset. Any other shape clears the
        path, so the caller stages the whole file. Manifests written
        before volume_chunk_size existed cannot resume a tail; their
        chains clear here as well.
    """
    prefixes: dict[str, SplitPrefix] = {}
    for m in manifests:
        if m.suspect:
            continue
        for entry in m.entries:
            if entry.volume_chunk is None or entry.volume_chunk_count is None:
                prefixes.pop(entry.path, None)
                continue
            size = _readable_chunk_size(entry)
            if size is None:
                prefixes.pop(entry.path, None)
                continue
            prefix = _extend_split_prefix(
                prefixes.get(entry.path), entry, size, m.session,
            )
            if prefix is None or prefix.next_chunk >= prefix.chunk_count:
                prefixes.pop(entry.path, None)
            else:
                prefixes[entry.path] = prefix
        for deleted_path in m.deleted:
            prefixes.pop(deleted_path, None)
    return prefixes


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
        _omit_entry_codec_defaults(entry)
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
    # Compression added default-valued codec fields that the writer omits.
    # Drop them before the direct comparison, so a written manifest still
    # verifies without falling through to the legacy normalizations.
    _omit_default_codec_fields(d)
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
