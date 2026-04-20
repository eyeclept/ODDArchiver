"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Encryption layer. Three modes share one interface: NullCrypto (mode 0),
    PassphraseCrypto (Argon2id + ChaCha20-Poly1305), KeyfileCrypto (age format).
    Plaintext never written to filesystem; all operations work on bytes in memory.
"""
# Imports
from __future__ import annotations

import abc
import os

# Globals
PASSPHRASE_ENV = "ODDARCHIVER_PASSPHRASE"

# Functions


class CryptoBackend(abc.ABC):
    """Abstract interface for encrypt/decrypt operations."""

    @abc.abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes:
        """
        Input:  plaintext — raw bytes to encrypt
        Output: ciphertext bytes
        Details:
            Must be authenticated encryption; nonce/salt embedded in output.
        """

    @abc.abstractmethod
    def decrypt(self, ciphertext: bytes) -> bytes:
        """
        Input:  ciphertext — bytes produced by encrypt()
        Output: plaintext bytes
        Details:
            Raises ValueError on authentication failure.
        """


class NullCrypto(CryptoBackend):
    """
    Input:  None
    Output: N/A (class)
    Details:
        Identity crypto — no encryption (mode 0).
        encrypt() and decrypt() return their input unchanged.
    """

    def encrypt(self, plaintext: bytes) -> bytes:
        return plaintext

    def decrypt(self, ciphertext: bytes) -> bytes:
        return ciphertext


class PassphraseCrypto(CryptoBackend):
    """
    Input:  passphrase — str, or None to read from ODDARCHIVER_PASSPHRASE env var
            salt       — bytes salt for Argon2id KDF; generated fresh if None
    Output: N/A (class)
    Details:
        KDF: Argon2id (argon2-cffi). Cipher: ChaCha20-Poly1305.
        Requires [encrypt] extra: argon2-cffi.
    """

    KDF_PARAMS = {"m": 65536, "t": 3, "p": 4}

    def __init__(self, passphrase: str | None = None, salt: bytes | None = None) -> None:
        self._passphrase = passphrase or os.environ.get(PASSPHRASE_ENV)
        self._salt = salt

    def encrypt(self, plaintext: bytes) -> bytes:
        raise NotImplementedError

    def decrypt(self, ciphertext: bytes) -> bytes:
        raise NotImplementedError


class KeyfileCrypto(CryptoBackend):
    """
    Input:  keyfile_path — path to age-format keyfile
    Output: N/A (class)
    Details:
        Per-file DEK encrypted with age key; encrypted_dek stored in manifest entry.
        Requires [encrypt] extra: pyage or age CLI subprocess.
    """

    def __init__(self, keyfile_path: str) -> None:
        self.keyfile_path = keyfile_path

    def encrypt(self, plaintext: bytes) -> bytes:
        raise NotImplementedError

    def decrypt(self, ciphertext: bytes) -> bytes:
        raise NotImplementedError


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
