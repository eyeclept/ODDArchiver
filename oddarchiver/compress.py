"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Per-blob LZMA compression for archive writes and reads.
"""
# Imports
from __future__ import annotations

import io
import logging
import lzma
import os
import shutil
from pathlib import Path


# Globals
_PROBE_BYTES = 4 * 1024 * 1024
# Below this size a full LZMA pass is cheap, and skipping the probe keeps
# the encode path to one read of the source.
PROBE_MIN = 1024 * 1024
CODEC_NONE = "none"
CODEC_LZMA = "lzma"
_RATIO_LIMIT = 0.95
_LZMA_PRESET = 6
_CHUNK_SIZE = 1024 * 1024
# Codec for new and changed blobs. callers may set CODEC_NONE to store
# raw bytes, which keeps packing math stable for tests and for users
# who prefer uncompressed discs.
DEFAULT_CODEC = CODEC_LZMA
logger = logging.getLogger(__name__)
_log = logger


# Functions


def _write_xz_from(inf, dest: Path, digest: object | None) -> None:
    """
    Input:  inf - binary stream of plaintext
            dest - xz output path
            digest - optional object with update(bytes)
    Output: None
    """
    with lzma.open(
        dest, "wb", format=lzma.FORMAT_XZ, preset=_LZMA_PRESET,
    ) as outf:
        while True:
            chunk = inf.read(_CHUNK_SIZE)
            if not chunk:
                break
            if digest is not None:
                digest.update(chunk)
            outf.write(chunk)


def _looks_incompressible(data: bytes) -> bool:
    """
    Input:  data - first bytes of a payload, at most _PROBE_BYTES
    Output: True when LZMA does not shrink the sample
    Details:
        A full xz pass on 3 GiB of weights costs about 10 minutes and
        is thrown away by the ratio rule. Random and already-compressed
        data expands under xz, so a sample settles it in a fraction of
        a second.
    """
    if not data:
        return True
    probe = lzma.compress(data, format=lzma.FORMAT_XZ, preset=_LZMA_PRESET)
    return len(probe) >= 0.99 * len(data)


def probe_file_incompressible(src: Path) -> bool:
    """
    Input:  src - plaintext file
    Output: True when the first sample does not shrink under LZMA
    """
    with Path(src).open("rb") as inf:
        data = inf.read(_PROBE_BYTES)
    return _looks_incompressible(data)


def _copy_raw(src: Path, dest: Path, digest: object | None) -> None:
    """Input: source, dest, optional digest. Output: dest holds a raw copy."""
    with Path(src).open("rb") as inf, Path(dest).open("wb") as outf:
        while True:
            chunk = inf.read(_CHUNK_SIZE)
            if not chunk:
                break
            if digest is not None:
                digest.update(chunk)
            outf.write(chunk)


def _commit_xz(
    tmp: Path, dest: Path, raw_size: int, raw_copy,
) -> tuple[str, int]:
    """
    Input:  tmp - xz temp file; dest - output; raw_size - plaintext size
            raw_copy - callable that writes raw bytes to dest
    Output: (codec, encoded_size)
    """
    encoded_size = tmp.stat().st_size
    if encoded_size >= _RATIO_LIMIT * raw_size:
        tmp.unlink(missing_ok=True)
        raw_copy()
        return CODEC_NONE, raw_size
    os.replace(tmp, dest)
    return CODEC_LZMA, encoded_size


def compress_file(
    src: Path, dest: Path, digest: object | None = None,
) -> tuple[str, int]:
    """
    Input:  src - plaintext file
            dest - encoded output path
            digest - optional object with update(bytes) for src bytes
    Output: (codec, encoded_size)
    Details:
        Writes FORMAT_XZ preset 6 in chunks. When encoded_size is at
        least 0.95 of raw size, dest receives the raw bytes instead.
    """
    src = Path(src)
    dest = Path(dest)
    raw_size = src.stat().st_size
    if DEFAULT_CODEC == CODEC_NONE or (
        raw_size >= PROBE_MIN and probe_file_incompressible(src)
    ):
        _copy_raw(src, dest, digest)
        return CODEC_NONE, raw_size
    tmp = dest.with_name(dest.name + ".lzma.tmp")
    try:
        with src.open("rb") as inf:
            _write_xz_from(inf, tmp, digest)
        return _commit_xz(
            tmp, dest, raw_size, lambda: shutil.copyfile(src, dest),
        )
    except Exception:
        logger.exception("failed to compress %s", src)
        tmp.unlink(missing_ok=True)
        raise


def compress_bytes(
    data: bytes, dest: Path, digest: object | None = None,
) -> tuple[str, int]:
    """
    Input:  data - plaintext bytes
            dest - encoded output path
            digest - optional object with update(bytes) for data
    Output: (codec, encoded_size)
    Details:
        Same ratio rule as compress_file. Does not write plaintext to
        a scratch file.
    """
    dest = Path(dest)
    if DEFAULT_CODEC == CODEC_NONE or (
        len(data) >= PROBE_MIN and _looks_incompressible(data[:_PROBE_BYTES])
    ):
        dest.write_bytes(data)
        if digest is not None:
            digest.update(data)
        return CODEC_NONE, len(data)
    tmp = dest.with_name(dest.name + ".lzma.tmp")
    try:
        _write_xz_from(io.BytesIO(data), tmp, digest)
        return _commit_xz(
            tmp, dest, len(data), lambda: dest.write_bytes(data),
        )
    except Exception:
        logger.exception("failed to compress in-memory blob")
        tmp.unlink(missing_ok=True)
        raise


def encode_bytes(data: bytes) -> tuple[str, bytes]:
    """
    Input:  data - plaintext bytes
    Output: (codec, payload bytes)
    Details:
        Same ratio rule as compress_file, kept in memory because callers
        hold the plaintext already. Storing the old inline ciphertext
        format keeps small blobs compatible with earlier archives.
    """
    if DEFAULT_CODEC == CODEC_NONE:
        return CODEC_NONE, data
    encoded = lzma.compress(data, format=lzma.FORMAT_XZ, preset=_LZMA_PRESET)
    if len(encoded) >= _RATIO_LIMIT * len(data):
        return CODEC_NONE, data
    return CODEC_LZMA, encoded


def decompress_file(src: Path, dest: Path) -> None:
    """
    Input:  src - xz file; dest - plaintext output path
    Output: None
    """
    src = Path(src)
    dest = Path(dest)
    try:
        with lzma.open(src, "rb", format=lzma.FORMAT_XZ) as inf, dest.open("wb") as outf:
            while True:
                chunk = inf.read(_CHUNK_SIZE)
                if not chunk:
                    break
                outf.write(chunk)
    except Exception:
        logger.exception("failed to decompress %s", src)
        raise


def decompress_bytes(data: bytes) -> bytes:
    """
    Input:  data - xz bytes
    Output: plaintext bytes
    """
    try:
        return lzma.decompress(data, format=lzma.FORMAT_XZ)
    except Exception:
        logger.exception("failed to decompress bytes")
        raise


def decode_payload(data: bytes, codec: str = CODEC_NONE) -> bytes:
    """
    Input:  data - stored payload after decrypt; codec - manifest codec
    Output: plaintext bytes
    """
    if codec == CODEC_LZMA:
        return decompress_bytes(data)
    return data


def decode_file(src: Path, dest: Path, codec: str = CODEC_NONE) -> None:
    """
    Input:  src - stored payload file after decrypt
            dest - plaintext path
            codec - manifest codec
    Output: None
    """
    if codec == CODEC_LZMA:
        decompress_file(src, dest)
        return
    if Path(src) != Path(dest):
        shutil.copyfile(src, dest)


if __name__ == "__main__":
    pass
