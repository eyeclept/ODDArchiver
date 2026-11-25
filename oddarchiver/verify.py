"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Integrity checks at three levels: fast (manifest checksum),
    checksum (raw blob hashes), full (session-ordered rebuild of the
    latest state of every file, compared against result_checksum).
    --level full (TODO 23c) reuses the raw encrypted blobs that the
    checksum pass already read this run. It does not take a second
    optical pass through restore(). One disc scan total, not two.
"""
# Imports
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from oddarchiver.crypto import STREAM_CHUNK_SIZE, blob_aad
from oddarchiver.compress import CODEC_LZMA, CODEC_NONE, decode_file, decode_payload, decompress_bytes
from oddarchiver.diskread import DEFAULT_READ_MODE, make_session_source
from oddarchiver.filehash import hash_file
from oddarchiver.manifest import (
    Manifest, validate_blob_path, load_session_manifests, manifest_checksum_ok,
)
from oddarchiver.session import remove_tree

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals
VerifyLevel = Literal["fast", "checksum", "full"]

logger = logging.getLogger(__name__)
_log = logger

_BLOB_ERRORS = (OSError, ValueError, RuntimeError)


class _CaptureSource:
    """
    Input:  root - directory _check_blobs captures raw blobs into
    Output: N/A (class)
    Details:
        Minimal SessionSource-shaped wrapper (see oddarchiver.diskread)
        over the blobs _check_blobs already read this run. The
        content of every session lives under the same root (entry.file /
        entry.delta_file already embed their own "session_NNN/"
        prefix). open_session() ignores its argument and always
        returns root. This lets --level full drive
        restore.run_session_replay() without a live BurnBackend or a
        second disc read.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def open_session(self, session_n: int) -> Path:
        del session_n
        return self._root

# Functions





def _check_verify_coverage(
    backend: BurnBackend, manifests_by_session: dict[int, Manifest],
    control_raw: object | None = None,
) -> None:
    """
    Input:  backend - disc backend; manifests_by_session - manifests keyed by session
            control_raw - already-fetched controls or ControlFiles with marker
    Output: None
    """
    from oddarchiver.ledger import backend_identity, check_session_coverage
    from oddarchiver.volume import parse_volume_marker_bytes, read_volume_marker

    label = next(
        (m.label for m in manifests_by_session.values() if m.label), "",
    )
    try:
        disc_count = backend.mediainfo().session_count
    except Exception:
        _log.exception(
            "verify: mediainfo failed while checking ledger coverage"
        )
        disc_count = 0
    marker = getattr(control_raw, "marker", None)
    if marker is None and isinstance(control_raw, dict):
        marker = parse_volume_marker_bytes(control_raw.get("volume.json"))
    if marker is None:
        marker = read_volume_marker(backend)
    check_session_coverage(
        list(manifests_by_session.values()),
        label=label,
        backend_id=backend_identity(backend),
        recover_scan=False,
        disc_session_count=disc_count,
        backend=backend,
        archive_id=marker.archive_id if marker else None,
        volume_index=marker.volume_index if marker else None,
        session_ranges=marker.session_ranges if marker else None,
    )



def _verify_manifests(
    backend: BurnBackend,
    crypto: CryptoBackend,
    manifests: list[Manifest] | None,
    tmp_path: Path,
    skip_disc_io: bool,
    control_raw: dict[str, bytes] | None,
) -> tuple[dict[int, Manifest], int | None]:
    """
    Input:  backend - disc backend; crypto - manifest decryption backend;
            manifests - optional preloaded manifests; tmp_path - scratch directory;
            skip_disc_io - whether to avoid disc reads; control_raw - fetched controls
    Output: Manifests keyed by session and the incomplete session index, if any.
    """
    if skip_disc_io:
        manifests_by_session = {m.session: m for m in (manifests or [])}
        incomplete_idx = None
    elif manifests is not None:
        manifests_by_session = {m.session: m for m in manifests}
        _check_verify_coverage(backend, manifests_by_session, control_raw)
        incomplete_idx = detect_incomplete_session(
            backend, manifests_by_session, control_raw=control_raw,
        )
    else:
        manifests_by_session = _read_all_manifests(
            backend, tmp_path, crypto=crypto, raw=control_raw,
        )
        _check_verify_coverage(backend, manifests_by_session, control_raw)
        incomplete_idx = detect_incomplete_session(
            backend, manifests_by_session, control_raw=control_raw,
        )
    return manifests_by_session, incomplete_idx


