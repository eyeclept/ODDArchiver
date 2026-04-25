"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Reconstruct any version of the source directory from disc or ISO.
    Non-destructive by default; --force overwrites unconditionally.
"""
# Imports
from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.delta import apply_delta
from oddarchiver.manifest import ManifestEntry, read_manifest

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals
_log = logging.getLogger(__name__)

_BLOB_ERRORS = (OSError, ValueError, RuntimeError)

# Functions


def restore(
    dest: Path,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    session: int | None = None,
    force: bool = False,
) -> tuple[int, int]:
    """
    Input:  dest    — destination directory for reconstructed files
            backend — BurnBackend to read blobs from
            crypto  — CryptoBackend for decryption
            session — stop replay at this session index (None = latest)
            force   — overwrite existing dest files unconditionally
    Output: (restored_count, failure_count)
    Details:
        Reads manifests from disc in session order up to max_session.
        Builds per-file reconstruction chain (full blobs + deltas).
        Verifies sha256 of each result against result_checksum.
        On checksum mismatch: logs ERROR; does not write corrupt data.
        SUSPECT manifests are skipped with a WARNING.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    max_session = _resolve_max_session(backend, session)
    if max_session < 0:
        _log.warning("No sessions found on disc; nothing to restore.")
        return (0, 0)

    file_chains, deleted_at = _build_chains(_read_manifests(backend, max_session, crypto))

    restored = 0
    failures = 0
    for rel_path, chain in sorted(file_chains.items()):
        outcome = _process_file(rel_path, chain, deleted_at, max_session, dest, backend, crypto, force)
        if outcome == "restored":
            restored += 1
        elif outcome == "failed":
            failures += 1

    _log.info("Restore complete: %d restored, %d failures", restored, failures)
    if failures:
        _log.error("%d file(s) could not be restored", failures)
    return (restored, failures)


def _resolve_max_session(backend: "BurnBackend", session: int | None) -> int:
    """
    Input:  backend — BurnBackend to query
            session — user-requested stop point (None = latest)
    Output: highest session index to read (may be -1 if disc is empty)
    """
    disc_info = backend.mediainfo()
    max_s = disc_info.session_count - 1
    if session is not None:
        max_s = min(session, max_s)
    return max_s


def _build_chains(
    manifests: list,
) -> tuple[dict[str, list[tuple[int, ManifestEntry]]], dict[str, int]]:
    """
    Input:  manifests — list of Manifest objects in ascending session order
    Output: (file_chains, deleted_at)
            file_chains — {path: [(session_n, entry), ...]}
            deleted_at  — {path: last_session_that_deleted_it}
    Details:
        SUSPECT manifests are skipped with a WARNING log.
    """
    file_chains: dict[str, list[tuple[int, ManifestEntry]]] = {}
    deleted_at: dict[str, int] = {}
    for m in manifests:
        if m.suspect:
            _log.warning("Skipping SUSPECT manifest for session %d", m.session)
            continue
        for entry in m.entries:
            file_chains.setdefault(entry.path, []).append((m.session, entry))
        for del_path in m.deleted:
            deleted_at[del_path] = m.session
    return file_chains, deleted_at


def _process_file(
    rel_path: str,
    chain: list[tuple[int, ManifestEntry]],
    deleted_at: dict[str, int],
    max_session: int,
    dest: Path,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    force: bool,
) -> str:
    """
    Input:  rel_path, chain, deleted_at, max_session — file state context
            dest, backend, crypto, force             — restore parameters
    Output: "restored" | "skipped" | "failed"
    Details:
        Handles deletion check, non-destructive skip, reconstruction,
        checksum verification, and writing to dest.
    """
    last_entry_session = chain[-1][0]
    del_session = deleted_at.get(rel_path, max_session + 1)
    if del_session <= max_session and del_session > last_entry_session:
        return "skipped"

    target_checksum = chain[-1][1].result_checksum
    dest_file = dest / rel_path

    if not force and dest_file.exists():
        if hashlib.sha256(dest_file.read_bytes()).hexdigest() == target_checksum:
            _log.info("skip %s: already at target checksum", rel_path)
            return "skipped"

    result, ok = _reconstruct(rel_path, chain, backend, crypto)
    if not ok:
        return "failed"

    if hashlib.sha256(result).hexdigest() != target_checksum:
        _log.error("Checksum mismatch for %s: expected %s", rel_path, target_checksum)
        return "failed"

    dest_file.parent.mkdir(parents=True, exist_ok=True)
    dest_file.write_bytes(result)
    _log.info("restored %s", rel_path)
    return "restored"


