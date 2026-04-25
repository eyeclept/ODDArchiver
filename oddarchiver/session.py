"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Staging directory construction — orchestrates scan, diff, delta/full
    staging, space check, and SIGINT-safe cleanup.
"""
# Imports
from __future__ import annotations

import datetime
import hashlib
import logging
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


def _blob_id(session_n: int, rel_path: str) -> str:
    """
    Input:  session_n — session index
            rel_path  — relative source path
    Output: 64-char hex string used as the on-disc blob filename
    Details:
        sha256(session_n:rel_path) — deterministic, opaque, collision-free.
        Stored flat in full/ or deltas/ with no extension so the disc
        directory tree reveals nothing about the original file names or layout.
    """
    import hashlib
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _print_bar(current: int, total: int, suffix: str = "", bar_width: int = 35) -> None:
    """
    Input:  current   — items completed
            total     — total items
            suffix    — short label appended after the counter (truncated to 40 chars)
            bar_width — number of characters in the bar itself
    Output: None (writes to stdout, no newline — caller prints newline when done)
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
    Input:  signum, frame — standard signal handler args
    Output: None
    Details:
        Sets flag; cleanup runs in finally blocks rather than here.
    """
    global _sigint_received
    _sigint_received = True


def _sha256_file(path: Path) -> str:
    """
    Input:  path — file to hash
    Output: hex sha256 digest of file contents
    Details:
        Reads entire file into memory.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _staging_bytes(staging: Path) -> int:
    """
    Input:  staging — directory to measure
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


def build_staging(
    session_n: int,
    source: Path,
    disc_state: dict[str, str],
    backend: "BurnBackend",
    cache: "CacheManager",
    crypto: "CryptoBackend",
    _staging_root: Path | None = None,
) -> Path:
    """
    Input:  session_n     — session number to build (e.g. 1 for first sync)
            source        — directory being archived
            disc_state    — {path: result_checksum} from manifest.build_disc_state()
            backend       — BurnBackend for disc reads on cache miss
            cache         — CacheManager for encrypted blob retrieval
            crypto        — CryptoBackend for encrypt/decrypt
            _staging_root — override temp root (default: system tmpdir); for testing
    Output: Path — path to completed staging directory (caller owns cleanup)
    Details:
        Uses a deterministic named staging dir so a crash-left dir can be detected
        and removed on the next run.  If a stale dir exists it is logged and removed
        before a fresh one is created.
        Installs SIGINT handler at entry.
        Raises SystemExit(1) if space check fails.
        On BaseException the staging dir is removed before re-raising.
    """
    global _sigint_received
    _sigint_received = False
    old_sigint = signal.signal(signal.SIGINT, _handle_sigint)

    root = _staging_root if _staging_root is not None else Path(tempfile.gettempdir())
    staging = root / f"oddarchiver_staging_{session_n:03d}"
    if staging.exists():
        _log.warning(
            "Stale staging dir found for session %03d; removing and rebuilding.", session_n
        )
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        session_name = f"session_{session_n:03d}"
        session_dir = staging / session_name
        full_dir = session_dir / "full"
        deltas_dir = session_dir / "deltas"
        full_dir.mkdir(parents=True, exist_ok=True)
        deltas_dir.mkdir(parents=True, exist_ok=True)

        # Step 3: scan source — collect paths first so we know the total for the bar
        print(f"Scanning {source} ...", flush=True)
        source_files = sorted(f for f in source.rglob("*") if f.is_file())
        total_files = len(source_files)
        print(f"  {total_files} file(s) found. Hashing...", flush=True)
        current_state: dict[str, str] = {}
        for i, src_file in enumerate(source_files, 1):
            if _sigint_received:
                raise KeyboardInterrupt
            rel = str(src_file.relative_to(source))
            current_state[rel] = _sha256_file(src_file)
            _print_bar(i, total_files, suffix=rel)
        if total_files:
            print()  # newline after bar

        # Step 4: diff
        changed = {
            p: cs for p, cs in current_state.items()
            if p in disc_state and cs != disc_state[p]
        }
        new_files = {p for p in current_state if p not in disc_state}
        deleted = [p for p in disc_state if p not in current_state]

        print(
            f"  {len(new_files)} new, {len(changed)} changed, {len(deleted)} deleted",
            flush=True,
        )

        entries: list[ManifestEntry] = []
        base_session = session_n - 1

        # Step 5: stage changed files
        if changed:
            print(f"Staging {len(changed)} changed file(s)...", flush=True)
        for idx, (rel_path, new_checksum) in enumerate(changed.items(), 1):
            if _sigint_received:
                raise KeyboardInterrupt
            abs_path = source / rel_path
            encrypted_old = cache.get_with_fallback(rel_path, base_session, backend)
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

        # Step 6: stage new files
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

        # Step 7: write manifest
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
        write_manifest(session_dir, manifest)  # plaintext; _patch_manifest encrypts on burn

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