def _verify_one_session(
    s: int,
    incomplete_idx: int | None,
    manifests_by_session: dict[int, Manifest],
    level: VerifyLevel,
    backend: BurnBackend,
    crypto: CryptoBackend,
    capture_dir: Path,
    session_source: object | None,
    split_progress: dict[str, tuple | None],
) -> int:
    """
    Input:  s - session index; incomplete_idx - incomplete slot, if any;
            manifests_by_session - manifests keyed by session; level - check depth;
            backend - disc backend; crypto - decryption backend;
            capture_dir - raw blob destination; session_source - disc reader;
            split_progress - progress shared between sessions
    Output: Number of errors printed for this session.
    """
    if incomplete_idx is not None and s == incomplete_idx:
        _print_session_incomplete(s)
        return 1
    manifest = manifests_by_session.get(s)
    if manifest is None:
        _print_session_fail(s, 0, [f"session_{s:03d}/manifest.json: unreadable"])
        return 1

    errors: list[str] = []
    _check_fast(manifest, s, manifests_by_session, errors)

    if level in ("checksum", "full") and not errors:
        _check_blobs(
            manifest, backend, crypto, errors,
            capture_dir=capture_dir if level == "full" else None,
            source=session_source,
            split_progress=split_progress,
        )

    file_count = len(manifest.entries)
    if errors:
        _print_session_fail(s, file_count, errors)
        return len(errors)
    _print_session_ok(s, file_count)
    return 0


def _verify_session_pass(
    backend: BurnBackend,
    crypto: CryptoBackend,
    level: VerifyLevel,
    manifests_by_session: dict[int, Manifest],
    incomplete_idx: int | None,
    tmp_path: Path,
    capture_dir: Path,
    read_mode: str,
    skip_disc_io: bool,
) -> int:
    """
    Input:  backend - disc backend; crypto - decryption backend; level - check depth;
            manifests_by_session - manifests keyed by session;
            incomplete_idx - incomplete slot, if any; tmp_path - scratch directory;
            capture_dir - raw blob destination; read_mode - disc read mode;
            skip_disc_io - whether to avoid disc reads
    Output: Total session errors.
    """
    session_indexes = set(manifests_by_session)
    if incomplete_idx is not None:
        session_indexes.add(incomplete_idx)

    session_source = None
    if level in ("checksum", "full") and not skip_disc_io:
        ordered = [
            manifests_by_session[i]
            for i in sorted(manifests_by_session)
        ]
        session_source = make_session_source(
            backend, ordered, tmp_path / "session_source", mode=read_mode,
        )

    split_progress: dict[str, tuple | None] = {}
    total_errors = 0
    try:
        for s in sorted(session_indexes):
            total_errors += _verify_one_session(
                s, incomplete_idx, manifests_by_session, level,
                backend, crypto, capture_dir, session_source, split_progress,
            )
    finally:
        # A chain that never completes (chunk on another disc, interrupt)
        # still owns a temp directory of decrypted payload.
        for state in split_progress.values():
            if state is not None and len(state) > 5:
                remove_tree(state[5].parent, "verify split temp")
    if level == "full":
        ordered_manifests = [
            manifests_by_session[s] for s in sorted(manifests_by_session)
        ]
        total_errors += _check_full(ordered_manifests, crypto, capture_dir, tmp_path)
    return total_errors


