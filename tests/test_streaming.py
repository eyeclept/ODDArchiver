"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for streaming encryption and I/O of files above STREAM_CHUNK_SIZE.
"""
# Imports
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

import oddarchiver.crypto as crypto_mod
import oddarchiver.session as session_mod
from oddarchiver.cli import build_parser, dispatch
from oddarchiver.crypto import (
    KeyfileCrypto,
    NullCrypto,
    PassphraseCrypto,
    generate_keyfile,
)
from oddarchiver.disc import DiscInfo, ISOBackend
from oddarchiver.manifest import read_manifest
from oddarchiver.restore import restore
from oddarchiver.verify import verify

# Globals
CHUNK = 64
SMALL_DISC = 50 * 2**20
_LARGE_REMAINING = 25 * 2**30


# Functions


def _patch_chunk(monkeypatch) -> None:
    """Point every STREAM_CHUNK_SIZE binding at the tiny test chunk."""
    monkeypatch.setattr(crypto_mod, "STREAM_CHUNK_SIZE", CHUNK)
    monkeypatch.setattr(session_mod, "STREAM_CHUNK_SIZE", CHUNK)
    import oddarchiver.restore as restore_mod
    import oddarchiver.verify as verify_mod
    monkeypatch.setattr(restore_mod, "STREAM_CHUNK_SIZE", CHUNK)
    monkeypatch.setattr(verify_mod, "STREAM_CHUNK_SIZE", CHUNK)


def _dispatch(argv: list[str]) -> int:
    """Parse argv and dispatch, matching a real CLI invocation."""
    return dispatch(build_parser().parse_args(argv))


def _make_backend(remaining_bytes: int = _LARGE_REMAINING) -> MagicMock:
    """Return a mock BurnBackend whose mediainfo() returns remaining_bytes."""
    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=0,
        remaining_bytes=remaining_bytes,
        used_bytes=0,
        label="TEST",
    )
    backend.disc_size = remaining_bytes
    return backend


def _make_cache(old_bytes: bytes = b"") -> MagicMock:
    """Return a mock CacheManager whose get_with_fallback returns old_bytes."""
    cache = MagicMock()
    cache.get_with_fallback.return_value = old_bytes
    return cache


def _wrap_aead(monkeypatch, chunk: int) -> list[int]:
    """
    Record every plaintext length ChaCha20Poly1305.encrypt sees while
    called from crypto._encrypt_chunks (the streaming file loop) and fail
    the test if any such chunk exceeds `chunk`. Calls from the small-blob
    encrypt(bytes) path (e.g. manifest encryption) are not part of the
    streaming contract and are left unchecked.
    """
    import inspect

    seen: list[int] = []
    real_encrypt = ChaCha20Poly1305.encrypt

    def wrapped(self, nonce, data, associated_data=None):
        caller = inspect.stack()[1].function
        if caller == "_encrypt_chunks":
            seen.append(len(data))
            assert len(data) <= chunk, (
                f"streaming AEAD encrypt saw {len(data)} bytes, chunk is {chunk}"
            )
        return real_encrypt(self, nonce, data, associated_data)

    monkeypatch.setattr(ChaCha20Poly1305, "encrypt", wrapped)
    return seen


def test_init_restore_streamed_file_no_overflow(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        init of a file just over STREAM_CHUNK_SIZE succeeds without
        OverflowError. restore reconstructs it byte-for-byte. AEAD encrypt
        never sees more than one chunk of plaintext.
    """
    _patch_chunk(monkeypatch)
    seen = _wrap_aead(monkeypatch, CHUNK)
    monkeypatch.setenv("ODDARCHIVER_PASSPHRASE", "stream-e2e-pass")

    source = tmp_path / "src"
    source.mkdir()
    payload = b"X" * (CHUNK + 1)
    (source / "big.bin").write_bytes(payload)

    iso = tmp_path / "test.iso"
    rc = _dispatch([
        "init", str(source), "--test-iso", str(iso),
        "--encrypt", "passphrase", "--disc-size", "50mb", "--label", "TEST",
    ])
    assert rc == 0
    assert seen, "encrypt_file must have called ChaCha20Poly1305.encrypt"
    assert max(seen) <= CHUNK

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    crypto = PassphraseCrypto(passphrase="stream-e2e-pass")
    from oddarchiver.manifest import load_session_manifests
    manifests = load_session_manifests(backend, crypto=crypto)
    assert manifests
    entries = [e for e in manifests[0].entries if e.path == "big.bin"]
    assert entries and entries[0].streamed is True
    assert entries[0].type == "full"

    dest = tmp_path / "dest"
    restored, failures = restore(dest, backend, crypto)
    assert failures == 0
    assert restored >= 1
    assert (dest / "big.bin").read_bytes() == payload


