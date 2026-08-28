"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Integrity checks at three levels: fast (manifest checksum),
    checksum (raw blob hashes), full (restore plus result_checksum comparison).
"""
# Imports
from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from oddarchiver.manifest import Manifest, read_manifest, validate_blob_path

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals
VerifyLevel = Literal["fast", "checksum", "full"]

_log = logging.getLogger(__name__)

_BLOB_ERRORS = (OSError, ValueError, RuntimeError)

# Functions


def verify(
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    level: VerifyLevel = "fast",
) -> bool:
    """
    Input:  backend - BurnBackend to read manifests and blobs from
            crypto  - CryptoBackend (used for encrypted manifests at every level)
            level   - verification depth: "fast", "checksum", or "full"
    Output: bool - True if all checks passed. Raises SystemExit(1) if total_errors != 0.
    Details:
        fast:     load manifests, flag suspect checksums, check session
                  index and timestamp ordering.
        checksum: all fast checks, then decrypt each blob. Hash full entries
                  against result_checksum. Check that delta entries decrypt.
        full:     run checksum checks, then restore to a temp dir. restore()
                  verifies each reconstructed file sha256 against result_checksum.
        A failed session does not stop later sessions. Per-session status is printed.
        Output format matches DesignDoc sample.
        Blob checks are skipped for a session that already failed fast checks.
    """
    total_errors = 0

    with tempfile.TemporaryDirectory(prefix="oddarchiver_verify_") as tmp:
        tmp_path = Path(tmp)
        manifests = _read_all_manifests(backend, tmp_path, crypto=crypto)

        if not manifests:
            print("No sessions found.")
            return True

        max_session = max(manifests)

        for s in sorted(manifests):
            manifest = manifests.get(s)
            if manifest is None:
                _print_session_fail(s, 0, [f"session_{s:03d}/manifest.json: unreadable"])
                total_errors += 1
                continue

            errors: list[str] = []
            _check_fast(manifest, s, manifests, errors)

            if level in ("checksum", "full") and not errors:
                _check_blobs(manifest, backend, crypto, errors)

            file_count = len(manifest.entries)
            if errors:
                _print_session_fail(s, file_count, errors)
                total_errors += len(errors)
            else:
                _print_session_ok(s, file_count)

        if level == "full":
            total_errors += _check_full(backend, crypto, max_session, tmp_path)

    print(
        f"\nResult: {total_errors} error{'s' if total_errors != 1 else ''}"
        f" across {max_session + 1} session{'s' if max_session + 1 != 1 else ''}."
    )
    if total_errors:
        raise SystemExit(1)
    return True


def _read_all_manifests(
    backend: "BurnBackend",
    tmp_path: Path,
    crypto: "CryptoBackend | None" = None,
) -> dict[int, Manifest]:
    """
    Input:  backend  - BurnBackend to read from
            tmp_path - temp directory for manifest files
            crypto   - CryptoBackend for encrypted manifests (None = plaintext)
    Output: dict mapping session index -> Manifest
    Details:
        Scans session_NNN/manifest.* until the first missing session.
        Does not use dvd+rw-mediainfo session count. On BD-R SRM+POW that count is 1.
    """
    manifests: dict[int, Manifest] = {}
    s = 0
    while True:
        data: bytes | None = None
        suffix = ".json"
        for disc_path in (
            f"session_{s:03d}/manifest.enc",
            f"session_{s:03d}/manifest.json",
        ):
            try:
                data = backend.read_path(disc_path)
                suffix = Path(disc_path).suffix
                break
            except (OSError, ValueError):
                continue
        if data is None:
            break
        tmp_file = tmp_path / f"manifest_{s:03d}{suffix}"
        tmp_file.write_bytes(data)
        manifests[s] = read_manifest(tmp_file, crypto=crypto)
        s += 1
    return manifests


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
    if manifest.suspect:
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
) -> None:
    """
    Input:  manifest - Manifest to check
            backend  - BurnBackend for blob reads
            crypto   - CryptoBackend for decryption
            errors   - list to append error strings to
    Output: None (appends to errors)
    Details:
        For "full" entries: decrypt the blob, then compare sha256 to result_checksum.
        For "delta" entries: decrypt the blob to check readability. No hash check.
        Records path-validation failures, read errors, auth failures, and hash mismatches.
    """
    for entry in manifest.entries:
        blob_path = entry.file if entry.type == "full" else entry.delta_file
        try:
            validate_blob_path(blob_path)
        except ValueError as exc:
            errors.append(f"{blob_path}: refused: {exc}")
            continue
        try:
            raw = backend.read_path(blob_path)
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


def _check_full(
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    max_session: int,
    tmp_path: Path,
) -> int:
    """
    Input:  backend, crypto, max_session - restore parameters
            tmp_path                     - temp directory for restore dest
    Output: number of restore() failures (0 on a clean restore)
    Details:
        Restores the latest reconstructed tree to a temp dest.
        max_session is unused. restore() already reports per-file failures.
    """
    from oddarchiver.restore import restore as do_restore

    dest = tmp_path / "full_restore"
    dest.mkdir()
    _, failures = do_restore(dest, backend, crypto)
    return failures


def _print_session_ok(session_idx: int, file_count: int) -> None:
    """Print a single OK session line matching DesignDoc format."""
    noun = "file" if file_count == 1 else "files"
    print(f"Session {session_idx:03d}: {file_count:2d} {noun:<6} -- OK")


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