def _read_manifests(
    backend: "BurnBackend",
    max_session: int,
    crypto: "CryptoBackend | None" = None,
) -> list:
    """
    Input:  backend     — BurnBackend to read from
            max_session — highest session index to read (inclusive)
            crypto      — CryptoBackend for encrypted manifests (None → plaintext)
    Output: list of Manifest objects in ascending session order
    Details:
        Tries manifest.enc before manifest.json for each session.
        Uses a temp directory so read_manifest() can operate on real files.
    """
    manifests = []
    with tempfile.TemporaryDirectory(prefix="oddarchiver_restore_") as tmp:
        tmp_path = Path(tmp)
        for s in range(max_session + 1):
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
                except OSError:
                    continue
            if data is None:
                _log.error("Cannot read manifest for session %d", s)
                continue
            tmp_file = tmp_path / f"manifest_{s:03d}{suffix}"
            tmp_file.write_bytes(data)
            manifests.append(read_manifest(tmp_file, crypto=crypto))
    return manifests


def _reconstruct(
    rel_path: str,
    chain: list[tuple[int, ManifestEntry]],
    backend: "BurnBackend",
    crypto: "CryptoBackend",
) -> tuple[bytes, bool]:
    """
    Input:  rel_path — relative file path (for log messages)
            chain    — ordered list of (session_n, ManifestEntry)
            backend  — BurnBackend for reading blobs
            crypto   — CryptoBackend for decryption
    Output: (bytes, success_bool)
    Details:
        A "full" entry restarts reconstruction; a "delta" applies an xdelta3 patch.
        Returns (b"", False) on any read, decrypt, or apply failure.
    """
    current = b""
    has_full = False

    for _, entry in chain:
        if entry.type == "full":
            current, has_full = _read_full(rel_path, entry, backend, crypto)
            if not has_full:
                return b"", False
        else:
            if not has_full:
                _log.error("Delta entry for %s with no prior full blob", rel_path)
                return b"", False
            current, ok = _apply_delta_entry(rel_path, entry, current, backend, crypto)
            if not ok:
                return b"", False

    if not has_full:
        _log.error("No full blob found for %s", rel_path)
        return b"", False

    return current, True


def _read_full(
    rel_path: str,
    entry: ManifestEntry,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
) -> tuple[bytes, bool]:
    """
    Input:  rel_path, entry — file identity and manifest entry
            backend, crypto — I/O and decryption
    Output: (plaintext_bytes, success_bool)
    """
    try:
        return crypto.decrypt(backend.read_path(entry.file)), True
    except _BLOB_ERRORS as exc:
        _log.error("Failed to read full blob for %s: %s", rel_path, exc)
        return b"", False


def _apply_delta_entry(
    rel_path: str,
    entry: ManifestEntry,
    current: bytes,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
) -> tuple[bytes, bool]:
    """
    Input:  rel_path, entry — file identity and manifest entry
            current         — bytes to patch
            backend, crypto — I/O and decryption
    Output: (patched_bytes, success_bool)
    """
    try:
        delta_bytes = crypto.decrypt(backend.read_path(entry.delta_file))
        return apply_delta(current, delta_bytes), True
    except _BLOB_ERRORS as exc:
        _log.error("Failed to apply delta for %s: %s", rel_path, exc)
        return b"", False


if __name__ == "__main__":
    pass
