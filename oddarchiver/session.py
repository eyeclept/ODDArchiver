"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Staging directory construction. Orchestrates scan, diff, delta/full
    staging, space check, and SIGINT-safe cleanup.
"""
# Imports
from __future__ import annotations

import datetime
import hashlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.delta import delta_or_full
from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.cache import CacheManager
    from oddarchiver.crypto import CryptoBackend

# Globals
SPACE_SAFETY_MARGIN = 0.95
_log = logging.getLogger(__name__)

# Functions


def _default_staging_root() -> Path:
    """Per-user staging root with 0o700 permissions.

    Prefers $XDG_RUNTIME_DIR/oddarchiver (tmpfs, cleaned on logout).
    Falls back to ~/.local/state/oddarchiver/staging.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        base = Path(xdg) / "oddarchiver"
    else:
        base = Path.home() / ".local" / "state" / "oddarchiver" / "staging"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        base.chmod(0o700)
    except OSError as exc:
        _log.warning("Could not tighten staging root mode on %s: %s", base, exc)
    return base


def _blob_id(session_n: int, rel_path: str) -> str:
    """
    Input:  session_n - session index
            rel_path  - relative source path
    Output: 64-char hex string used as the on-disc blob filename
    Details:
        sha256 of "session_n:rel_path". Deterministic and opaque.
        Stored flat in full/ or deltas/ with no extension so the disc
        directory tree does not show original file names or layout.
    """
    import hashlib
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _print_bar(current: int, total: int, suffix: str = "", bar_width: int = 35) -> None:
    """
    Input:  current   - items completed
            total     - total items
            suffix    - short label appended after the counter (truncated to 40 chars)
            bar_width - number of characters in the bar itself
    Output: None (writes to stdout, no newline. The caller prints a newline when done)
    Details:
        Uses carriage-return overwrite so successive calls update in-place.
    """
    pct = current / total if total > 0 else 1.0
    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)
    label = suffix[:40].ljust(40)
    print(f"\r  [{bar}] {current}/{total}  {label}", end="", flush=True)


_sigint_received = False


def _handle_sigint(_signum: int, _frame: object) -> None:
    """
    Input:  signum, frame - standard signal handler args
    Output: None
    Details:
        Sets a flag. Loops check the flag. Cleanup is not done here.
    """
    global _sigint_received
    _sigint_received = True


