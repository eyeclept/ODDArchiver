"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Regression tests for a source file that changes between the scan and
    the staging read. The sync archives the bytes that staging read, under
    their own checksum, warns, and exits 0. The next sync archives the
    content that is on disk then.
"""
# Imports
import hashlib
import io
import logging
import random
from pathlib import Path

import pytest

import oddarchiver.crypto as crypto_mod
import oddarchiver.restore as restore_mod
import oddarchiver.session as session_mod
import oddarchiver.verify as verify_mod
from oddarchiver.cache import CacheManager
from oddarchiver.cli import dispatch
from oddarchiver.crypto import KeyfileCrypto, NullCrypto, PassphraseCrypto, generate_keyfile
from oddarchiver.delta import apply_delta
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import read_manifest
from oddarchiver.restore import restore
from tests.helpers import _args, fill

# Globals
logger = logging.getLogger(__name__)
DISC_BYTES = 64 * 2**20
BASE = fill(b"A", 34000)
CHUNK = 4096

# Functions


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """
    Input: isolated test directory and monkeypatch
    Output: source, ISO, cache, and the data.bin path
    Details: Keep the ledger, cache, and staging out of the user HOME.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg_runtime"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cache = CacheManager(tmp_path / "cache")
    monkeypatch.setattr("oddarchiver.cli.CacheManager", lambda: cache)
    source = tmp_path / "source"
    source.mkdir()
    path = source / "data.bin"
    path.write_bytes(BASE)
    iso = tmp_path / "archive.iso"
    assert dispatch(_args("init", source, iso, label="SOURCE_RACE")) == 0
    return source, iso, cache, path


def _race(monkeypatch, path: Path, read_back: bytes) -> dict:
    """
    Input: monkeypatch, the source path, and the bytes a later read returns
    Output: {"reads": count} of binary reads of path
    Details:
        The first binary open of path (the scan) reads the disk. Each
        later open returns read_back. The disk keeps the scanned content.
    """
    original = Path.open
    state = {"reads": 0}

    def racing_open(self, mode="r", *args, **kwargs):
        if self == path and "r" in mode and "b" in mode:
            state["reads"] += 1
            if state["reads"] > 1:
                return io.BytesIO(read_back)
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    state["undo"] = lambda: monkeypatch.setattr(Path, "open", original)
    return state


def _backend(iso: Path) -> ISOBackend:
    """
    Input: ISO path
    Output: ISOBackend for the test disc size
    """
    return ISOBackend(iso, disc_size=DISC_BYTES)


def _entry(iso: Path, session: int, name: str):
    """
    Input: ISO path, session number, and source-relative path
    Output: the manifest entry for name in that session
    """
    backend = _backend(iso)
    return next(entry for entry in read_manifest(
        backend._sessions_root / f"session_{session:03d}" / "manifest.json"
    ).entries if entry.path == name)


def _burned_content(iso: Path, session: int, name: str, base: bytes = BASE) -> bytes:
    """
    Input: ISO path, session, path, and the base of a delta entry
    Output: plaintext content that the burned blob holds
    Details: NullCrypto blobs are plaintext. A delta applies to base.
    """
    entry = _entry(iso, session, name)
    backend = _backend(iso)
    if entry.type == "delta":
        return apply_delta(base, backend.read_path(entry.delta_file))
    return backend.read_path(entry.file)


def _restored(iso: Path, dest: Path, name: str) -> bytes:
    """
    Input: ISO path, empty destination, and path
    Output: restored bytes of name
    """
    result = restore(dest, _backend(iso), NullCrypto())
    assert result.failures == 0
    return (dest / name).read_bytes()


def _assert_warned(name: str, capsys, caplog) -> None:
    """
    Input: path, captured output, and captured log
    Output: None
    Details: Check the stderr line and the log warning for name.
    """
    err = capsys.readouterr().err
    assert (
        f"Warning: {name} changed while it was read. "
        "The archive holds the version that was read."
    ) in err
    assert any(
        record.levelno == logging.WARNING
        and f"source changed while it was read: {name}" in record.getMessage()
        for record in caplog.records
    )


