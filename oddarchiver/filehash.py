"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Chunked SHA-256 hashing for files.
"""
# Imports
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

# Globals
logger = logging.getLogger(__name__)
_log = logger

# Functions


def hash_file(path: Path | str, chunk_size: int, digest: Any | None = None) -> Any:
    """
    Input:  path - file to hash
            chunk_size - number of bytes read per chunk
            digest - optional digest to update in place
    Output: digest updated with the file's contents
    """
    if digest is None:
        digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:  # scbc ignore[fixed-chunk-file-hash] Python 3.10 lacks hashlib.file_digest
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest
