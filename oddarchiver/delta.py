"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    This module wraps xdelta3. It computes and applies binary deltas.
    process_files() runs xdelta3 subprocess jobs in a ThreadPoolExecutor.
"""
# Imports
from __future__ import annotations

import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Globals
DELTA_THRESHOLD = 0.90  # Store the full file when the delta is more than 90% of the full size.
_log = logging.getLogger(__name__)

# Functions


def _memfd_with_bytes(name: str, data: bytes) -> int:
    """Create an anonymous file in memory. Write the data to it. Move the
    file pointer back to position 0.

    The function returns the file descriptor. The caller must close it with
    os.close(fd). The function uses os.memfd_create, so the plaintext bytes
    never touch the disk.
    """
    fd = os.memfd_create(name, 0)
    os.write(fd, data)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def compute_delta(old_bytes: bytes, new_path: Path) -> bytes:
    """
    Input:  old_bytes - the plain content of the previous version
            new_path  - the file path of the new version
    Output: bytes - the raw xdelta3 delta
    Details:
        The function sends old_bytes to xdelta3 through an anonymous memfd
        (/proc/self/fd/N). The function does not create a plaintext
        temporary file on disk.
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
    Input:  base_bytes  - plaintext content of the base version
            delta_bytes - xdelta3 delta to apply
    Output: bytes - reconstructed file content
    Details:
        The function sends base_bytes through an anonymous memfd.
        delta_bytes arrive on stdin.
        The function does not write a plaintext temp file to disk.
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
    Input:  old_bytes - previous plaintext version
            new_path  - path to new version
    Output: tuple ("delta", delta_bytes) or ("full", full_bytes)
    Details:
        This function computes the delta. It stores the full bytes when the file is
        not empty and the delta length is more than DELTA_THRESHOLD times the full
        size. It logs the choice.
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
    Input:  jobs        - a list of (old_bytes, new_path) pairs.
            max_workers - the size of the thread pool.
    Output: a list of ("delta", bytes) or ("full", bytes) tuples, in the
            same order as jobs.
    Details:
        This function runs delta_or_full for each job in a ThreadPoolExecutor.
        The results stay in input order. The order does not depend on the
        order in which the jobs complete.
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