def verify(
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    level: VerifyLevel = "fast",
    manifests: list[Manifest] | None = None,
    read_mode: str = DEFAULT_READ_MODE,
    skip_disc_io: bool | None = None,
    control_raw: dict[str, bytes] | None = None,
) -> bool:
    """
    Input:  backend   - BurnBackend to read manifests and blobs from
            crypto    - CryptoBackend (used for encrypted manifests at every level)
            level     - verification depth: "fast", "checksum", or "full"
            manifests     - optional preloaded Manifest list
            read_mode     - "sequential" (default) or "full-copy" for blob reads
            skip_disc_io  - if True, do not scan the disc or probe incomplete
                            slots (post-burn in-memory path). Default True
                            when the caller passes manifests, False otherwise.
            control_raw   - optional already-fetched session control files
    Output: bool - True if all checks passed. Raises SystemExit(1) if total_errors != 0.
    Details:
        fast:     load manifests, flag suspect checksums, check session
                  index and timestamp ordering.
        checksum: all fast checks, then decrypt each blob. Hash full entries
                  against result_checksum. Check that delta entries decrypt.
        full:     run checksum checks, then reconstruct to a temp dir from
                  the blobs the checksum pass already captured. One optical
                  pass total, not a second restore() of the live backend.
        A failed session does not stop later sessions. The code prints
        per-session status. Output format matches the DesignDoc sample.
        The code skips blob checks for a session that already failed
        fast checks. When skip_disc_io is True (post-burn), the code
        checksums the in-memory objects and does not call
        read_path/read_paths on backend.
    """
    total_errors = 0
    if skip_disc_io is None:
        skip_disc_io = manifests is not None

    with tempfile.TemporaryDirectory(prefix="oddarchiver_verify_") as tmp:
        tmp_path = Path(tmp)
        capture_dir = tmp_path / "captured_blobs"
        manifests_by_session, incomplete_idx = _verify_manifests(
            backend, crypto, manifests, tmp_path, skip_disc_io, control_raw,
        )

        if not manifests_by_session and incomplete_idx is None:
            print("No sessions found.")
            return True

        max_session = max(manifests_by_session) if manifests_by_session else -1
        if incomplete_idx is not None:
            max_session = max(max_session, incomplete_idx)

        total_errors += _verify_session_pass(
            backend, crypto, level, manifests_by_session, incomplete_idx,
            tmp_path, capture_dir, read_mode, skip_disc_io,
        )

    print(
        f"\nResult: {total_errors} error{'s' if total_errors != 1 else ''}"
        f" across {max_session + 1} session{'s' if max_session + 1 != 1 else ''}."
    )
    if total_errors:
        raise SystemExit(1)
    return True


def _manifests_by_session(
    loaded: list[Manifest],
    listed: list[int] | None,
) -> dict[int, Manifest]:
    """Input: loaded manifests and listed slots. Output: session to manifest."""
    by_session: dict[int, Manifest] = {}
    used: set[int] = set()
    leftovers: list[Manifest] = []
    for manifest in loaded:
        if manifest.session >= 0:
            by_session[manifest.session] = manifest
            used.add(manifest.session)
        else:
            leftovers.append(manifest)
    if listed:
        unassigned = [number for number in listed if number not in used]
        for number, manifest in zip(unassigned, leftovers):
            by_session[number] = manifest
    return by_session


def _read_all_manifests(
    backend: "BurnBackend",
    tmp_path: Path | None = None,
    crypto: "CryptoBackend | None" = None,
    raw: dict[str, bytes] | None = None,
) -> dict[int, Manifest]:
    """
    Input:  backend  - BurnBackend to read from
            tmp_path - unused. Kept so existing call sites stay valid
            crypto   - CryptoBackend for encrypted manifests (None = plaintext)
            raw      - optional already-fetched control-file bytes
    Output: dict mapping session number -> Manifest
    Details:
        Thin adapter over load_session_manifests(). Keys are the directory
        session numbers so a gapped disc keeps the real session identity.
    """
    from oddarchiver.volume import read_volume_marker
    marker = read_volume_marker(backend)
    loaded = load_session_manifests(
        backend, crypto, raw=raw, marker=marker,
        start_session=marker.first_session if marker else 0,
    )
    try:
        listed = backend.list_session_numbers()
    except (RuntimeError, OSError) as exc:
        _log.warning("Cannot list sessions while reading manifests: %s", exc)
        listed = None
    return _manifests_by_session(loaded, listed)


def _later_unparsed_sessions(
    listed: list[int], manifests: dict[int, Manifest], last_clean: int,
) -> list[int]:
    """Input: listed sessions, parsed manifests, last clean session. Output: unparsed later sessions."""
    return [
        number for number in listed
        if number > last_clean and (
            number not in manifests
            or (manifests[number].suspect and manifests[number].session < 0)
        )
    ]


