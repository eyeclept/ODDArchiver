"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for per-blob LZMA compression and codec manifest fields.
"""
# Imports
import pytest
import json
import logging
import lzma
import os
import shutil
from unittest.mock import MagicMock

from oddarchiver.compress import (
    CODEC_LZMA, CODEC_NONE, compress_bytes, compress_file,
    decode_payload, decompress_bytes, decompress_file,
)
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import (
    ManifestEntry,
    _compute_checksum,
    manifest_checksum_ok,
    read_manifest,
    write_manifest,
)
from oddarchiver.restore import _apply_delta_entry, _read_full
from oddarchiver.session import build_staging
from oddarchiver.verify import _check_buffered_blob

pytestmark = pytest.mark.usefixtures("compression_on")

from tests.helpers import _make_manifest, _sha256

# Globals
logger = logging.getLogger(__name__)
_TIMESTAMP = "2026-04-19T12:00:00Z"


# Functions


def _assert_lzma_cap_chunk(
    entry: ManifestEntry, payload: bytes, blob: bytes, cap: int,
) -> None:
    """Input: one split entry and blob. Output: None; checks codec and cap."""
    assert entry.path == "note.txt"
    assert entry.codec == CODEC_LZMA
    assert entry.original_size == len(payload)
    assert len(blob) <= cap


def _assert_cap_split_decodes(
    chunks: list, payload: bytes, cap: int,
) -> None:
    """Input: staged chunks, source bytes, cap. Output: None; checks split xz."""
    parts: list[bytes] = []
    count = None
    for staging, session_n, _is_new in chunks:
        manifest = read_manifest(
            staging / f"session_{session_n:03d}" / "manifest.json"
        )
        for entry in manifest.entries:
            blob = (staging / entry.file).read_bytes()
            _assert_lzma_cap_chunk(entry, payload, blob, cap)
            if count is None:
                count = entry.volume_chunk_count
            parts.append(blob)
    encoded = b"".join(parts)
    assert count == len(parts)
    assert len(parts) > 1
    assert lzma.decompress(encoded, format=lzma.FORMAT_XZ) == payload




def test_text_compresses_to_lzma(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Repeated text writes codec lzma. encoded_size is well below raw.
    """
    src = tmp_path / "note.txt"
    src.write_bytes(b"abcdefghijklmnopqrstuvwxyz\n" * 4000)
    dest = tmp_path / "note.xz"
    raw_size = src.stat().st_size
    codec, encoded_size = compress_file(src, dest)
    assert codec == CODEC_LZMA
    assert encoded_size == dest.stat().st_size
    assert encoded_size < 0.5 * raw_size
    assert lzma.decompress(dest.read_bytes(), format=lzma.FORMAT_XZ) == src.read_bytes()


