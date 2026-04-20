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

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals

# Functions


def restore(
    dest: Path,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    session: int | None = None,
    force: bool = False,
) -> None:
    """
    Input:  dest    — destination directory for reconstructed files
            backend — BurnBackend to read blobs from
            crypto  — CryptoBackend for decryption
            session — stop replay at this session (None = latest)
            force   — overwrite existing dest files unconditionally
    Output: None
    Details:
        Reads all manifests ascending; builds per-file chain (full + deltas).
        Decrypts full blob, applies each delta in order.
        Verifies sha256 of each result against result_checksum.
        On checksum mismatch: logs ERROR; uses last good version unless
        --skip-corrupt (not yet wired in; raises by default).
        Reports files restored and verification failures.
    """
    raise NotImplementedError


if __name__ == "__main__":
    pass