def detect_incomplete_session(
    backend: "BurnBackend",
    manifests: dict[int, Manifest],
    control_raw: dict[str, bytes] | None = None,
) -> int | None:
    """
    Input:  backend     - BurnBackend to inspect
            manifests   - session number -> Manifest already loaded
            control_raw - already-fetched session control files, if any
    Output: int session index of a dangling trailing slot, or None
    Details:
        A listed session after the last cleanly-parsed manifest is
        INCOMPLETE when code cannot fully read its manifest (blank
        suspect with session < 0) or a session directory exists with
        no readable manifest. A fully-parsed trailing suspect
        (checksum mismatch) stays FAIL. A lone unreadable session 0
        stays FAIL, so the code does not relabel a corrupted first
        session as an interrupted burn.

        The check uses list_session_numbers(), not last_clean + 1, so a
        gap that lives on another volume is not treated as a torn slot.
        The check uses the session listing and the control bytes that
        the caller already fetched. It never probes the medium with a
        blind read_path call.
    """
    last_clean, trailing_unreadable = _last_clean_and_unreadable(manifests)
    listed = _listed_trailing_sessions(backend)
    if listed is not None:
        if last_clean < 0:
            return None
        later_unparsed = _later_unparsed_sessions(listed, manifests, last_clean)
        return min(later_unparsed) if later_unparsed else None
    if trailing_unreadable is not None and last_clean >= 0:
        return trailing_unreadable
    if last_clean < 0:
        return None
    return _probe_trailing_slot(backend, last_clean, control_raw=control_raw)





def _last_clean_and_unreadable(
    manifests: dict[int, Manifest],
) -> tuple[int, int | None]:
    """Input: parsed manifests. Output: last clean session and trailing unreadable session."""
    last_clean = -1
    trailing_unreadable: int | None = None
    for s in sorted(manifests):
        manifest = manifests[s]
        if not manifest.suspect and manifest.session == s:
            last_clean = s
            trailing_unreadable = None
            continue
        if manifest.suspect and manifest.session < 0 and s == last_clean + 1:
            trailing_unreadable = s
            continue
        trailing_unreadable = None
    return last_clean, trailing_unreadable


def _split_digest_state(
    entry: ManifestEntry,
    split_progress: dict[str, tuple | None],
    idx: int,
    count: int,
) -> tuple[hashlib._Hash, int, str] | None:
    """Input: split entry state. Output: digest, next index, and target when valid."""
    state = split_progress.get(entry.path)
    if state is None and entry.path in split_progress:
        return None
    if state is None:
        if idx != 0:
            split_progress[entry.path] = None
            return None
        return hashlib.sha256(), 0, entry.result_checksum
    digest, next_idx, prev_count, target = state[:4]
    if prev_count != count or idx != next_idx:
        split_progress[entry.path] = None
        return None
    return digest, next_idx, target


def _listed_trailing_sessions(backend: "BurnBackend") -> list[int] | None:
    """Input: backend to inspect. Output: listed sessions or None after drive failure."""
    try:
        return backend.list_session_numbers()
    except RuntimeError as exc:
        from oddarchiver.disc import DiscBackend

        if not isinstance(backend, DiscBackend):
            _log.warning("Trailing session listing failed on non-drive backend: %s", exc)
            raise
        _log.warning("Trailing session listing failed: %s. Using control probe.", exc)
        return None


def _control_has_slot(control_raw: dict[str, bytes], nxt: int) -> bool:
    """Input: fetched control files and slot index. Output: True when its manifest exists."""
    for name in (f"session_{nxt:03d}/manifest.enc", f"session_{nxt:03d}/manifest.json"):
        if name in control_raw:
            return True
    return False


def _probe_trailing_slot(
    backend: "BurnBackend",
    last_clean: int,
    control_raw: dict[str, bytes] | None = None,
) -> int | None:
    """
    Input:  backend     - BurnBackend to inspect
            last_clean  - last fully-parsed session index
            control_raw - already-fetched session control files, if any
    Output: last_clean+1 if that slot looks like an interrupted burn, else None
    Details:
        A session that the listing does not show is a normal end of
        scan. A session that the listing shows but no readable
        manifest covers is an interrupted burn. If a drive listing
        fails, the code warns and checks control bytes from the full
        probe. It does not issue read_path probes.
    """
    nxt = last_clean + 1
    listed = _listed_trailing_sessions(backend)
    if listed is not None:
        return nxt if nxt in listed else None
    if control_raw is not None and _control_has_slot(control_raw, nxt):
        return nxt
    try:
        count = backend.mediainfo().session_count
    except Exception:
        _log.exception("incomplete probe: mediainfo failed")
        return None
    return nxt if count > nxt else None




