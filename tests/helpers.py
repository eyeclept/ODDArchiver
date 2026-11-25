"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Shared test helpers for archive staging.
"""
# Imports
import argparse
import hashlib
import logging
from pathlib import Path

import pytest

import oddarchiver.session as session_mod
from oddarchiver.crypto import NullCrypto
from oddarchiver.manifest import Manifest, ManifestEntry, read_manifest, write_manifest
from oddarchiver.session import build_staging
from oddarchiver.volume import VolumeBaseline, VolumeMarker, write_volume_baseline

# Globals
logger = logging.getLogger(__name__)
_crypto = NullCrypto()

# Functions


def _sha256(data: bytes) -> str:
    """Input: file bytes. Output: SHA-256 checksum."""
    return hashlib.sha256(data).hexdigest()


def _blob_id(session_n: int, rel_path: str) -> str:
    """Input: session and path. Output: a legacy blob name for this fixture."""
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _make_manifest(
    session: int = 0,
    entries: list[ManifestEntry] | None = None,
    deleted: list[str] | None = None,
) -> Manifest:
    """Input: session, entries, deleted. Output: a minimal valid manifest."""
    return Manifest(
        version=1,
        session=session,
        timestamp="2026-04-19T12:00:00Z",
        source="/home/user/docs",
        label="TEST",
        based_on_session=None if session == 0 else session - 1,
        encryption={"mode": "none"},
        entries=entries or [],
        deleted=deleted or [],
        manifest_checksum="",
    )


def _make_entry(path: str, checksum: str = "abc123") -> ManifestEntry:
    """Input: path and checksum. Output: a minimal full manifest entry."""
    return ManifestEntry(
        path=path,
        type="full",
        result_checksum=checksum,
        full_size_bytes=100,
    )


def _entry(
    tmp_path: Path, session: int, payload: bytes, *, streamed=False,
    chunk=None, count=None,
) -> ManifestEntry:
    """Input: blob content and metadata. Output: a full manifest entry."""
    blob_name = hashlib.sha256(f"{session}:file.bin".encode()).hexdigest()
    path = f"session_{session:03d}/full/{blob_name}"
    blob = tmp_path / "disc" / path
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(payload)
    return ManifestEntry(
        path="file.bin", type="full", file=path, streamed=streamed,
        result_checksum=hashlib.sha256(payload).hexdigest(),
        full_size_bytes=len(payload), volume_chunk=chunk,
        volume_chunk_count=count,
    )


def _make_staging(
    tmp_path: Path,
    session_n: int,
    files: dict[str, bytes],
    deleted: list[str] | None = None,
    based_on: int | None = None,
    timestamp: str = "2026-04-23T00:00:00Z",
) -> Path:
    """
    Input:  tmp_path, session_n, files - base dir, session index, file contents
            deleted, based_on, timestamp - optional manifest fields
    Output: staging Path ready for ISOBackend.init / append
    Details:
        Write NullCrypto blobs with legacy names and a valid manifest.
    """
    session_name = f"session_{session_n:03d}"
    staging = tmp_path / f"staging_{session_n}"
    session_dir = staging / session_name
    full_dir = session_dir / "full"
    full_dir.mkdir(parents=True)

    entries = []
    for rel_path, content in files.items():
        blob = _crypto.encrypt(content)
        blob_name = _blob_id(session_n, rel_path)
        dest = full_dir / blob_name
        dest.write_bytes(blob)
        entries.append(ManifestEntry(
            path=rel_path,
            type="full",
            result_checksum=_sha256(content),
            full_size_bytes=len(content),
            file=f"{session_name}/full/{blob_name}",
        ))

    manifest = Manifest(
        version=1,
        session=session_n,
        timestamp=timestamp,
        source="/src",
        label="TEST",
        based_on_session=based_on,
        encryption={},
        entries=entries,

        deleted=deleted or [],
        manifest_checksum="",
    )
    write_manifest(session_dir, manifest)
    return staging

def _assert_build_staging_interrupted(
    source: Path,
    backend,
    cache,
    staging_root: Path,
    *,
    session_n: int = 0,
) -> None:
    """
    Input: source, backend, cache, staging root, and session number
    Output: None
    Details: Assert that an interrupted staging build propagates KeyboardInterrupt.
    """
    with pytest.raises(KeyboardInterrupt):
        build_staging(
            session_n=session_n,
            source=source,
            disc_state={},
            disc_origin={},
            backend=backend,
            cache=cache,
            crypto=NullCrypto(),
            _staging_root=staging_root,
        )


def _assert_staging_sigint(
    session_n: int,
    source: Path,
    backend,
    cache,
    staging_root: Path,
    hash_then_signal,
    real_hash,
) -> None:
    """
    Input:  session setup, replacement hash function, original hash function
    Output: None
    Details:
        Check that a hash interruption propagates and restore the hash function.
    """
    session_mod.hash_file = hash_then_signal
    try:
        _assert_build_staging_interrupted(
            source, backend, cache, staging_root, session_n=session_n,
        )
    finally:
        session_mod.hash_file = real_hash


def _make_args(**kwargs) -> argparse.Namespace:
    """Input: field overrides. Output: CLI Namespace with test defaults."""
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "TEST",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "50mb",
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
        "mirror": None,
        "recover_scan": False,
        "yubikey": False,
        "yubikey_slot": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _write_prior_baseline(
    staging: Path,
    prior: VolumeBaseline | None,
    crypto: NullCrypto,
    marker: VolumeMarker,
    session_n: int,
) -> None:
    """
    Input: staging, previous baseline, crypto, volume marker, session number
    Output: None
    Details: Write the prior volume baseline and record its checksum.
    """
    if prior is not None:
        write_volume_baseline(staging, prior, crypto, marker)
        manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
        manifest.baseline_checksum = hashlib.sha256(
            (staging / "volume_baseline.json").read_bytes(),
        ).hexdigest()
        write_manifest(staging / f"session_{session_n:03d}", manifest)


def fill(seed: bytes, size: int) -> bytes:
    """
    Input: seed - one or more bytes identifying the content; size - bytes
    Output: deterministic incompressible bytes of the requested size
    Details:
        Compressed archives store repeated bytes far smaller, so tests
        that assert chunk or volume counts must use data that no codec
        shrinks. The hash chain keeps the same bytes across runs.
    """
    out = bytearray()
    block = hashlib.sha256(seed).digest()
    while len(out) < size:
        out += block
        block = hashlib.sha256(block).digest()
    return bytes(out[:size])


def _args(
    command: str, source: Path, iso: Path, *, label: str = "DELTA_WRITE",
    **overrides,
) -> argparse.Namespace:
    """
    Input: command, source, iso, label, and optional CLI values
    Output: CLI namespace for the test ISO
    Details: Use one disc and no encryption.
    """
    options = dict(
        command=command, source=str(source), test_iso=str(iso),
        label=label, encrypt="none", key=None, dry_run=False,
        no_cache=False, disc_size="64mb", prefill=None, force=False,
        slow=False, skip_large=False, embed_software=None,
        yubikey=False, yubikey_slot=None,
    )
    options.update(overrides)
    return argparse.Namespace(**options)