def _assert_race_result(
    iso: Path, tmp_path: Path, session: int, name: str, read_back: bytes,
    base: bytes = BASE,
) -> None:
    """
    Input: ISO, test dir, session, path, bytes that staging read, delta base
    Output: None
    Details:
        result_checksum is the sha256 of the burned content, and restore
        gives the bytes that staging read.
    """
    entry = _entry(iso, session, name)
    burned = _burned_content(iso, session, name, base)
    assert burned == read_back
    assert entry.result_checksum == hashlib.sha256(burned).hexdigest()
    assert entry.full_size_bytes == len(read_back)
    assert _restored(iso, tmp_path / f"restored_{session}", name) == read_back


def _assert_next_sync(source: Path, iso: Path, tmp_path: Path, name: str, current: bytes) -> None:
    """
    Input: source, ISO, test dir, path, and its content on disk
    Output: None
    Details: A later sync archives the content that is on disk now.
    """
    assert dispatch(_args("sync", source, iso, label="SOURCE_RACE")) == 0
    session = _backend(iso).mediainfo().session_count - 1
    entry = _entry(iso, session, name)
    assert entry.result_checksum == hashlib.sha256(current).hexdigest()
    assert _restored(iso, tmp_path / "restored_next", name) == current


def _patch_chunk(monkeypatch) -> None:
    """Point every STREAM_CHUNK_SIZE binding at the small test chunk."""
    monkeypatch.setattr(crypto_mod, "STREAM_CHUNK_SIZE", CHUNK)
    monkeypatch.setattr(session_mod, "STREAM_CHUNK_SIZE", CHUNK)
    monkeypatch.setattr(restore_mod, "HASH_CHUNK_SIZE", CHUNK)
    monkeypatch.setattr(verify_mod, "STREAM_CHUNK_SIZE", CHUNK)


def test_changed_file_delta_archives_bytes_read(archive, tmp_path, monkeypatch, capsys, caplog):
    """
    Input: a changed file whose staging read differs from the scan
    Output: a delta entry for the bytes read, and exit 0
    Details: The delta sidecar caches the bytes read.
    """
    source, iso, cache, path = archive
    scanned = b"B" + BASE[1:]
    read_back = b"C" + BASE[1:]
    path.write_bytes(scanned)
    race = _race(monkeypatch, path, read_back)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso, label="SOURCE_RACE")) == 0
    race["undo"]()
    assert race["reads"] == 2
    _assert_warned("data.bin", capsys, caplog)
    assert _entry(iso, 1, "data.bin").type == "delta"
    assert cache.get("data.bin", 1) == read_back
    _assert_race_result(iso, tmp_path, 1, "data.bin", read_back)
    _assert_next_sync(source, iso, tmp_path, "data.bin", scanned)


def test_changed_file_full_archives_bytes_read(archive, tmp_path, monkeypatch, capsys, caplog):
    """
    Input: a changed file whose staging read returns unrelated bytes
    Output: a full entry for the bytes read, and exit 0
    Details: Random bytes make the delta larger than the full blob.
    """
    source, iso, cache, path = archive
    scanned = b"B" + BASE[1:]
    read_back = random.Random(7).randbytes(len(BASE))
    path.write_bytes(scanned)
    race = _race(monkeypatch, path, read_back)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso, label="SOURCE_RACE")) == 0
    race["undo"]()
    _assert_warned("data.bin", capsys, caplog)
    assert _entry(iso, 1, "data.bin").type == "full"
    _assert_race_result(iso, tmp_path, 1, "data.bin", read_back)
    _assert_next_sync(source, iso, tmp_path, "data.bin", scanned)


def test_new_small_file_archives_bytes_read(archive, tmp_path, monkeypatch, capsys, caplog):
    """
    Input: a new file whose staging read differs from the scan
    Output: a full entry for the bytes read, and exit 0
    """
    source, iso, cache, _path = archive
    new = source / "new.txt"
    scanned = b"scanned content\n"
    read_back = b"content that the staging read returned\n"
    new.write_bytes(scanned)
    race = _race(monkeypatch, new, read_back)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso, label="SOURCE_RACE")) == 0
    race["undo"]()
    _assert_warned("new.txt", capsys, caplog)
    assert _entry(iso, 1, "new.txt").type == "full"
    _assert_race_result(iso, tmp_path, 1, "new.txt", read_back)
    _assert_next_sync(source, iso, tmp_path, "new.txt", scanned)