def test_init_restore_streamed_keyfile(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Same init+restore path with KeyfileCrypto.
    """
    _patch_chunk(monkeypatch)
    _wrap_aead(monkeypatch, CHUNK)
    key = str(tmp_path / "archive.key")
    generate_keyfile(key)

    source = tmp_path / "src"
    source.mkdir()
    payload = b"K" * (CHUNK * 2 + 3)
    (source / "big.bin").write_bytes(payload)

    iso = tmp_path / "test.iso"
    rc = _dispatch([
        "init", str(source), "--test-iso", str(iso),
        "--encrypt", "keyfile", "--key", key,
        "--disc-size", "50mb", "--label", "TEST",
    ])
    assert rc == 0

    dest = tmp_path / "dest"
    crypto = KeyfileCrypto(key)
    restored, failures = restore(dest, ISOBackend(iso, disc_size=SMALL_DISC), crypto)
    assert failures == 0
    assert restored >= 1
    assert (dest / "big.bin").read_bytes() == payload


def test_shrink_large_then_grow_small(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A large file that shrinks below the threshold re-encodes as a small
        full blob (not a delta). A small file that grows above the threshold
        switches to the streamed full path. Both restore correctly.
    """
    _patch_chunk(monkeypatch)
    monkeypatch.setenv("ODDARCHIVER_PASSPHRASE", "stream-e2e-pass")
    crypto = PassphraseCrypto(passphrase="stream-e2e-pass")

    source = tmp_path / "src"
    source.mkdir()
    (source / "shrink.bin").write_bytes(b"L" * (CHUNK + 8))
    (source / "grow.bin").write_bytes(b"s")

    iso = tmp_path / "test.iso"
    assert _dispatch([
        "init", str(source), "--test-iso", str(iso),
        "--encrypt", "passphrase", "--disc-size", "50mb", "--label", "TEST",
    ]) == 0

    (source / "shrink.bin").write_bytes(b"small-now")
    (source / "grow.bin").write_bytes(b"G" * (CHUNK + 8))
    assert _dispatch([
        "sync", str(source), "--test-iso", str(iso),
        "--disc-size", "50mb",
    ]) == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    # Session 1 entries: shrink must be a non-streamed full; grow streamed full.
    from oddarchiver.manifest import load_session_manifests
    manifests = load_session_manifests(backend, crypto=crypto)
    assert len(manifests) >= 2
    s1 = {e.path: e for e in manifests[1].entries}
    assert s1["shrink.bin"].type == "full"
    assert s1["shrink.bin"].streamed is False
    assert s1["grow.bin"].type == "full"
    assert s1["grow.bin"].streamed is True

    dest = tmp_path / "dest"
    restored, failures = restore(dest, backend, crypto)
    assert failures == 0
    assert restored >= 1
    assert (dest / "shrink.bin").read_bytes() == b"small-now"
    assert (dest / "grow.bin").read_bytes() == b"G" * (CHUNK + 8)


def test_verify_checksum_streamed_entry(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        checksum-level verify passes against a streamed entry and reports a
        checksum-mismatch error (not a crash) when that entry is tampered.
    """
    _patch_chunk(monkeypatch)

    source = tmp_path / "src"
    source.mkdir()
    payload = b"V" * (CHUNK + 5)
    (source / "big.bin").write_bytes(payload)

    iso = tmp_path / "test.iso"
    assert _dispatch([
        "init", str(source), "--test-iso", str(iso),
        "--encrypt", "none", "--disc-size", "50mb", "--label", "TEST",
    ]) == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    crypto = NullCrypto()
    assert verify(backend, crypto, level="checksum") is True

    from oddarchiver.session import _blob_id
    blob_path = backend._sessions_root / "session_000" / "full" / _blob_id(0, "big.bin")
    blob_path.write_bytes(b"CORRUPTED GARBAGE")

    with pytest.raises(SystemExit) as exc:
        verify(backend, crypto, level="checksum")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "checksum mismatch" in out.lower()


def test_sigint_mid_encode_cleans_scratch(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        KeyboardInterrupt during encrypt_file of a large file leaves no
        orphaned scratch directory.
    """
    _patch_chunk(monkeypatch)
    (tmp_path / "big.bin").write_bytes(b"Q" * (CHUNK + 1))

    def boom(self, src: Path, dest: Path) -> None:
        dest.write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(NullCrypto, "encrypt_file", boom)

    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    scratch = staging_root / "oddarchiver_scratch_000"
    staging = staging_root / "oddarchiver_staging_000"

    with pytest.raises(KeyboardInterrupt):
        session_mod.build_staging(
            session_n=0,
            source=tmp_path,
            disc_state={},
            disc_origin={},
            backend=_make_backend(),
            cache=_make_cache(),
            crypto=NullCrypto(),
            _staging_root=staging_root,
        )

    assert not scratch.exists(), "scratch dir must be removed after SIGINT"
    assert not staging.exists(), "staging dir must be removed after SIGINT"


def test_build_staging_marks_large_file_streamed(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A new file above STREAM_CHUNK_SIZE is written under full/ with
        streamed=True on its ManifestEntry.
    """
    _patch_chunk(monkeypatch)
    (tmp_path / "big.bin").write_bytes(b"M" * (CHUNK + 1))
    (tmp_path / "tiny.txt").write_bytes(b"ok")

    chunks = session_mod.build_staging(
        session_n=0,
        source=tmp_path,
        disc_state={},
        disc_origin={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    staging = chunks[0][0]
    try:
        manifest = read_manifest(staging / "session_000" / "manifest.json")
        by_path = {e.path: e for e in manifest.entries}
        assert by_path["big.bin"].streamed is True
        assert by_path["big.bin"].type == "full"
        assert by_path["tiny.txt"].streamed is False
        assert (staging / by_path["big.bin"].file).exists()
        assert not (tmp_path / "staging" / "oddarchiver_scratch_000").exists()
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_iso_extract_path_to_file(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        ISOBackend.extract_path_to_file copies a blob to dest without
        returning bytes, and raises FileNotFoundError for a missing path.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "test.iso"
    assert _dispatch([
        "init", str(source), "--test-iso", str(iso),
        "--encrypt", "none", "--disc-size", "50mb", "--label", "TEST",
    ]) == 0

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    from oddarchiver.session import _blob_id
    rel = f"session_000/full/{_blob_id(0, 'a.txt')}"
    dest = tmp_path / "out.bin"
    backend.extract_path_to_file(rel, dest)
    assert dest.read_bytes() == b"hello"

    with pytest.raises(FileNotFoundError):
        backend.extract_path_to_file(
            f"session_000/full/{'ab' * 32}", tmp_path / "missing.bin",
        )