def _sha256_file(path: Path) -> str:
    """
    Input:  path - file to hash
    Output: hex sha256 digest of file contents
    Details:
        Reads entire file into memory.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _staging_bytes(staging: Path) -> int:
    """
    Input:  staging - directory to measure
    Output: total bytes used on disk (as reported by du -sb)
    Details:
        Uses du -sb for accurate disk usage including directory overhead.
    """
    proc = subprocess.run(
        ["du", "-sb", str(staging)],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.split()[0])


def _check_staging_lock(staging: Path, session_n: int) -> None:
    """
    Input:  staging    - deterministic staging directory that already exists
            session_n  - session number, used only for the error message
    Output: None
    Details:
        Guards against the exact collision that caused a real disc corruption:
        two processes racing to (re)build the same deterministically-named
        staging directory, one silently deleting the other's in-flight files
        mid-burn. Reads a PID from staging/.lock (written by build_staging on
        every successful start) and probes it with os.kill(pid, 0):
          - No lockfile, or unreadable/non-integer contents: treated as a
            leftover from an old oddarchiver version or a crash before the
            lock was written. Stale; caller may remove it.
          - Lockfile names a PID that no longer exists (ProcessLookupError):
            stale; caller may remove it.
          - Lockfile names a PID that still exists (kill succeeds, or raises
            PermissionError because it belongs to another user): that build
            is genuinely in flight. Raises RuntimeError instead of letting
            the caller delete live data out from under it.
    """
    lock_path = staging / ".lock"
    try:
        pid = int(lock_path.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except OSError as exc:
        _log.warning("Could not probe staging lock pid %d for %s: %s", pid, staging, exc)
    raise RuntimeError(
        f"Staging directory for session {session_n:03d} ({staging}) is in use "
        f"by pid {pid}; refusing to remove it. If that process is no longer "
        f"running, remove {staging} manually and retry."
    )


def build_staging(
    session_n: int,
    source: Path,
    disc_state: dict[str, str],
    backend: "BurnBackend",
    cache: "CacheManager",
    crypto: "CryptoBackend",
    _staging_root: Path | None = None,
    _current_state: dict[str, str] | None = None,
) -> Path:
    """
    Input:  session_n      - session number to build (e.g. 1 for first sync)
            source         - directory being archived
            disc_state     - {path: result_checksum} from manifest.build_disc_state()
            backend        - BurnBackend for disc reads on cache miss
            cache          - CacheManager for encrypted blob retrieval
            crypto         - CryptoBackend for encrypt/decrypt
            _staging_root  - override staging root (default: _default_staging_root)
            _current_state - pre-computed {path: sha256} to skip re-scanning source
    Output: Path - path to completed staging directory (caller owns cleanup)
    Details:
        Uses a deterministic named staging dir so a crash-left dir can be detected
        and removed on the next run. Before removing an existing dir, checks a
        PID lockfile via _check_staging_lock(): if the PID that created it is
        still alive, raises RuntimeError instead of deleting live data out from
        under another in-flight build (see Assist/HANDOVER_disc_sync_bug.md).
        Otherwise the stale dir is logged and removed before a fresh one is
        created, and a lockfile naming this process is written immediately.
        The source scan excludes the staging tree itself (including that
        lockfile) if staging happens to be nested inside source -- e.g. the
        home-directory fallback staging root when syncing a source tree that
        contains the user's home directory -- so staging bookkeeping is never
        mistaken for archive content.
        Installs a SIGINT handler at entry. The handler only sets a flag.
        Raises SystemExit(1) if space check fails.
        On BaseException the staging dir is removed before re-raising.
    """
    global _sigint_received
    _sigint_received = False
    old_sigint = signal.signal(signal.SIGINT, _handle_sigint)

    root = _staging_root if _staging_root is not None else _default_staging_root()
    staging = root / f"oddarchiver_staging_{session_n:03d}"
    if staging.exists():
        _check_staging_lock(staging, session_n)
        _log.warning(
            "Stale staging dir found for session %03d; removing and rebuilding.", session_n
        )
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / ".lock").write_text(str(os.getpid()))
    try:
        session_name = f"session_{session_n:03d}"
        session_dir = staging / session_name
        full_dir = session_dir / "full"
        deltas_dir = session_dir / "deltas"
        full_dir.mkdir(parents=True, exist_ok=True)
        deltas_dir.mkdir(parents=True, exist_ok=True)

        # Scan source (skipped if caller pre-computed current_state)
        if _current_state is not None:
            current_state = _current_state
        else:
            print(f"Scanning {source} ...", flush=True)
            source_files = sorted(
                f for f in source.rglob("*")
                if f.is_file() and not f.is_relative_to(staging)
            )
            total_files = len(source_files)
            print(f"  {total_files} file(s) found. Hashing...", flush=True)
            current_state = {}
            for i, src_file in enumerate(source_files, 1):
                if _sigint_received:
                    raise KeyboardInterrupt
                rel = str(src_file.relative_to(source))
                current_state[rel] = _sha256_file(src_file)
                _print_bar(i, total_files, suffix=rel)
            if total_files:
                print()

        # Diff current source hashes against disc_state
        changed = {
            p: cs for p, cs in current_state.items()
            if p in disc_state and cs != disc_state[p]
        }
        new_files = {p for p in current_state if p not in disc_state}
        deleted = [p for p in disc_state if p not in current_state]

        if _current_state is None:
            # Only reprint the summary when the caller has not already shown it.
            unchanged = len(current_state) - len(new_files) - len(changed)
            print(
                f"  {len(new_files)} new, {len(changed)} changed, "
                f"{len(deleted)} deleted, {unchanged} unchanged",
                flush=True,
            )

        # Staging-filesystem space check, separate from the disc/ISO capacity
        # check further below. Staging commonly lives on a small, RAM-backed
        # tmpfs (see _default_staging_root / XDG_RUNTIME_DIR), which can run
        # out long before the real disk or the target disc/ISO does. Uses a
        # worst-case estimate (source size for every new or changed file,
        # since a changed file's actual delta-vs-full size is not known until
        # it is diffed) so this fails fast, before any file is hashed for a
        # blob or staged, instead of dying mid-job after hours of work.
        estimated_bytes = sum(
            (source / p).stat().st_size for p in list(new_files) + list(changed)
        )
        if estimated_bytes:
            staging_free = shutil.disk_usage(staging).free
            staging_limit = staging_free * SPACE_SAFETY_MARGIN
            if estimated_bytes >= staging_limit:
                _log.error(
                    "Staging space check failed: need ~%d bytes for %d new/changed "
                    "file(s), staging filesystem at %s has %d bytes free * %.2f = %d. "
                    "The staging area is separate from the target disc/ISO capacity; "
                    "it commonly lives on a size-limited tmpfs. Free up space there, "
                    "or point XDG_RUNTIME_DIR at a larger filesystem, and retry.",
                    estimated_bytes, len(new_files) + len(changed), staging,
                    staging_free, SPACE_SAFETY_MARGIN, int(staging_limit),
                )
                raise SystemExit(1)

        entries: list[ManifestEntry] = []
        base_session = session_n - 1

        # Stage changed files as delta or full
        if changed:
            print(f"Staging {len(changed)} changed file(s)...", flush=True)
        for idx, (rel_path, new_checksum) in enumerate(changed.items(), 1):
            if _sigint_received:
                raise KeyboardInterrupt
            abs_path = source / rel_path
            encrypted_old = cache.get_with_fallback(rel_path, base_session, backend, crypto=crypto)
            old_bytes = crypto.decrypt(encrypted_old)

            kind, blob = delta_or_full(old_bytes, abs_path)
            encrypted_blob = crypto.encrypt(blob)

            blob_id = _blob_id(session_n, rel_path)
            if kind == "delta":
                dest = deltas_dir / blob_id
            else:
                dest = full_dir / blob_id

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(encrypted_blob)

            entries.append(ManifestEntry(
                path=rel_path,
                type=kind,
                result_checksum=new_checksum,
                full_size_bytes=abs_path.stat().st_size,
                source_checksum=disc_state.get(rel_path, ""),
                delta_file=f"{session_name}/deltas/{blob_id}" if kind == "delta" else "",
                delta_size_bytes=len(blob) if kind == "delta" else 0,
                file=f"{session_name}/full/{blob_id}" if kind == "full" else "",
            ))

            _print_bar(idx, len(changed), suffix=f"[{kind}] {rel_path}")
            _log.info("staged %s as %s", rel_path, kind)
        if changed:
            print()

        # Stage new files as full blobs
        if new_files:
            print(f"Staging {len(new_files)} new file(s)...", flush=True)
        for idx, rel_path in enumerate(sorted(new_files), 1):
            if _sigint_received:
                raise KeyboardInterrupt
            abs_path = source / rel_path
            file_bytes = abs_path.read_bytes()
            encrypted_blob = crypto.encrypt(file_bytes)

            blob_id = _blob_id(session_n, rel_path)
            dest = full_dir / blob_id
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(encrypted_blob)

            entries.append(ManifestEntry(
                path=rel_path,
                type="full",
                result_checksum=_sha256_file(abs_path),
                full_size_bytes=abs_path.stat().st_size,
                file=f"{session_name}/full/{blob_id}",
            ))

            _print_bar(idx, len(new_files), suffix=rel_path)
            _log.info("staged new file %s", rel_path)
        if new_files:
            print()

        if _sigint_received:
            raise KeyboardInterrupt

        # Space check
        print("Checking available disc space...", flush=True)
        used = _staging_bytes(staging)
        disc_info = backend.mediainfo()
        limit = disc_info.remaining_bytes * SPACE_SAFETY_MARGIN
        if used >= limit:
            _log.error(
                "Space check failed: staging %d bytes >= remaining %d * %.2f = %d",
                used, disc_info.remaining_bytes, SPACE_SAFETY_MARGIN, int(limit),
            )
            raise SystemExit(1)

        pct = 100.0 * used / disc_info.remaining_bytes if disc_info.remaining_bytes else 0
        print(f"  OK — session is {pct:.1f}% of remaining space.", flush=True)
        _log.info(
            "Space check OK: staging %d bytes, remaining %d bytes",
            used, disc_info.remaining_bytes,
        )

        # Write the session manifest
        print("Writing session manifest...", flush=True)
        manifest = Manifest(
            version=1,
            session=session_n,
            timestamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            source=str(source),
            label="",
            based_on_session=base_session if session_n > 0 else None,
            encryption={},
            entries=entries,
            deleted=deleted,
            manifest_checksum="",
        )
        write_manifest(session_dir, manifest)  # plaintext. _patch_manifest encrypts on burn

        _log.info(
            "Staged %s: %d new, %d changed, %d deleted",
            session_name, len(new_files), len(changed), len(deleted),
        )
        return staging

    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        signal.signal(signal.SIGINT, old_sigint)


if __name__ == "__main__":
    pass
