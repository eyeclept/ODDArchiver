"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for oddarchiver.crypto — NullCrypto, PassphraseCrypto, KeyfileCrypto.
"""
# Imports
import os
import tempfile

import pytest

from oddarchiver.crypto import (
    KeyfileCrypto,
    NullCrypto,
    PassphraseCrypto,
    generate_keyfile,
    make_crypto,
)

# Functions

SAMPLE_PLAINTEXT = b"Hello, ODDArchiver! \x00\xff\xde\xad\xbe\xef"


class TestNullCrypto:
    def test_round_trip(self):
        nc = NullCrypto()
        assert nc.decrypt(nc.encrypt(SAMPLE_PLAINTEXT)) == SAMPLE_PLAINTEXT

    def test_encrypt_is_identity(self):
        nc = NullCrypto()
        assert nc.encrypt(SAMPLE_PLAINTEXT) == SAMPLE_PLAINTEXT

    def test_decrypt_is_identity(self):
        nc = NullCrypto()
        assert nc.decrypt(SAMPLE_PLAINTEXT) == SAMPLE_PLAINTEXT


class TestPassphraseCrypto:
    def test_round_trip(self):
        pc = PassphraseCrypto(passphrase="correct horse battery staple")
        assert pc.decrypt(pc.encrypt(SAMPLE_PLAINTEXT)) == SAMPLE_PLAINTEXT

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
        with pytest.raises(ValueError):
            KeyfileCrypto(bad)

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
