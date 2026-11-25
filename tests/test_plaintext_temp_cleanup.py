"""
Interrupted restore and verify must not leave decrypted temp files behind.

A live Ctrl-C test found an oddarchiver_vsplit_ directory holding a
644 MB decrypted chunk that survived the process.
"""

import tempfile
from pathlib import Path

import pytest

from oddarchiver import restore as restore_mod
from oddarchiver import verify as verify_mod
from oddarchiver.manifest import ManifestEntry

_BLOB = "session_000/full/" + "ab" * 32 + "_0"


def _split_entry() -> ManifestEntry:
    """Output: the first chunk entry of a two-chunk split file."""
    return ManifestEntry(
        path="big.bin", type="full", file=_BLOB, result_checksum="00" * 32,
        full_size_bytes=10, volume_chunk=0, volume_chunk_count=2,
    )


def test_interrupted_split_chunk_restore_removes_plaintext(tmp_path, monkeypatch):
    """Ctrl-C during decrypt must still delete the temp directory."""
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))

    class Reader:
        def extract_path_to_file(self, path, dest):
            Path(dest).write_bytes(b"cipher")

    class Crypto:
        def decrypt_file(self, src, dest, aad=None):
            Path(dest).write_bytes(b"plaintext")
            raise KeyboardInterrupt

    task = restore_mod._FileTask(
        rel_path="big.bin", entries=[(0, _split_entry())],
        dest_file=tmp_path / "out" / "big.bin", target_checksum="00" * 32,
        last_entry_session=0, recover=False, streamed=False, volume_split=True,
    )
    with pytest.raises(KeyboardInterrupt):
        restore_mod._apply_volume_split_chunk(task, Reader(), Crypto())
    assert list(temp_root.iterdir()) == []


def test_verify_removes_temp_of_split_chain_that_never_completes(tmp_path, monkeypatch):
    """A chain whose last chunk is on another disc still owns a temp dir."""
    payload_dir = tmp_path / "oddarchiver_vsplit_verify_x"
    payload_dir.mkdir()
    (payload_dir / "payload").write_bytes(b"decrypted half")

    def one_session(s, incomplete, by_session, level, backend, crypto,
                    capture_dir, source, split_progress):
        split_progress["big.bin"] = (
            None, 1, 2, "00" * 32, _BLOB, payload_dir / "payload",
        )
        return 0

    monkeypatch.setattr(verify_mod, "_verify_one_session", one_session)
    verify_mod._verify_session_pass(
        None, None, "checksum", {0: object()}, None, tmp_path,
        tmp_path / "cap", "linear", True,
    )
    assert not payload_dir.exists()
