"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Integrity checking at three levels: fast (manifest checksum),
    checksum (raw blob hashes), full (restore + result_checksum comparison).
"""
# Imports
from __future__ import annotations

from typing import Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals
VerifyLevel = Literal["fast", "checksum", "full"]

# Functions


def verify(
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    level: VerifyLevel = "fast",
) -> bool:
    """
    Input:  backend — BurnBackend to read manifests and blobs from
            crypto  — CryptoBackend (required for level="full" only)
            level   — verification depth
    Output: bool — True if all checks passed
    Details:
        fast:     re-read manifest, validate manifest_checksum, check index/timestamp.
        checksum: sha256 every stored blob vs manifest entry (no decryption).
        full:     restore to temp dir, verify all result_checksums.
        A failed session does not invalidate others; per-session status reported.
        Exits 1 (via SystemExit) if any error found.
    """
    raise NotImplementedError


if __name__ == "__main__":
    pass