def test_streamed_file_archives_bytes_read(archive, tmp_path, monkeypatch, capsys, caplog):
    """
    Input: a new file above STREAM_CHUNK_SIZE whose read differs from the scan
    Output: a streamed full entry for the bytes read, and exit 0
    Details: encrypt_file hashes the bytes in the read that encodes them.
    """
    source, iso, cache, _path = archive
    _patch_chunk(monkeypatch)
    big = source / "big.bin"
    scanned = b"S" * (3 * CHUNK + 5)
    read_back = b"R" * (2 * CHUNK + 9)
    big.write_bytes(scanned)
    race = _race(monkeypatch, big, read_back)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso, label="SOURCE_RACE")) == 0
    race["undo"]()
    assert race["reads"] == 2
    _assert_warned("big.bin", capsys, caplog)
    entry = _entry(iso, 1, "big.bin")
    assert entry.type == "full"
    assert entry.streamed
    _assert_race_result(iso, tmp_path, 1, "big.bin", read_back)
    _assert_next_sync(source, iso, tmp_path, "big.bin", scanned)


def test_dedup_candidate_archives_bytes_read(archive, tmp_path, monkeypatch, capsys, caplog):
    """
    Input: a new file whose scan checksum matches a blob on disc, but whose
           staging read returns other bytes
    Output: an own full blob for the bytes read, not a dedup reference
    Details: The next sync reads the scanned content and shares the blob.
    """
    source, iso, cache, _path = archive
    copy = source / "copy.bin"
    read_back = b"Z" + BASE[1:]
    copy.write_bytes(BASE)
    race = _race(monkeypatch, copy, read_back)
    capsys.readouterr()
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso, label="SOURCE_RACE")) == 0
    race["undo"]()
    _assert_warned("copy.bin", capsys, caplog)
    entry = _entry(iso, 1, "copy.bin")
    assert entry.type == "full"
    assert entry.file.startswith("session_001/")
    _assert_race_result(iso, tmp_path, 1, "copy.bin", read_back)
    _assert_next_sync(source, iso, tmp_path, "copy.bin", BASE)
    assert _entry(iso, 2, "copy.bin").file == _entry(iso, 0, "data.bin").file


def test_delta_self_check_failure_stages_full_blob(archive, tmp_path, monkeypatch, caplog):
    """
    Input: a delta that does not rebuild the bytes read
    Output: a full entry with a warning, and exit 0
    """
    source, iso, cache, path = archive
    content = b"B" + BASE[1:]
    path.write_bytes(content)
    monkeypatch.setattr(session_mod, "apply_delta", lambda old, delta: b"wrong")
    with caplog.at_level(logging.WARNING):
        assert dispatch(_args("sync", source, iso, label="SOURCE_RACE")) == 0
    assert any(
        "delta result checksum did not match for data.bin" in record.getMessage()
        for record in caplog.records
    )
    assert _entry(iso, 1, "data.bin").type == "full"
    _assert_race_result(iso, tmp_path, 1, "data.bin", content)


@pytest.mark.parametrize("backend_name", ["null", "passphrase", "keyfile"])
def test_encrypt_file_digest_matches_source(tmp_path, monkeypatch, backend_name):
    """
    Input: one crypto backend and a multi-chunk source file
    Output: None
    Details:
        The digest equals the sha256 of the source, and the output
        decrypts to the source.
    """
    monkeypatch.setattr(crypto_mod, "STREAM_CHUNK_SIZE", CHUNK)
    if backend_name == "null":
        crypto = NullCrypto()
    elif backend_name == "passphrase":
        crypto = PassphraseCrypto(passphrase="correct horse battery staple")
    else:
        keyfile = tmp_path / "key.bin"
        generate_keyfile(str(keyfile))
        crypto = KeyfileCrypto(str(keyfile))
    data = random.Random(11).randbytes(3 * CHUNK + 17)
    src = tmp_path / "plain.bin"
    src.write_bytes(data)
    enc = tmp_path / "enc.bin"
    out = tmp_path / "out.bin"
    digest = hashlib.sha256()
    crypto.encrypt_file(src, enc, digest=digest)
    assert digest.hexdigest() == hashlib.sha256(data).hexdigest()
    crypto.decrypt_file(enc, out)
    assert out.read_bytes() == data