def _check_fast(
    manifest: Manifest,
    session_idx: int,
    all_manifests: dict[int, Manifest],
    errors: list[str],
) -> None:
    """
    Input:  manifest      - Manifest to check
            session_idx   - expected session number
            all_manifests - all loaded manifests (for timestamp ordering)
            errors        - list to append error strings to
    Output: None (appends to errors)
    Details:
        Flags a suspect manifest_checksum, a session index mismatch, and a
        timestamp earlier than the previous session timestamp.
    """
    if manifest.suspect or not manifest_checksum_ok(manifest):
        errors.append(
            f"session_{session_idx:03d}/manifest.json: manifest_checksum mismatch"
        )

    if manifest.session != session_idx:
        errors.append(
            f"session_{session_idx:03d}/manifest.json: session field is"
            f" {manifest.session}, expected {session_idx}"
        )

    prev = all_manifests.get(session_idx - 1)
    if prev is not None and manifest.timestamp < prev.timestamp:
        errors.append(
            f"session_{session_idx:03d}/manifest.json: timestamp {manifest.timestamp!r}"
            f" is earlier than session {session_idx - 1} timestamp {prev.timestamp!r}"
        )


def _append_checksum_mismatch(
    errors: list[str], blob_path: str, expected: str, got: str,
) -> None:
    """Input: checksum mismatch details. Output: None (appends to errors)."""
    errors.append(
        f"{blob_path}: checksum mismatch\n"
        f"    expected: {expected[:16]}...\n"
        f"    got:      {got[:16]}..."
    )


