"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Encryption layer. Three modes share one interface: NullCrypto (mode 0),
    PassphraseCrypto (Argon2id + ChaCha20-Poly1305), KeyfileCrypto (X25519 DEK wrapping).
    Plaintext never written to filesystem; all operations work on bytes in memory.
"""
# Imports
from __future__ import annotations

import abc
import os
import secrets

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

# Globals
PASSPHRASE_ENV = "ODDARCHIVER_PASSPHRASE"

_SALT_LEN = 16
_NONCE_LEN = 12
_KEY_LEN = 32
_DEK_CIPHERTEXT_LEN = _KEY_LEN + 16  # ChaCha20-Poly1305 tag is 16 bytes


# Functions


class CryptoBackend(abc.ABC):
    """Abstract interface for encrypt/decrypt operations."""

    @abc.abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes:
        """
        Input:  plaintext — raw bytes to encrypt
        Output: ciphertext bytes (nonce/salt embedded)
        Details:
            Must be authenticated encryption; raises ValueError on auth failure in decrypt.
        """

    @abc.abstractmethod
    def decrypt(self, ciphertext: bytes) -> bytes:
        """
        Input:  ciphertext — bytes produced by encrypt()
        Output: plaintext bytes
        Details:
            Raises ValueError on authentication failure or malformed input.
        """


class NullCrypto(CryptoBackend):
    """
    Input:  None
    Output: N/A (class)
    Details:
        Identity crypto — no encryption (mode 0).
    """

    def encrypt(self, plaintext: bytes) -> bytes:
        return plaintext

    def decrypt(self, ciphertext: bytes) -> bytes:
        return ciphertext


class PassphraseCrypto(CryptoBackend):
    """
    Input:  passphrase — str, or None to read from ODDARCHIVER_PASSPHRASE env var
    Output: N/A (class)
    Details:
        KDF: Argon2id (argon2-cffi), m=65536, t=3, p=4.
        Cipher: ChaCha20-Poly1305 (cryptography).
        Wire format: salt(16) + nonce(12) + ciphertext.
        Raises RuntimeError if no passphrase is available.
    """

    KDF_PARAMS = {"m": 65536, "t": 3, "p": 4}

    def __init__(self, passphrase: str | None = None) -> None:
        self._passphrase = passphrase or os.environ.get(PASSPHRASE_ENV)
        if not self._passphrase:
            raise RuntimeError(
                "No passphrase provided and ODDARCHIVER_PASSPHRASE is not set."
            )

    def _derive_key(self, salt: bytes) -> bytes:
        """
        Input:  salt — 16-byte random salt
        Output: 32-byte derived key
        Details:
            Argon2id with KDF_PARAMS; secret is passphrase UTF-8 encoded.
        """
        return hash_secret_raw(
            secret=self._passphrase.encode(),
            salt=salt,
            time_cost=self.KDF_PARAMS["t"],
            memory_cost=self.KDF_PARAMS["m"],
            parallelism=self.KDF_PARAMS["p"],
            hash_len=_KEY_LEN,
            type=Type.ID,
        )

    def encrypt(self, plaintext: bytes) -> bytes:
        salt = secrets.token_bytes(_SALT_LEN)
        nonce = secrets.token_bytes(_NONCE_LEN)
        key = self._derive_key(salt)
        ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, None)
        return salt + nonce + ciphertext

    def decrypt(self, ciphertext: bytes) -> bytes:
        if len(ciphertext) < _SALT_LEN + _NONCE_LEN:
            raise ValueError("Ciphertext too short.")
        salt = ciphertext[:_SALT_LEN]
        nonce = ciphertext[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
        body = ciphertext[_SALT_LEN + _NONCE_LEN :]
        key = self._derive_key(salt)
        try:
            return ChaCha20Poly1305(key).decrypt(nonce, body, None)
        except Exception as exc:
            raise ValueError("Decryption failed: authentication error.") from exc


class KeyfileCrypto(CryptoBackend):
    """
    Input:  keyfile_path — path to a file containing a 32-byte symmetric key (raw bytes)
    Output: N/A (class)
    Details:
        Per-file DEK: generate random 32-byte DEK, wrap it with keyfile key via
        ChaCha20-Poly1305, embed encrypted DEK in output.
        Wire format: kek_nonce(12) + enc_dek(48) + data_nonce(12) + ciphertext.
        The keyfile holds exactly 32 raw bytes (the Key Encryption Key).
        Raises ValueError on authentication failure.
    """

    def __init__(self, keyfile_path: str) -> None:
        self.keyfile_path = keyfile_path
        with open(keyfile_path, "rb") as fh:
            self._kek = fh.read()
        if len(self._kek) != _KEY_LEN:
            raise ValueError(
                f"Keyfile must contain exactly {_KEY_LEN} bytes; got {len(self._kek)}."
            )

    def encrypt(self, plaintext: bytes) -> bytes:
        dek = secrets.token_bytes(_KEY_LEN)
        kek_nonce = secrets.token_bytes(_NONCE_LEN)
        enc_dek = ChaCha20Poly1305(self._kek).encrypt(kek_nonce, dek, None)
        data_nonce = secrets.token_bytes(_NONCE_LEN)
        enc_data = ChaCha20Poly1305(dek).encrypt(data_nonce, plaintext, None)
        return kek_nonce + enc_dek + data_nonce + enc_data

    def decrypt(self, ciphertext: bytes) -> bytes:
        header = _NONCE_LEN + _DEK_CIPHERTEXT_LEN
        if len(ciphertext) < header + _NONCE_LEN:
            raise ValueError("Ciphertext too short.")
        kek_nonce = ciphertext[:_NONCE_LEN]
        enc_dek = ciphertext[_NONCE_LEN : header]
        data_nonce = ciphertext[header : header + _NONCE_LEN]
        enc_data = ciphertext[header + _NONCE_LEN :]
        try:
            dek = ChaCha20Poly1305(self._kek).decrypt(kek_nonce, enc_dek, None)
            return ChaCha20Poly1305(dek).decrypt(data_nonce, enc_data, None)
        except Exception as exc:
            raise ValueError("Decryption failed: authentication error.") from exc


def generate_keyfile(path: str) -> None:
    """
    Input:  path — destination file path
    Output: None (writes file)
    Details:
        Writes 32 cryptographically random bytes to path. Used for test key generation.
    """
    with open(path, "wb") as fh:
        fh.write(secrets.token_bytes(_KEY_LEN))


def make_crypto(mode: str, **kwargs: object) -> CryptoBackend:
    """
    Input:  mode   — "none" | "passphrase" | "keyfile"
            kwargs — forwarded to the appropriate CryptoBackend constructor
    Output: CryptoBackend instance matching the requested mode
    Details:
        Factory; raises ValueError on unknown mode.
    """
    if mode == "none":
        return NullCrypto()
    if mode == "passphrase":
        return PassphraseCrypto(**kwargs)  # type: ignore[arg-type]
    if mode == "keyfile":
        return KeyfileCrypto(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"Unknown encryption mode: {mode!r}")


if __name__ == "__main__":
    pass
