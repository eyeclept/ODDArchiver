"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Integrity checks at three levels: fast (manifest checksum),
    checksum (raw blob hashes), full (session-ordered reconstruction of
    every file's latest state, compared against result_checksum).
    --level full (TODO 23c) reuses the raw encrypted blobs that the
    checksum pass already read this run. It does not take a second
    optical pass through restore(). One disc scan total, not two.
"""
# Imports
from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from oddarchiver.crypto import STREAM_CHUNK_SIZE
from oddarchiver.diskread import DEFAULT_READ_MODE, make_session_source
from oddarchiver.manifest import (
    Manifest, validate_blob_path, load_session_manifests, manifest_checksum_ok,
)

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals
VerifyLevel = Literal["fast", "checksum", "full"]

_log = logging.getLogger(__name__)

_BLOB_ERRORS = (OSError, ValueError, RuntimeError)


class _CaptureSource:
    """
    Input:  root - directory _check_blobs has been capturing raw blobs into
    Output: N/A (class)
    Details:
        Minimal SessionSource-shaped wrapper (see oddarchiver.diskread)
        over the blobs _check_blobs already read this run. Every
        session's content lives under the same root (entry.file /
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


def _sha256_path(path: Path) -> str:
    """
    Input:  path - file to hash
    Output: hex sha256 digest
    Details:
        Reads STREAM_CHUNK_SIZE chunks so a large plaintext is never
        fully materialized.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()



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
                            when manifests is provided, False otherwise.
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
        if skip_disc_io:
            manifests_by_session = {m.session: m for m in (manifests or [])}
            incomplete_idx = None
        elif manifests is not None:
            manifests_by_session = {m.session: m for m in manifests}
            from oddarchiver.ledger import backend_identity, check_session_coverage
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
            check_session_coverage(
                list(manifests_by_session.values()),
                label=label,
                backend_id=backend_identity(backend),
                recover_scan=False,
                disc_session_count=disc_count,
                backend=backend,
            )
            incomplete_idx = detect_incomplete_session(backend, manifests_by_session)
        else:
            manifests_by_session = _read_all_manifests(
                backend, tmp_path, crypto=crypto, raw=control_raw,
            )
            from oddarchiver.ledger import backend_identity, check_session_coverage
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
            check_session_coverage(
                list(manifests_by_session.values()),
                label=label,
                backend_id=backend_identity(backend),
                recover_scan=False,
                disc_session_count=disc_count,
                backend=backend,
            )
            incomplete_idx = detect_incomplete_session(backend, manifests_by_session)

        if not manifests_by_session and incomplete_idx is None:
            print("No sessions found.")
            return True

        max_session = max(manifests_by_session) if manifests_by_session else -1
        if incomplete_idx is not None:
            max_session = max(max_session, incomplete_idx)

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
        for s in sorted(session_indexes):
            if incomplete_idx is not None and s == incomplete_idx:
                _print_session_incomplete(s)
                total_errors += 1
                continue
            manifest = manifests_by_session.get(s)
            if manifest is None:
                _print_session_fail(s, 0, [f"session_{s:03d}/manifest.json: unreadable"])
                total_errors += 1
                continue

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
                total_errors += len(errors)
            else:
                _print_session_ok(s, file_count)
        if level == "full":
            ordered_manifests = [
                manifests_by_session[s] for s in sorted(manifests_by_session)
            ]
            total_errors += _check_full(ordered_manifests, crypto, capture_dir, tmp_path)

    print(
        f"\nResult: {total_errors} error{'s' if total_errors != 1 else ''}"
        f" across {max_session + 1} session{'s' if max_session + 1 != 1 else ''}."
    )
    if total_errors:
        raise SystemExit(1)
    return True


def _read_all_manifests(
    backend: "BurnBackend",
    tmp_path: Path | None = None,
    crypto: "CryptoBackend | None" = None,
    raw: dict[str, bytes] | None = None,
) -> dict[int, Manifest]:
    """
    Input:  backend  - BurnBackend to read from
            tmp_path - unused; kept so existing call sites stay valid
            crypto   - CryptoBackend for encrypted manifests (None = plaintext)
            raw      - optional already-fetched control-file bytes
    Output: dict mapping session index -> Manifest
    Details:
        Thin adapter over load_session_manifests(). Keys are contiguous
        probe indices so a mismatched session field is still detected.
    """
    loaded = load_session_manifests(backend, crypto, raw=raw)
    return {i: m for i, m in enumerate(loaded)}

def detect_incomplete_session(
    backend: "BurnBackend",
    manifests: dict[int, Manifest],
) -> int | None:
    """
    Input:  backend   - BurnBackend to probe a trailing session slot
            manifests - session index -> Manifest already loaded
    Output: int session index of a dangling trailing slot, or None
    Details:
        A trailing slot beyond the last cleanly-parsed manifest is
        INCOMPLETE when its manifest is truncated/unreadable (blank
        suspect with session < 0) or a session directory exists with
        no readable manifest. A fully-parsed trailing suspect
        (checksum mismatch) stays FAIL. A lone unreadable session 0
        stays FAIL, so the code does not relabel a corrupted first
        session as an interrupted burn.
    """
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
    if trailing_unreadable is not None and last_clean >= 0:
        return trailing_unreadable
    if last_clean < 0:
        return None
    return _probe_trailing_slot(backend, last_clean)


def _probe_trailing_slot(backend: "BurnBackend", last_clean: int) -> int | None:
    """
    Input:  backend    - BurnBackend to probe
            last_clean - last fully-parsed session index
    Output: last_clean+1 if that slot looks like an interrupted burn, else None
    Details:
        FileNotFound on both manifest names is a normal end-of-scan
        unless mediainfo reports more session directories than the
        clean count.
    """
    nxt = last_clean + 1
    for disc_path in (
        f"session_{nxt:03d}/manifest.enc",
        f"session_{nxt:03d}/manifest.json",
    ):
        try:
            backend.read_path(disc_path)
            return nxt
        except FileNotFoundError:
            _log.warning("incomplete probe: %s not present", disc_path)
            continue
        except RuntimeError:
            _log.exception("incomplete probe: read failed for %s", disc_path)
            return nxt
        except (OSError, ValueError) as exc:
            _log.warning("incomplete probe: %s: %s", disc_path, exc)
            continue
    try:
        count = backend.mediainfo().session_count
    except Exception:
        _log.exception("incomplete probe: mediainfo failed")
        return None
    if count > last_clean + 1:
        return nxt
    return None


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
            capture_dir - when set (--level full only), every raw
                          (still-encrypted) blob this call reads is also
                          written under capture_dir at its own disc path,
                          so --level full can reconstruct without a
                          second disc read (see _check_full)
            source      - optional SessionSource; when set, blobs are read
                          from source.open_session() instead of backend
            split_progress - session-ordered running concat state for
                          volume-split files; shared across _check_blobs
                          calls so a complete 0..count-1 group hashes
                          once, at the last chunk
    Output: None (appends to errors)
    Details:
        For "full" entries: decrypt the blob, then compare sha256 to result_checksum.
        For "delta" entries: decrypt the blob to check readability. No hash check.
        A volume-split streamed entry is decrypted for readability only;
        result_checksum is the whole file, so the concat hash runs when
        this session holds the last chunk and every prior chunk was
        already folded in session order.
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
        if entry.streamed:
            tmp_dir = Path(tempfile.mkdtemp(prefix="oddarchiver_verify_"))
            tmp_blob = tmp_dir / "blob"
            tmp_plain = tmp_dir / "plain"
            try:
                if session_root is not None:
                    src = session_root / entry.file
                    if not src.is_file():
                        raise FileNotFoundError(entry.file)
                    shutil.copy2(src, tmp_blob)
                else:
                    backend.extract_path_to_file(entry.file, tmp_blob)
                if capture_dir is not None:
                    captured = capture_dir / blob_path
                    captured.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(tmp_blob, captured)
                crypto.decrypt_file(tmp_blob, tmp_plain)
                if entry.volume_chunk is not None:
                    _fold_volume_split_chunk(
                        entry, blob_path, tmp_plain, split_progress, errors,
                    )
                elif entry.type == "full":
                    got = _sha256_path(tmp_plain)
                    if got != entry.result_checksum:
                        errors.append(
                            f"{blob_path}: checksum mismatch\n"
                            f"    expected: {entry.result_checksum[:16]}...\n"
                            f"    got:      {got[:16]}..."
                        )
            except _BLOB_ERRORS as exc:
                errors.append(f"{blob_path}: read/decrypt failed: {exc}")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            continue
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
            plaintext = crypto.decrypt(raw)
        except _BLOB_ERRORS as exc:
            errors.append(f"{blob_path}: read/decrypt failed: {exc}")
            continue

        if entry.type == "full":
            got = hashlib.sha256(plaintext).hexdigest()
            if got != entry.result_checksum:
                errors.append(
                    f"{blob_path}: checksum mismatch\n"
                    f"    expected: {entry.result_checksum[:16]}...\n"
                    f"    got:      {got[:16]}..."
                )

def _fold_volume_split_chunk(
    entry: ManifestEntry,
    blob_path: str,
    tmp_plain: Path,
    split_progress: dict[str, tuple | None],
    errors: list[str],
) -> None:
    """
    Input:  entry, blob_path - the volume-split chunk just decrypted
            tmp_plain        - decrypted plaintext of this chunk
            split_progress   - running concat state keyed by rel_path
            errors           - list to append checksum mismatches to
    Output: None
    Details:
        Folds this chunk into a running sha256 when it is the next
        expected index. A complete 0..count-1 run compares the digest
        to result_checksum. Gaps (a missing earlier chunk, or a count
        change) poison the path so a later last-chunk does not hash an
        incomplete concatenation.
    """
    count = entry.volume_chunk_count
    idx = entry.volume_chunk
    if count is None or idx is None:
        return
    state = split_progress.get(entry.path)
    if state is None and entry.path in split_progress:
        return
    if state is None:
        if idx != 0:
            split_progress[entry.path] = None
            return
        digest = hashlib.sha256()
        next_idx = 0
        target = entry.result_checksum
    else:
        digest, next_idx, prev_count, target, _last_blob = state
        if prev_count != count:
            split_progress[entry.path] = None
            return
    if idx != next_idx:
        split_progress[entry.path] = None
        return
    with tmp_plain.open("rb") as fh:
        while True:
            chunk = fh.read(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    next_idx += 1
    split_progress[entry.path] = (digest, next_idx, count, target, blob_path)
    if next_idx != count:
        return
    got = digest.hexdigest()
    if got != target:
        errors.append(
            f"{blob_path}: checksum mismatch\n"
            f"    expected: {target[:16]}...\n"
            f"    got:      {got[:16]}..."
        )


def _check_full(
    manifests: list[Manifest],
    crypto: "CryptoBackend",
    capture_dir: Path,
    tmp_path: Path,
) -> int:
    """
    Input:  manifests   - ascending-session Manifest list already loaded
                          and, for every session that reached this check,
                          checksum-verified by _check_blobs this run
            crypto      - CryptoBackend for decryption
            capture_dir - raw encrypted blobs _check_blobs already wrote
                          for every entry it read; reused here instead of
                          a second disc read
            tmp_path    - temp directory for the throwaway restore dest
    Output: number of reconstruction failures (0 on a clean archive)
    Details:
        Replaces the old restore()-as-black-box second full disc pass
        (23c). The code reconstructs every file's latest state
        directly from the blobs _check_blobs already captured this
        run, through the same session-ordered replay engine that
        restore() uses (restore.run_session_replay), fed by
        _CaptureSource wrapping capture_dir instead of a live backend.
        This makes zero additional
        backend.read_path/read_paths/extract_path_to_file calls. A
        session _check_blobs never reached (its own fast check already
        failed, so checksum-level blob checks were skipped for it) has
        no captured blobs. Any file whose chain needs one of its
        entries fails to reconstruct here, consistent with that
        session already being counted as an error.
    """
    from oddarchiver.restore import _build_chains, run_session_replay

    dest = tmp_path / "full_restore"
    dest.mkdir()
    file_chains, deleted_at = _build_chains(manifests)
    max_session = max((m.session for m in manifests), default=-1)
    source = _CaptureSource(capture_dir)
    _, failures, *_rest = run_session_replay(
        dest, source, crypto, file_chains, deleted_at, max_session,
        force=False, paths=None, prior_files={}, allow_unresolved=False,
        recover=False,
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