def _check_streamed_blob(
    entry: ManifestEntry,
    blob_path: str,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    errors: list[str],
    capture_dir: Path | None,
    session_root: Path | None,
    split_progress: dict[str, tuple | None],
) -> None:
    """Input: streamed blob context. Output: None (appends verification errors)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="oddarchiver_verify_"))
    tmp_blob = tmp_dir / "blob"
    tmp_plain = tmp_dir / "plain"
    try:
        if session_root is not None:
            src = session_root / blob_path
            if not src.is_file():
                raise FileNotFoundError(blob_path)
            shutil.copy2(src, tmp_blob)
        else:
            backend.extract_path_to_file(blob_path, tmp_blob)
        if capture_dir is not None:
            captured = capture_dir / blob_path
            captured.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tmp_blob, captured)
        if entry.volume_chunk is not None:
            _fold_volume_split_chunk(
                entry, blob_path, tmp_blob, split_progress, errors, crypto,
            )
        elif entry.type == "full":
            tmp_stored = tmp_dir / "stored"
            crypto.decrypt_file(tmp_blob, tmp_stored, aad=blob_aad(entry))
            codec = getattr(entry, "codec", CODEC_NONE)
            if codec == CODEC_LZMA:
                decode_file(tmp_stored, tmp_plain, codec)
            else:
                # The decrypted bytes already equal the file; adopt them
                # rather than copying the whole blob again.
                os.replace(tmp_stored, tmp_plain)
            got = hash_file(tmp_plain, STREAM_CHUNK_SIZE).hexdigest()
            if got != entry.result_checksum:
                errors.append(
                    f"{blob_path}: checksum mismatch\n"
                    f"    expected: {entry.result_checksum[:16]}...\n"
                    f"    got:      {got[:16]}..."
                )
    except _BLOB_ERRORS as exc:
        _log.warning("Streamed blob read/decrypt failed for %s", blob_path, exc_info=exc)
        errors.append(f"{blob_path}: read/decrypt failed: {exc}")
    finally:
        remove_tree(tmp_dir, "verify temp")


def _check_buffered_blob(
    entry: ManifestEntry,
    blob_path: str,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    errors: list[str],
    capture_dir: Path | None,
    session_root: Path | None,
) -> None:
    """Input: buffered blob context. Output: None (appends verification errors)."""
    try:
        if session_root is not None:
            target = session_root / blob_path
            if not target.is_file():
                raise FileNotFoundError(blob_path)
            raw = target.read_bytes()
        else:
            raw = backend.read_path(blob_path)
        if capture_dir is not None:
            captured = capture_dir / blob_path
            captured.parent.mkdir(parents=True, exist_ok=True)
            captured.write_bytes(raw)
        plaintext = decode_payload(
            crypto.decrypt(raw, aad=blob_aad(entry)), getattr(entry, "codec", CODEC_NONE),
        )
    except _BLOB_ERRORS as exc:
        _log.warning("Buffered blob read/decrypt failed for %s", blob_path, exc_info=exc)
        errors.append(f"{blob_path}: read/decrypt failed: {exc}")
        return
    if entry.type == "full":
        got = hashlib.sha256(plaintext).hexdigest()
        if got != entry.result_checksum:
            errors.append(
                f"{blob_path}: checksum mismatch\n"
                f"    expected: {entry.result_checksum[:16]}...\n"
                f"    got:      {got[:16]}..."
            )


def _check_entry_blob(
    entry: ManifestEntry,
    blob_path: str,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    errors: list[str],
    capture_dir: Path | None,
    session_root: Path | None,
    split_progress: dict[str, tuple | None],
) -> None:
    """Input: validated blob context. Output: None (dispatches streamed or buffered check)."""
    if entry.streamed:
        _check_streamed_blob(
            entry, blob_path, backend, crypto, errors, capture_dir,
            session_root, split_progress,
        )
        return
    _check_buffered_blob(
        entry, blob_path, backend, crypto, errors, capture_dir, session_root,
    )


def _check_blobs(
    manifest: Manifest,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    errors: list[str],
    capture_dir: Path | None = None,
    source: object | None = None,
    split_progress: dict[str, tuple | None] | None = None,
) -> None:
    """
    Input:  manifest    - Manifest to check
            backend     - BurnBackend for blob reads
            crypto      - CryptoBackend for decryption
            errors      - list to append error strings to
            capture_dir - when set (--level full only), the call also
                          writes every raw (still-encrypted) blob it
                          reads under capture_dir at its own disc
                          path, so --level full can reconstruct
                          without a second disc read (see _check_full)
            source      - optional SessionSource. When set, the code
                          reads blobs from source.open_session()
                          instead of backend
            split_progress - session-ordered running concat state for
                          volume-split files. Callers share it across
                          _check_blobs calls so a complete 0..count-1
                          group hashes once, at the last chunk
    Output: None (appends to errors)
    Details:
        For "full" entries: decrypt the blob, then compare sha256 to result_checksum.
        For "delta" entries: decrypt the blob to check readability. No hash check.
        For a volume-split streamed entry the code checks readability
        only. result_checksum covers the whole file, so the concat
        hash runs when this session holds the last chunk and the
        replay already folded in every prior chunk in session order.
        The code records path-validation failures, read errors, auth
        failures, and hash mismatches.
    """
    if split_progress is None:
        split_progress = {}
    session_root = None
    if source is not None:
        session_root = source.open_session(manifest.session)
    for entry in manifest.entries:
        blob_path = entry.file if entry.type == "full" else entry.delta_file
        try:
            validate_blob_path(blob_path)
        except ValueError as exc:
            errors.append(f"{blob_path}: refused: {exc}")
            continue
        _check_entry_blob(
            entry, blob_path, backend, crypto, errors, capture_dir,
            session_root, split_progress,
        )

def _fold_volume_split_chunk(
    entry: ManifestEntry,
    blob_path: str,
    tmp_cipher: Path,
    split_progress: dict[str, tuple | None],
    errors: list[str],
    crypto: "CryptoBackend",
) -> None:
    """
    Input:  entry, blob_path - the volume-split chunk
            tmp_cipher       - ciphertext of this chunk
            split_progress   - running payload state keyed by rel_path
            errors           - list to append checksum mismatches to
            crypto           - decrypts each chunk
    Output: None
    Details:
        Each chunk carries its own ciphertext. Append the decrypted
        payload slices to a temp file, then decode and hash the whole
        payload after the last chunk.
    """
    count = entry.volume_chunk_count
    idx = entry.volume_chunk
    if count is None or idx is None:
        return
    state = _split_digest_state(entry, split_progress, idx, count)
    if state is None:
        return
    digest, next_idx, target = state
    extra = split_progress.get(entry.path)
    if extra is not None and len(extra) > 5:
        payload_path = extra[5]
    else:
        tmp_root = Path(tempfile.mkdtemp(prefix="oddarchiver_vsplit_verify_"))
        payload_path = tmp_root / "payload"
        payload_path.touch()
    chunk_plain = payload_path.with_name(f"chunk_{idx}")
    crypto.decrypt_file(tmp_cipher, chunk_plain, aad=blob_aad(entry))
    with payload_path.open("ab") as out, chunk_plain.open("rb") as src:
        shutil.copyfileobj(src, out)
    chunk_plain.unlink(missing_ok=True)
    next_idx += 1
    split_progress[entry.path] = (
        digest, next_idx, count, target, blob_path, payload_path,
    )
    if next_idx != count:
        return
    try:
        final = payload_path
        decoded = None
        if getattr(entry, "codec", CODEC_NONE) == CODEC_LZMA:
            decoded = payload_path.with_name("decoded")
            decode_file(payload_path, decoded, getattr(entry, "codec", CODEC_NONE))
            final = decoded
        if (
            getattr(entry, "original_size", None) is not None
            and final.stat().st_size != getattr(entry, "original_size", None)
        ):
            errors.append(
                f"{blob_path}: restored size {final.stat().st_size} does not "
                f"match original_size {getattr(entry, "original_size", None)}"
            )
            return
        got = hash_file(final, STREAM_CHUNK_SIZE).hexdigest()
        if got != target:
            errors.append(
                f"{blob_path}: checksum mismatch\n"
                f"    expected: {target[:16]}...\n"
                f"    got:      {got[:16]}..."
            )
    finally:
        remove_tree(payload_path.parent, "verify split temp")


def _check_full(
    manifests: list[Manifest],
    crypto: "CryptoBackend",
    capture_dir: Path,
    tmp_path: Path,
) -> int:
    """
    Input:  manifests   - ascending-session Manifest list already
                          loaded. For every session that reached this
                          check, _check_blobs already verified the
                          checksum this run
            crypto      - CryptoBackend for decryption
            capture_dir - raw encrypted blobs _check_blobs already
                          wrote for every entry it read. The code
                          reuses them here instead of a second disc
                          read
            tmp_path    - temp directory for the throwaway restore dest
    Output: number of reconstruction failures (0 on a clean archive)
    Details:
        Replaces the old restore()-as-black-box second full disc pass
        (23c). The code reconstructs the latest state of every file
        directly from the blobs _check_blobs already captured this
        run, through the same session-ordered replay engine that
        restore() uses (restore.run_session_replay), fed by
        _CaptureSource wrapping capture_dir instead of a live backend.
        This makes zero additional
        backend.read_path/read_paths/extract_path_to_file calls. A
        session _check_blobs never reached (its own fast check already
        failed, so the code skipped checksum-level blob checks for it)
        has no captured blobs. Any file whose chain needs one of its
        entries fails to reconstruct here. The error count for that
        session already covers it.
    """
    from oddarchiver.restore import _build_chains, run_session_replay

    dest = tmp_path / "full_restore"
    dest.mkdir()
    file_chains, deleted_at = _build_chains(manifests)
    max_session = max((m.session for m in manifests), default=-1)
    source = _CaptureSource(capture_dir)
    (_restored, failures, _files, _empty, unresolved,
     _stale, _missing) = run_session_replay(
        dest, source, crypto, file_chains, deleted_at, max_session,
        force=False, paths=None, prior_files={}, allow_unresolved=True,
        recover=False,
    )
    if unresolved:
        print(
            f"  {len(unresolved)} split file(s) span discs; verify them "
            "from the disc that holds chunk 0",
            flush=True,
        )
    return failures


def _print_session_ok(session_idx: int, file_count: int) -> None:
    """Print a single OK session line matching DesignDoc format."""
    noun = "file" if file_count == 1 else "files"
    print(f"Session {session_idx:03d}: {file_count:2d} {noun:<6} -- OK")


def _print_session_incomplete(session_idx: int) -> None:
    """Print an INCOMPLETE session line distinct from FAIL."""
    print(
        f"Session {session_idx:03d}: INCOMPLETE "
        "(interrupted burn, not corruption)"
    )


def _print_session_fail(
    session_idx: int,
    file_count: int,
    errors: list[str],
) -> None:
    """Print a FAIL session block with per-error detail."""
    noun = "file" if file_count == 1 else "files"
    print(f"Session {session_idx:03d}: {file_count:2d} {noun:<6} -- FAIL")
    for err in errors:
        for line in err.splitlines():
            print(f"  {line}")


if __name__ == "__main__":
    pass