def test_urandom_stays_codec_none(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Random bytes stay codec none and dest matches src.
    """
    src = tmp_path / "rand.bin"
    payload = os.urandom(50_000)
    src.write_bytes(payload)
    dest = tmp_path / "rand.out"
    codec, encoded_size = compress_file(src, dest)
    assert codec == CODEC_NONE
    assert encoded_size == len(payload)
    assert dest.read_bytes() == payload


def test_manifest_missing_codec_defaults_none(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        JSON without codec or original_size loads as codec none and
        original_size equal to the stored size.
    """
    payload = {
        "version": 1,
        "session": 0,
        "timestamp": _TIMESTAMP,
        "source": "/src",
        "label": "TEST",
        "based_on_session": None,
        "encryption": {"mode": "none"},
        "entries": [
            {
                "path": "a.txt",
                "type": "full",
                "result_checksum": "abc123",
                "full_size_bytes": 4,
                "source_checksum": "",
                "delta_file": "",
                "delta_size_bytes": 0,
                "file": "session_000/full/" + ("ab" * 32),
                "encrypted_dek": "",
            }
        ],
        "deleted": [],
        "drives": [],
        "manifest_checksum": "",
    }
    payload["manifest_checksum"] = _compute_checksum(
        {**payload, "manifest_checksum": ""}
    )
    dest = tmp_path / "manifest.json"
    dest.write_text(json.dumps(payload), encoding="utf-8")
    loaded = read_manifest(dest)
    assert not loaded.suspect
    assert loaded.entries[0].codec == CODEC_NONE
    assert loaded.entries[0].original_size == 4
    assert manifest_checksum_ok(loaded)


def test_lzma_entry_round_trips(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        codec lzma and original_size survive write_manifest/read_manifest
        and remain in the JSON.
    """
    entry = ManifestEntry(
        path="note.txt",
        type="full",
        result_checksum="dead",
        full_size_bytes=1234,
        codec=CODEC_LZMA,
        original_size=9999,
    )
    write_manifest(tmp_path, _make_manifest(entries=[entry]))
    raw = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert raw["entries"][0]["codec"] == CODEC_LZMA
    assert raw["entries"][0]["original_size"] == 9999
    loaded = read_manifest(tmp_path / "manifest.json")
    assert not loaded.suspect
    assert loaded.entries[0].codec == CODEC_LZMA
    assert loaded.entries[0].original_size == 9999


def test_codec_none_omitted_from_json(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        codec none does not write codec or original_size keys.
    """
    entry = ManifestEntry(
        path="a.txt",
        type="full",
        result_checksum="dead",
        full_size_bytes=4,
        codec=CODEC_NONE,
        original_size=4,
    )
    write_manifest(tmp_path, _make_manifest(entries=[entry]))
    raw = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "codec" not in raw["entries"][0]
    assert "original_size" not in raw["entries"][0]


def test_encoded_blob_splits_at_blob_size_cap(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A compressible file with a small BLOB_SIZE_CAP splits into
        cap-sized encoded chunks. Concatenated chunks decode to src.
    """
    cap = 64
    monkeypatch.setattr("oddarchiver.session.BLOB_SIZE_CAP", cap)
    source = tmp_path / "src"
    source.mkdir()
    payload = b"compressible text line\n" * 4000
    (source / "note.txt").write_bytes(payload)
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "test.iso", disc_size=10_000_000),
        cache=cache,
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        _assert_cap_split_decodes(chunks, payload, cap)
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)



def test_compress_bytes_round_trips(tmp_path):
    """Input: tmp_path. Output: None. Details: In-memory compress then decompress."""
    payload = b"compressible text line\n" * 200
    dest = tmp_path / "out.xz"
    codec, size = compress_bytes(payload, dest)
    assert codec == CODEC_LZMA
    assert size == dest.stat().st_size
    assert decompress_bytes(dest.read_bytes()) == payload
    out = tmp_path / "plain"
    decompress_file(dest, out)
    assert out.read_bytes() == payload


class _DirReader:
    """Input: staging root. Output: blob bytes from that tree."""

    def __init__(self, root):
        self.root = root

    def read_path(self, rel):
        return (self.root / rel).read_bytes()


def _stage_source(tmp_path, rel, payload, monkeypatch=None, cap=None):
    """Input: tmp_path, path, bytes. Output: staging dir, manifest, payload."""
    if cap is not None:
        monkeypatch.setattr("oddarchiver.session.BLOB_SIZE_CAP", cap)
    source = tmp_path / "src"
    source.mkdir()
    (source / rel).write_bytes(payload)
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=ISOBackend(tmp_path / "test.iso", disc_size=10_000_000),
        cache=cache,
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    staging, session_n, _is_new = chunks[0]
    manifest = read_manifest(staging / f"session_{session_n:03d}" / "manifest.json")
    return staging, manifest, chunks


def test_compressed_full_blob_restores(tmp_path):
    """Input: tmp_path. Output: None. Details: lzma full blob restores bytes."""
    payload = b"hello world, this is archive text.\n" * 3000
    staging, manifest, chunks = _stage_source(tmp_path, "doc.txt", payload)
    try:
        entry = manifest.entries[0]
        assert entry.codec == CODEC_LZMA
        data, ok = _read_full("doc.txt", entry, _DirReader(staging), NullCrypto())
        assert ok
        assert data == payload
        assert len(data) == entry.original_size
        assert _sha256(data) == entry.result_checksum
    finally:
        for staging_dir, _session_n, _is_new in chunks:
            shutil.rmtree(staging_dir, ignore_errors=True)


def test_compressed_split_blob_across_cap_restores(tmp_path, monkeypatch):
    """Input: tmp_path. Output: None. Details: cap-split lzma concat restores."""
    payload = b"compressible text line\n" * 4000
    staging, manifest, chunks = _stage_source(
        tmp_path, "note.txt", payload, monkeypatch=monkeypatch, cap=64,
    )
    try:
        stored = b"".join(
            (stg / entry.file).read_bytes()
            for stg, _n, _new in chunks
            for entry in read_manifest(
                stg / f"session_{_n:03d}" / "manifest.json"
            ).entries
        )
        entry = manifest.entries[0]
        assert entry.codec == CODEC_LZMA
        data = decode_payload(stored, entry.codec)
        assert data == payload
        assert len(data) == entry.original_size
    finally:
        for staging_dir, _session_n, _is_new in chunks:
            shutil.rmtree(staging_dir, ignore_errors=True)


def test_delta_on_compressed_base_restores(tmp_path, monkeypatch):
    """Input: tmp_path. Output: None. Details: delta over lzma base restores."""
    old = b"hello world, this is archive text.\n" * 3000
    new = b"hello world, this is CHANGED text.\n" * 3000
    staging, manifest, chunks = _stage_source(tmp_path, "doc.txt", old)
    try:
        base_entry = manifest.entries[0]
        reader = _DirReader(staging)
        base, ok = _read_full("doc.txt", base_entry, reader, NullCrypto())
        assert ok and base == old
        delta_raw = b"delta payload that compresses well\n" * 200
        name = "0" * 64
        delta_path = staging / "session_000" / "deltas"
        delta_path.mkdir(parents=True, exist_ok=True)
        blob = delta_path / name
        codec, _size = compress_bytes(delta_raw, blob)
        assert codec == CODEC_LZMA
        monkeypatch.setattr(
            "oddarchiver.restore.apply_delta",
            lambda _src, payload: new if payload == delta_raw else b"",
        )
        delta_entry = ManifestEntry(
            path="doc.txt",
            type="delta",
            result_checksum=_sha256(new),
            full_size_bytes=len(new),
            source_checksum=_sha256(old),
            delta_file=f"session_000/deltas/{name}",
            delta_size_bytes=len(delta_raw),
            codec=codec,
            original_size=len(delta_raw),
        )
        patched, _blob, dok = _apply_delta_entry(
            "doc.txt", 1, delta_entry, base, None, reader, NullCrypto(),
        )
        assert dok
        assert patched == new
    finally:
        for staging_dir, _session_n, _is_new in chunks:
            shutil.rmtree(staging_dir, ignore_errors=True)


def test_verify_full_on_compressed_archive(tmp_path):
    """Input: tmp_path. Output: None. Details: checksum verify of lzma blob."""
    payload = b"hello world, this is archive text.\n" * 3000
    staging, manifest, chunks = _stage_source(tmp_path, "doc.txt", payload)
    try:
        entry = manifest.entries[0]
        errors = []
        _check_buffered_blob(
            entry, entry.file, MagicMock(), NullCrypto(), errors, None, staging,
        )
        assert errors == []
    finally:
        for staging_dir, _session_n, _is_new in chunks:
            shutil.rmtree(staging_dir, ignore_errors=True)


def test_precompression_manifest_still_restores(tmp_path):
    """Input: tmp_path. Output: None. Details: legacy no-codec blob restores."""
    payload = b"hello archiver"
    staging = tmp_path / "stg"
    name = "a" * 64
    session_dir = staging / "session_000" / "full"
    session_dir.mkdir(parents=True)
    blob = session_dir / name
    blob.write_bytes(payload)
    entry = ManifestEntry(
        path="a.txt",
        type="full",
        result_checksum=_sha256(payload),
        full_size_bytes=len(payload),
        file=f"session_000/full/{name}",
    )
    data, ok = _read_full("a.txt", entry, _DirReader(staging), NullCrypto())
    assert ok
    assert data == payload


def test_incompressible_stores_none_and_restores(tmp_path):
    """Input: tmp_path. Output: None. Details: urandom stays codec none."""
    payload = os.urandom(4096)
    staging, manifest, chunks = _stage_source(tmp_path, "rand.bin", payload)
    try:
        entry = manifest.entries[0]
        assert entry.codec == CODEC_NONE
        data, ok = _read_full("rand.bin", entry, _DirReader(staging), NullCrypto())
        assert ok
        assert data == payload
    finally:
        for staging_dir, _session_n, _is_new in chunks:
            shutil.rmtree(staging_dir, ignore_errors=True)
