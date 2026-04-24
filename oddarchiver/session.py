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
) -> Path:
    """
    Input:  session_n  — session number to build (e.g. 1 for first sync)
            source     — directory being archived
            disc_state — {path: result_checksum} from manifest.build_disc_state()
            backend    — BurnBackend for disc reads on cache miss
            cache      — CacheManager for encrypted blob retrieval
            crypto     — CryptoBackend for encrypt/decrypt
    Output: Path — path to completed staging directory (caller owns cleanup)
    Details:
        Uses tempfile.mkdtemp; cleans up in except (runs on Ctrl+C via SIGINT flag).
        Installs SIGINT handler at entry.
        Raises SystemExit(1) if space check fails.
        On BaseException the staging dir is removed before re-raising.
    """
    global _sigint_received
    _sigint_received = False
    signal.signal(signal.SIGINT, _handle_sigint)

    staging = Path(tempfile.mkdtemp(prefix="oddarchiver_"))
    try:
        session_name = f"session_{session_n:03d}"
        session_dir = staging / session_name
        full_dir = session_dir / "full"
        deltas_dir = session_dir / "deltas"
        full_dir.mkdir(parents=True, exist_ok=True)
        deltas_dir.mkdir(parents=True, exist_ok=True)

        # Step 3: scan source
        current_state: dict[str, str] = {}
        for src_file in sorted(source.rglob("*")):
            if src_file.is_file():
                rel = str(src_file.relative_to(source))
                current_state[rel] = _sha256_file(src_file)

        # Step 4: diff
        changed = {
            p: cs for p, cs in current_state.items()
            if p in disc_state and cs != disc_state[p]
        }
        new_files = {p for p in current_state if p not in disc_state}
        deleted = [p for p in disc_state if p not in current_state]

        entries: list[ManifestEntry] = []
        base_session = session_n - 1

        # Step 5: stage changed files
        for rel_path, new_checksum in changed.items():
            abs_path = source / rel_path
            encrypted_old = cache.get_with_fallback(rel_path, base_session, backend)
            old_bytes = crypto.decrypt(encrypted_old)

            kind, blob = delta_or_full(old_bytes, abs_path)
            encrypted_blob = crypto.encrypt(blob)

            if kind == "delta":
                dest = deltas_dir / (rel_path + ".xdelta")
            else:
                dest = full_dir / rel_path

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(encrypted_blob)

            entries.append(ManifestEntry(
                path=rel_path,
                type=kind,
                result_checksum=new_checksum,
                full_size_bytes=abs_path.stat().st_size,
                source_checksum=disc_state.get(rel_path, ""),
                delta_file=f"{session_name}/deltas/{rel_path}.xdelta" if kind == "delta" else "",
                delta_size_bytes=len(blob) if kind == "delta" else 0,
                file=f"{session_name}/full/{rel_path}" if kind == "full" else "",
            ))

            _log.info("staged %s as %s", rel_path, kind)

        # Step 6: stage new files
        for rel_path in sorted(new_files):
            abs_path = source / rel_path
            file_bytes = abs_path.read_bytes()
            encrypted_blob = crypto.encrypt(file_bytes)

            dest = full_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(encrypted_blob)

            entries.append(ManifestEntry(
                path=rel_path,
                type="full",
                result_checksum=_sha256_file(abs_path),
                full_size_bytes=abs_path.stat().st_size,
                file=f"{session_name}/full/{rel_path}",
            ))

            _log.info("staged new file %s", rel_path)

        if _sigint_received:
            raise KeyboardInterrupt

        # Space check
        used = _staging_bytes(staging)
        disc_info = backend.mediainfo()
        limit = disc_info.remaining_bytes * SPACE_SAFETY_MARGIN
        if used >= limit:
            _log.error(
                "Space check failed: staging %d bytes >= remaining %d * %.2f = %d",
                used, disc_info.remaining_bytes, SPACE_SAFETY_MARGIN, int(limit),
            )
            raise SystemExit(1)

        _log.info(
            "Space check OK: staging %d bytes, remaining %d bytes",
            used, disc_info.remaining_bytes,
        )

        # Step 7: write manifest
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
        write_manifest(session_dir, manifest)

        _log.info(
            "Staged %s: %d new, %d changed, %d deleted",
            session_name, len(new_files), len(changed), len(deleted),
        )
        return staging

    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


if __name__ == "__main__":
    pass
