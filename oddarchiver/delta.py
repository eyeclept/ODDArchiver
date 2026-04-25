"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    xdelta3 wrapper for binary delta computation and application.
    Parallelizes across files via ThreadPoolExecutor (I/O bound).
"""
# Imports
from __future__ import annotations

import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Globals
DELTA_THRESHOLD = 0.90  # store full if delta > 90% of full size
_log = logging.getLogger(__name__)

# Functions


def _memfd_with_bytes(name: str, data: bytes) -> int:
    """Create an anonymous in-memory file, write data, seek back to 0.

    Returns the file descriptor. Caller is responsible for os.close(fd).
    Uses os.memfd_create so no plaintext bytes ever touch disk.
    """
    fd = os.memfd_create(name, 0)
    os.write(fd, data)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def compute_delta(old_bytes: bytes, new_path: Path) -> bytes:
    """
    Input:  old_bytes — plaintext content of the previous version
            new_path  — filesystem path to the new version
    Output: bytes — raw xdelta3 delta
    Details:
        Feeds old_bytes to xdelta3 via an anonymous memfd (/proc/self/fd/N)
        so no plaintext temp file is ever created on disk.
    """
    fd = _memfd_with_bytes("oddarchiver_delta_src", old_bytes)
    try:
        proc = subprocess.Popen(
            ["xdelta3", "-e", "-c", "-s", f"/proc/self/fd/{fd}", str(new_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(fd,),
        )
        delta, err = proc.communicate()
    finally:
        os.close(fd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"xdelta3 encode failed (exit {proc.returncode}): {err.decode()}"
        )
    return delta


def apply_delta(base_bytes: bytes, delta_bytes: bytes) -> bytes:
    """
    Input:  base_bytes  — plaintext content of the base version
            delta_bytes — xdelta3 delta to apply
    Output: bytes — reconstructed file content
    Details:
        Feeds base_bytes via an anonymous memfd; delta_bytes arrive via
        stdin. No plaintext temp file is created on disk.
    """
    fd = _memfd_with_bytes("oddarchiver_delta_base", base_bytes)
    try:
        proc = subprocess.Popen(
            ["xdelta3", "-d", "-c", "-s", f"/proc/self/fd/{fd}", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(fd,),
        )
        result, err = proc.communicate(input=delta_bytes)
    finally:
        os.close(fd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"xdelta3 decode failed (exit {proc.returncode}): {err.decode()}"
        )
    return result


def delta_or_full(old_bytes: bytes, new_path: Path) -> tuple[str, bytes]:
    """
    Input:  old_bytes — previous plaintext version
            new_path  — path to new version
    Output: tuple ("delta", delta_bytes) or ("full", full_bytes)
    Details:
        Computes delta; if len(delta) > DELTA_THRESHOLD * full_size
        returns full bytes instead. Logs the per-file decision.
    """
    full_bytes = new_path.read_bytes()
    delta_bytes = compute_delta(old_bytes, new_path)
    full_kb = len(full_bytes) // 1024
    delta_kb = len(delta_bytes) // 1024

    if len(full_bytes) > 0 and len(delta_bytes) > DELTA_THRESHOLD * len(full_bytes):
        _log.info(
            "%s: delta %dKB vs full %dKB -- storing full",
            new_path.name,
            delta_kb,
            full_kb,
        )
        return ("full", full_bytes)

    _log.info(
        "%s: delta %dKB vs full %dKB -- storing delta",
        new_path.name,
        delta_kb,
        full_kb,
    )
    return ("delta", delta_bytes)


def process_files(
    jobs: list[tuple[bytes, Path]],
    max_workers: int = 4,
) -> list[tuple[str, bytes]]:
    """
    Input:  jobs        — list of (old_bytes, new_path) pairs
            max_workers — thread pool size
    Output: list of ("delta"/"full", bytes) in same order as jobs
    Details:
        Runs delta_or_full for each job concurrently via ThreadPoolExecutor.
        Results are returned in input order regardless of completion order.
    """
    results: list[tuple[str, bytes] | None] = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(delta_or_full, old, path): idx
            for idx, (old, path) in enumerate(jobs)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()
    return results  # type: ignore[return-value]


if __name__ == "__main__":
    pass
