"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for oddarchiver.crypto. NullCrypto, PassphraseCrypto, KeyfileCrypto.
"""
# Imports
import logging
import os
import stat
import tempfile

import pytest

from oddarchiver.crypto import (
    KeyfileCrypto,
    NullCrypto,
    PassphraseCrypto,
    generate_keyfile,
    make_crypto,
)
import oddarchiver.crypto as crypto_mod

# Globals
logger = logging.getLogger(__name__)
_log = logger

# Functions

SAMPLE_PLAINTEXT = b"Hello, ODDArchiver! \x00\xff\xde\xad\xbe\xef"


def _assert_null_crypto_identity(operation: str) -> None:
    """Input: operation - encryption or decryption method. Output: None."""
    nc = NullCrypto()
    assert getattr(nc, operation)(SAMPLE_PLAINTEXT) == SAMPLE_PLAINTEXT


class TestNullCrypto:
    def test_round_trip(self):
        nc = NullCrypto()
        assert nc.decrypt(nc.encrypt(SAMPLE_PLAINTEXT)) == SAMPLE_PLAINTEXT

    def test_encrypt_is_identity(self):
        _assert_null_crypto_identity("encrypt")

    def test_decrypt_is_identity(self):
        _assert_null_crypto_identity("decrypt")


class TestPassphraseCrypto:
    def test_round_trip(self):
        pc = PassphraseCrypto(passphrase="correct horse battery staple")
        assert pc.decrypt(pc.encrypt(SAMPLE_PLAINTEXT)) == SAMPLE_PLAINTEXT

    def test_non_2fa_passphrase_round_trip(self):
        pc = PassphraseCrypto(passphrase=b"standalone secret")
        ciphertext = pc.encrypt(SAMPLE_PLAINTEXT)
        assert pc.decrypt(ciphertext) == SAMPLE_PLAINTEXT

    def test_round_trip_arbitrary_bytes(self):
        pc = PassphraseCrypto(passphrase="test-pass")
        data = bytes(range(256))
        assert pc.decrypt(pc.encrypt(data)) == data

    def test_wrong_passphrase_raises(self):
        pc_enc = PassphraseCrypto(passphrase="right")
        pc_dec = PassphraseCrypto(passphrase="wrong")
        ciphertext = pc_enc.encrypt(SAMPLE_PLAINTEXT)
        with pytest.raises(ValueError):
            pc_dec.decrypt(ciphertext)

    def test_no_passphrase_raises(self, monkeypatch):
        monkeypatch.delenv("ODDARCHIVER_PASSPHRASE", raising=False)
        with pytest.raises(RuntimeError):
            PassphraseCrypto()

    def test_env_var_passphrase(self, monkeypatch):
        monkeypatch.setenv("ODDARCHIVER_PASSPHRASE", "env-pass")
        pc = PassphraseCrypto()
        assert pc.decrypt(pc.encrypt(SAMPLE_PLAINTEXT)) == SAMPLE_PLAINTEXT

    def test_no_plaintext_in_tmp(self, tmp_path):
        # Encrypt does not write any temp files
        before = set(os.listdir("/tmp"))
        pc = PassphraseCrypto(passphrase="test-pass")
        pc.encrypt(SAMPLE_PLAINTEXT)
        after = set(os.listdir("/tmp"))
        assert after == before


class TestKeyfileCrypto:
    @pytest.fixture()
    def keyfile(self, tmp_path):
        path = str(tmp_path / "keyfile.bin")
        generate_keyfile(path)
        return path

    def test_round_trip(self, keyfile):
        kc = KeyfileCrypto(keyfile)
        assert kc.decrypt(kc.encrypt(SAMPLE_PLAINTEXT)) == SAMPLE_PLAINTEXT

    def test_round_trip_arbitrary_bytes(self, keyfile):
        kc = KeyfileCrypto(keyfile)
        data = bytes(range(256))
        assert kc.decrypt(kc.encrypt(data)) == data

    def test_wrong_keyfile_raises(self, keyfile, tmp_path):
        kc_enc = KeyfileCrypto(keyfile)
        other = str(tmp_path / "other.bin")
        generate_keyfile(other)
        kc_dec = KeyfileCrypto(other)
        ciphertext = kc_enc.encrypt(SAMPLE_PLAINTEXT)
        with pytest.raises(ValueError):
            kc_dec.decrypt(ciphertext)

    def test_bad_keyfile_length_raises(self, tmp_path):
        bad = str(tmp_path / "bad.bin")
        with open(bad, "wb") as fh:
            fh.write(b"short")
        os.chmod(bad, 0o600)
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            KeyfileCrypto(bad)

    def test_generate_keyfile_uses_private_mode_under_umask(self, tmp_path):
        path = tmp_path / "keyfile.bin"
        previous = os.umask(0o022)
        try:
            generate_keyfile(str(path))
        finally:
            os.umask(previous)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert len(path.read_bytes()) == 32

    def test_generate_keyfile_refuses_existing_file(self, tmp_path):
        path = tmp_path / "keyfile.bin"
        path.write_bytes(b"keep this key")
        with pytest.raises(FileExistsError, match="already exists"):
            generate_keyfile(str(path))
        assert path.read_bytes() == b"keep this key"

    def test_generate_keyfile_cleans_up_failed_write(self, tmp_path, monkeypatch):
        path = tmp_path / "keyfile.bin"
        def fail_write(_fd, _data):
            raise OSError("test write failure")
        monkeypatch.setattr(crypto_mod.os, "write", fail_write)
        with pytest.raises(OSError, match="test write failure"):
            generate_keyfile(str(path))
        assert not path.exists()

    def test_world_readable_keyfile_is_refused(self, tmp_path):
        path = tmp_path / "keyfile.bin"
        path.write_bytes(b"x" * 32)
        path.chmod(0o644)
        with pytest.raises(ValueError, match="mode 0644") as error:
            KeyfileCrypto(str(path))
        assert str(path) in str(error.value)
        assert f"chmod 600 {path}" in str(error.value)

    def test_symlinked_keyfile_is_refused(self, tmp_path):
        target = tmp_path / "keyfile.bin"
        generate_keyfile(str(target))
        link = tmp_path / "link.bin"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="symlink"):
            KeyfileCrypto(str(link))

    def test_nonregular_keyfile_is_refused(self, tmp_path):
        path = tmp_path / "pipe.bin"
        os.mkfifo(path, 0o600)
        with pytest.raises(ValueError, match="Use a regular file"):
            KeyfileCrypto(str(path))

    def test_other_users_keyfile_is_refused(self, tmp_path, monkeypatch):
        path = tmp_path / "keyfile.bin"
        generate_keyfile(str(path))
        uid = os.getuid()
        monkeypatch.setattr(crypto_mod.os, "getuid", lambda: uid + 1)
        with pytest.raises(ValueError, match="Use a regular file that you own") as error:
            KeyfileCrypto(str(path))
        assert str(path) in str(error.value)
        assert "mode 0600" in str(error.value)

    def test_no_plaintext_in_tmp(self, keyfile):
        # Encrypt does not write any temp files
        before = set(os.listdir("/tmp"))
        kc = KeyfileCrypto(keyfile)
        kc.encrypt(SAMPLE_PLAINTEXT)
        after = set(os.listdir("/tmp"))
        assert after == before


class TestMakeCrypto:
    def test_none_mode(self):
        assert isinstance(make_crypto("none"), NullCrypto)

    def test_passphrase_mode(self):
        assert isinstance(make_crypto("passphrase", passphrase="x"), PassphraseCrypto)

    def test_keyfile_mode(self, tmp_path):
        path = str(tmp_path / "k.bin")
        generate_keyfile(path)
        assert isinstance(make_crypto("keyfile", keyfile_path=path), KeyfileCrypto)

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            make_crypto("unknown")


class TestPassphraseBytesRegression:
    """B3 regression: PassphraseCrypto must accept bytes without AttributeError."""

    def test_bytes_passphrase_no_attribute_error(self):
        pc = PassphraseCrypto(passphrase=b"already bytes")
        result = pc.decrypt(pc.encrypt(SAMPLE_PLAINTEXT))
        assert result == SAMPLE_PLAINTEXT

    def test_bytes_and_str_produce_same_key(self):
        pc_str = PassphraseCrypto(passphrase="samepass")
        pc_bytes = PassphraseCrypto(passphrase=b"samepass")
        ciphertext = pc_str.encrypt(SAMPLE_PLAINTEXT)
        assert pc_bytes.decrypt(ciphertext) == SAMPLE_PLAINTEXT


class TestEncryptFile:
    """Streaming encrypt_file/decrypt_file round-trips and edge cases."""

    CHUNK = 64

    def _patch_chunk(self, monkeypatch):
        monkeypatch.setattr(crypto_mod, "STREAM_CHUNK_SIZE", self.CHUNK)

    def _round_trip(self, crypto, tmp_path, data: bytes):
        src = tmp_path / "plain.bin"
        enc = tmp_path / "enc.bin"
        out = tmp_path / "out.bin"
        src.write_bytes(data)
        crypto.encrypt_file(src, enc)
        crypto.decrypt_file(enc, out)
        assert out.read_bytes() == data
        return enc

    def test_passphrase_multi_chunk_round_trip(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        data = b"A" * (self.CHUNK * 3 + 7)
        pc = PassphraseCrypto(passphrase="stream-pass")
        self._round_trip(pc, tmp_path, data)

    def test_keyfile_multi_chunk_round_trip(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        key = str(tmp_path / "k.bin")
        generate_keyfile(key)
        data = b"B" * (self.CHUNK * 3 + 7)
        kc = KeyfileCrypto(key)
        self._round_trip(kc, tmp_path, data)

    def test_exact_chunk_multiple_no_trailing_empty(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        data = b"C" * (self.CHUNK * 2)
        pc = PassphraseCrypto(passphrase="stream-pass")
        enc = self._round_trip(pc, tmp_path, data)
        # salt(16) + 2 * (nonce(12) + ct(chunk+16))
        expected = 16 + 2 * (12 + self.CHUNK + 16)
        assert enc.stat().st_size == expected

    def test_zero_byte_file_round_trip(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        pc = PassphraseCrypto(passphrase="stream-pass")
        enc = self._round_trip(pc, tmp_path, b"")
        # salt(16) + nonce(12) + empty-plaintext tag(16)
        assert enc.stat().st_size == 16 + 12 + 16

    def test_single_byte_file_round_trip(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        pc = PassphraseCrypto(passphrase="stream-pass")
        self._round_trip(pc, tmp_path, b"Z")

    def test_tampered_chunk_fails_auth(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        data = b"D" * (self.CHUNK * 2 + 3)
        pc = PassphraseCrypto(passphrase="stream-pass")
        src = tmp_path / "plain.bin"
        enc = tmp_path / "enc.bin"
        out = tmp_path / "out.bin"
        src.write_bytes(data)
        pc.encrypt_file(src, enc)
        blob = bytearray(enc.read_bytes())
        blob[-1] ^= 0xFF
        enc.write_bytes(bytes(blob))
        with pytest.raises(ValueError, match="authentication"):
            pc.decrypt_file(enc, out)

    def test_null_crypto_file_round_trip(self, tmp_path):
        data = b"identity-stream"
        nc = NullCrypto()
        self._round_trip(nc, tmp_path, data)

    def test_keyfile_tampered_chunk_fails_auth(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        key = str(tmp_path / "k.bin")
        generate_keyfile(key)
        kc = KeyfileCrypto(key)
        src = tmp_path / "plain.bin"
        enc = tmp_path / "enc.bin"
        out = tmp_path / "out.bin"
        src.write_bytes(b"E" * (self.CHUNK + 1))
        kc.encrypt_file(src, enc)
        blob = bytearray(enc.read_bytes())
        blob[-1] ^= 0x01
        enc.write_bytes(bytes(blob))
        with pytest.raises(ValueError, match="authentication"):
            kc.decrypt_file(enc, out)



class TestEncryptFileRange:
    """offset/length byte-range encryption (section 30 volume-splitting groundwork)."""

    CHUNK = 64

    def _patch_chunk(self, monkeypatch):
        monkeypatch.setattr(crypto_mod, "STREAM_CHUNK_SIZE", self.CHUNK)

    def test_passphrase_middle_range_round_trip(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        data = bytes((i % 251) for i in range(self.CHUNK * 5 + 13))
        src = tmp_path / "plain.bin"
        src.write_bytes(data)
        offset, length = self.CHUNK * 2 + 3, self.CHUNK * 2 + 7
        pc = PassphraseCrypto(passphrase="range-pass")
        enc = tmp_path / "enc.bin"
        out = tmp_path / "out.bin"
        pc.encrypt_file(src, enc, offset=offset, length=length)
        pc.decrypt_file(enc, out)
        assert out.read_bytes() == data[offset : offset + length]

    def test_passphrase_offset_start_and_partial_final_chunk_round_trip(
        self, tmp_path, monkeypatch
    ):
        self._patch_chunk(monkeypatch)
        data = bytes((i % 251) for i in range(self.CHUNK * 3 + 11))
        src = tmp_path / "plain.bin"
        src.write_bytes(data)
        offset = self.CHUNK + 5
        length = len(data) - offset  # reaches EOF with a partial final sub-chunk
        assert length % self.CHUNK != 0
        pc = PassphraseCrypto(passphrase="range-pass")
        enc = tmp_path / "enc.bin"
        out = tmp_path / "out.bin"
        pc.encrypt_file(src, enc, offset=offset, length=length)
        pc.decrypt_file(enc, out)
        assert out.read_bytes() == data[offset:]

    def test_keyfile_middle_range_round_trip(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        key = str(tmp_path / "k.bin")
        generate_keyfile(key)
        data = bytes((i % 251) for i in range(self.CHUNK * 4 + 9))
        src = tmp_path / "plain.bin"
        src.write_bytes(data)
        offset, length = self.CHUNK, self.CHUNK * 2 + 1
        kc = KeyfileCrypto(key)
        enc = tmp_path / "enc.bin"
        out = tmp_path / "out.bin"
        kc.encrypt_file(src, enc, offset=offset, length=length)
        kc.decrypt_file(enc, out)
        assert out.read_bytes() == data[offset : offset + length]

    def test_two_independent_ranges_decrypt_independently(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        data = bytes((i % 251) for i in range(self.CHUNK * 6 + 5))
        src = tmp_path / "plain.bin"
        src.write_bytes(data)
        pc = PassphraseCrypto(passphrase="range-pass")

        range_a = (self.CHUNK, self.CHUNK * 2)
        range_b = (self.CHUNK * 4, self.CHUNK + 3)

        enc_a = tmp_path / "enc_a.bin"
        enc_b = tmp_path / "enc_b.bin"
        out_a = tmp_path / "out_a.bin"
        out_b = tmp_path / "out_b.bin"

        pc.encrypt_file(src, enc_a, offset=range_a[0], length=range_a[1])
        pc.encrypt_file(src, enc_b, offset=range_b[0], length=range_b[1])

        # Independently decryptable: decrypting b before a proves neither
        # needs any shared state or the other's presence.
        pc.decrypt_file(enc_b, out_b)
        pc.decrypt_file(enc_a, out_a)

        assert out_a.read_bytes() == data[range_a[0] : range_a[0] + range_a[1]]
        assert out_b.read_bytes() == data[range_b[0] : range_b[0] + range_b[1]]
        # Independent random salts/nonces -- ciphertexts never collide even
        # though both derive from the same source file.
        assert enc_a.read_bytes() != enc_b.read_bytes()

    def test_passphrase_range_ciphertext_is_complete_streamed_blob(
        self, tmp_path, monkeypatch
    ):
        self._patch_chunk(monkeypatch)
        data = bytes((i % 251) for i in range(self.CHUNK * 2 + 3))
        src = tmp_path / "plain.bin"
        src.write_bytes(data)
        offset, length = self.CHUNK, self.CHUNK + 3
        pc = PassphraseCrypto(passphrase="range-pass")
        enc = tmp_path / "enc.bin"
        pc.encrypt_file(src, enc, offset=offset, length=length)
        # salt(16) + one full CHUNK-sized sub-chunk + a 3-byte trailer.
        expected = 16 + (12 + self.CHUNK + 16) + (12 + 3 + 16)
        assert enc.stat().st_size == expected

    def test_passphrase_range_tampered_chunk_fails_auth(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        data = bytes((i % 251) for i in range(self.CHUNK * 3))
        src = tmp_path / "plain.bin"
        src.write_bytes(data)
        pc = PassphraseCrypto(passphrase="range-pass")
        enc = tmp_path / "enc.bin"
        out = tmp_path / "out.bin"
        pc.encrypt_file(src, enc, offset=self.CHUNK, length=self.CHUNK)
        blob = bytearray(enc.read_bytes())
        blob[-1] ^= 0xFF
        enc.write_bytes(bytes(blob))
        with pytest.raises(ValueError, match="authentication"):
            pc.decrypt_file(enc, out)

    def test_keyfile_range_tampered_chunk_fails_auth(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        key = str(tmp_path / "k.bin")
        generate_keyfile(key)
        data = bytes((i % 251) for i in range(self.CHUNK * 3))
        src = tmp_path / "plain.bin"
        src.write_bytes(data)
        kc = KeyfileCrypto(key)
        enc = tmp_path / "enc.bin"
        out = tmp_path / "out.bin"
        kc.encrypt_file(src, enc, offset=self.CHUNK, length=self.CHUNK)
        blob = bytearray(enc.read_bytes())
        blob[-1] ^= 0x01
        enc.write_bytes(bytes(blob))
        with pytest.raises(ValueError, match="authentication"):
            kc.decrypt_file(enc, out)

    def test_null_crypto_range_round_trip(self, tmp_path):
        data = b"0123456789ABCDEFGHIJ"
        nc = NullCrypto()
        src = tmp_path / "plain.bin"
        src.write_bytes(data)
        enc = tmp_path / "enc.bin"
        out = tmp_path / "out.bin"
        nc.encrypt_file(src, enc, offset=3, length=5)
        nc.decrypt_file(enc, out)
        assert out.read_bytes() == data[3:8]

    def test_omitted_offset_length_matches_whole_file_behavior(self, tmp_path, monkeypatch):
        self._patch_chunk(monkeypatch)
        data = bytes((i % 251) for i in range(self.CHUNK * 3 + 5))
        src = tmp_path / "plain.bin"
        src.write_bytes(data)
        pc = PassphraseCrypto(passphrase="range-pass")
        whole = tmp_path / "whole.bin"
        explicit = tmp_path / "explicit.bin"
        pc.encrypt_file(src, whole)
        pc.encrypt_file(src, explicit, offset=0, length=None)
        out_whole = tmp_path / "out_whole.bin"
        out_explicit = tmp_path / "out_explicit.bin"
        pc.decrypt_file(whole, out_whole)
        pc.decrypt_file(explicit, out_explicit)
        assert out_whole.read_bytes() == data
        assert out_explicit.read_bytes() == data
