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

import signal
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.cache import CacheManager
    from oddarchiver.crypto import CryptoBackend

# Globals
SPACE_SAFETY_MARGIN = 0.95

# Functions

_sigint_received = False


def _handle_sigint(signum: int, frame: object) -> None:
    """
    Input:  signum, frame — standard signal handler args
    Output: None
    Details:
        Sets flag; cleanup runs in finally blocks rather than here.
    """
    global _sigint_received
    _sigint_received = True


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
    Output: Path — path to completed staging directory
    Details:
        Uses tempfile.mkdtemp; cleans up in finally (runs on Ctrl+C).
        Installs SIGINT handler at entry.
        Raises SystemExit(1) if space check fails.
    """
    signal.signal(signal.SIGINT, _handle_sigint)
    raise NotImplementedError


if __name__ == "__main__":
    pass
