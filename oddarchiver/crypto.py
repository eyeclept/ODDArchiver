"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Encryption layer. Three modes share one interface: NullCrypto (mode 0),
    PassphraseCrypto (Argon2id + ChaCha20-Poly1305), and KeyfileCrypto
    (ChaCha20-Poly1305 DEK wrapping). Small blobs use encrypt()/decrypt()
    on bytes. Files above STREAM_CHUNK_SIZE use encrypt_file()/decrypt_file()
    so the program never fully materializes plaintext or
    ciphertext. Rescue-key wrapping uses scrypt + ChaCha20-Poly1305
    so recovery of a lost passphrase or keyfile works without a
    second AEAD scheme.
"""
# Imports
from __future__ import annotations

import abc
import base64
import errno
import hashlib
import logging
import os
import secrets
import shutil
import stat
from pathlib import Path

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# Globals
PASSPHRASE_ENV = "ODDARCHIVER_PASSPHRASE"
STREAM_CHUNK_SIZE = 256 * 2**20  # 256 MiB, the AEAD chunk size and large-file threshold

logger = logging.getLogger(__name__)
_log = logger

_SALT_LEN = 16
_NONCE_LEN = 12
_KEY_LEN = 32
_DEK_CIPHERTEXT_LEN = _KEY_LEN + 16  # ChaCha20-Poly1305 tag is 16 bytes
_TAG_LEN = 16  # ChaCha20-Poly1305 authentication tag
_BOUND_MAGIC = b"ODDARCHIVER-BLOB-3\x00"


# scrypt cost for rescue-key wrapping. Runs once per init/restore, not per blob.
# n=2**14 (16 MiB memory with r=8) is enough: the rescue code is 256 bits of
# independent entropy, so KDF cost is not what stops a brute-force search.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_RESCUE_GROUP = 4
_RESCUE_AAD = b"oddarchiver-rescue-v1"


# Functions

def is_bound_ciphertext(ciphertext: bytes) -> bool:
    """Input: ciphertext bytes. Output: True for a format 3 envelope."""
    return ciphertext.startswith(_BOUND_MAGIC)


def blob_aad(entry: object) -> bytes | None:
    """Input: a manifest entry. Output: authenticated physical blob identity."""
    origin = getattr(entry, "blob_origin_session", None)
    if origin is None:
        return None
    kind = getattr(entry, "type")
    path = getattr(entry, "delta_file") if kind == "delta" else getattr(entry, "file")
    return f"oddarchiver-blob-v3\x00{origin}\x00{kind}\x00{path.rsplit('/', 1)[-1]}".encode()


def new_blob_aad(origin: int, kind: str, blob_id: str) -> bytes:
    """Input: the original session, kind, and blob name. Output: blob AAD."""
    return f"oddarchiver-blob-v3\x00{origin}\x00{kind}\x00{blob_id}".encode()


def manifest_aad(session: int) -> bytes:
    """Input: session slot. Output: manifest AAD."""
    return f"oddarchiver-manifest-v3\x00{session}".encode()


def baseline_aad(archive_id: str, volume_index: int) -> bytes:
    """Input: volume identity. Output: baseline AAD."""
    return f"oddarchiver-baseline-v3\x00{archive_id}\x00{volume_index}".encode()


class CryptoBackend(abc.ABC):
    """Abstract interface for encrypt/decrypt operations."""

    @abc.abstractmethod
    def encrypt(self, plaintext: bytes, aad: bytes | None = None) -> bytes:
        """
        Input:  plaintext - raw bytes to encrypt
        Output: ciphertext bytes (nonce or salt embedded)
        Details:
            Use authenticated encryption. decrypt() raises ValueError on
            authentication failure.
        """

    @abc.abstractmethod
    def decrypt(self, ciphertext: bytes, aad: bytes | None = None) -> bytes:
        """
        Input:  ciphertext - bytes produced by encrypt()
        Output: plaintext bytes
        Details:
            Raises ValueError on authentication failure or malformed input.
        """

    @abc.abstractmethod
    def encrypt_file(
        self, src: Path, dest: Path, offset: int = 0, length: int | None = None,
        digest: "hashlib._Hash | None" = None, aad: bytes | None = None,
    ) -> None:
        """
        Input:  src    - plaintext file
                dest   - ciphertext destination path
                offset - byte offset into src to start encrypting from
                         (default 0)
                length - number of bytes to encrypt starting at offset, or
                         None to encrypt through end-of-file (default None)
                digest - optional hashlib object. Each plaintext chunk that
                         the call reads also updates it (default None)
        Output: None (writes dest)
        Details:
            Streaming counterpart of encrypt(). A dedicated wire format,
            not the same as encrypt(bytes). Never materializes the whole
            file in memory. When offset/length select a byte range, dest
            is a complete, independently self-decryptable streamed blob
            for just that range. decrypt_file() needs no knowledge of
            the range or the total size of the source file. Omitting
            offset/length (or passing length=None) is identical to
            encrypting the whole file. With digest set, the caller gets
            the hash of the exact bytes encoded from the same read.
        """

    @abc.abstractmethod
    def decrypt_file(self, src: Path, dest: Path, aad: bytes | None = None) -> None:
        """
        Input:  src  - ciphertext file produced by encrypt_file()
                dest - plaintext destination path
        Output: None (writes dest)
        Details:
            Streaming counterpart of decrypt(). Raises ValueError on
            authentication failure or malformed input.
        """


def _copy_null_crypto_range(
    inf, outf, remaining: int | None, digest: "hashlib._Hash | None",
) -> None:
    """Input: source, destination, range length, and optional digest. Output: copied bytes."""
    while remaining is None or remaining > 0:
        read_size = (
            STREAM_CHUNK_SIZE
            if remaining is None
            else min(STREAM_CHUNK_SIZE, remaining)
        )
        chunk = inf.read(read_size)
        if not chunk:
            break
        if digest is not None:
            digest.update(chunk)
        outf.write(chunk)
        if remaining is not None:
            remaining -= len(chunk)


class NullCrypto(CryptoBackend):
    """
    Input:  None
    Output: N/A (class)
    Details:
        Identity crypto. No encryption (mode 0).
    """

    def encrypt(self, plaintext: bytes, aad: bytes | None = None) -> bytes:
        return plaintext

    def decrypt(self, ciphertext: bytes, aad: bytes | None = None) -> bytes:
        return ciphertext

    def encrypt_file(
        self, src: Path, dest: Path, offset: int = 0, length: int | None = None,
        digest: "hashlib._Hash | None" = None, aad: bytes | None = None,
    ) -> None:
        """
        Input:  src, dest, offset, length, digest - see CryptoBackend.encrypt_file
        Output: None
        Details:
            Identity crypto. The streamed format for a range is just
            that raw byte range. When the caller omits offset/length
            and digest, the whole-file copy path stays the same. A
            digest needs the chunked loop so that each chunk updates it.
        """
        if offset == 0 and length is None and digest is None:
            shutil.copy2(src, dest)
            return
        with src.open("rb") as inf, dest.open("wb") as outf:
            if offset:
                inf.seek(offset)
            _copy_null_crypto_range(inf, outf, length, digest)

    def decrypt_file(self, src: Path, dest: Path, aad: bytes | None = None) -> None:
        shutil.copy2(src, dest)


class PassphraseCrypto(CryptoBackend):
    """
    Input:  passphrase - str, bytes, or None to read from ODDARCHIVER_PASSPHRASE
    Output: N/A (class)
    Details:
        KDF: Argon2id (argon2-cffi), m=65536, t=3, p=4.
        Cipher: ChaCha20-Poly1305 (cryptography).
        Wire format: salt(16) + nonce(12) + ciphertext.
        Raises RuntimeError if no passphrase is available.
    """

    KDF_PARAMS = {"m": 65536, "t": 3, "p": 4}

    def __init__(self, passphrase: str | bytes | None = None) -> None:
        raw = passphrase or os.environ.get(PASSPHRASE_ENV)
        if not raw:
            raise RuntimeError(
                "No passphrase provided and ODDARCHIVER_PASSPHRASE is not set."
            )
        self._passphrase: bytes = raw if isinstance(raw, bytes) else raw.encode()

    def _derive_key(self, salt: bytes) -> bytes:
        """
        Input:  salt - 16-byte random salt
        Output: 32-byte derived key
        Details:
            Argon2id with KDF_PARAMS. The secret is the passphrase bytes.
        """
        return hash_secret_raw(
            secret=self._passphrase,
            salt=salt,
            time_cost=self.KDF_PARAMS["t"],
            memory_cost=self.KDF_PARAMS["m"],
            parallelism=self.KDF_PARAMS["p"],
            hash_len=_KEY_LEN,
            type=Type.ID,
        )

    def encrypt(self, plaintext: bytes, aad: bytes | None = None) -> bytes:
        salt = secrets.token_bytes(_SALT_LEN)
        nonce = secrets.token_bytes(_NONCE_LEN)
        key = self._derive_key(salt)
        ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
        return (_BOUND_MAGIC if aad is not None else b"") + salt + nonce + ciphertext

    def decrypt(self, ciphertext: bytes, aad: bytes | None = None) -> bytes:
        bound = is_bound_ciphertext(ciphertext)
        if bound != (aad is not None):
            raise ValueError("Ciphertext format does not match the authenticated manifest.")
        if bound:
            ciphertext = ciphertext[len(_BOUND_MAGIC):]
        if len(ciphertext) < _SALT_LEN + _NONCE_LEN:
            raise ValueError("Ciphertext too short.")
        salt = ciphertext[:_SALT_LEN]
        nonce = ciphertext[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
        body = ciphertext[_SALT_LEN + _NONCE_LEN :]
        key = self._derive_key(salt)
        try:
            return ChaCha20Poly1305(key).decrypt(nonce, body, aad)
        except Exception as exc:
            _log.warning("Passphrase envelope authentication failed: %s", exc)
            raise ValueError("Decryption failed: authentication error.") from exc

    def encrypt_file(
        self, src: Path, dest: Path, offset: int = 0, length: int | None = None,
        digest: "hashlib._Hash | None" = None, aad: bytes | None = None,
    ) -> None:
        """
        Input:  src, dest, offset, length, digest - see CryptoBackend.encrypt_file
        Output: None
        Details:
            Wire format: salt(16) + repeated[nonce(12) + ciphertext_chunk].
            One Argon2id derivation for the whole file or the selected
            range. Fresh nonce per chunk. A range produces its own salt, so
            the resulting blob is fully self-decryptable.
        """
        salt = secrets.token_bytes(_SALT_LEN)
        key = self._derive_key(salt)
        _encrypt_chunks(
            key, src, dest, header=(_BOUND_MAGIC if aad is not None else b"") + salt,
            offset=offset, length=length, digest=digest, aad=aad,
        )

    def decrypt_file(self, src: Path, dest: Path, aad: bytes | None = None) -> None:
        """
        Input: src and dest paths, plus optional authenticated blob identity
        Output: decrypted file
        Details: Check the format before the first streamed chunk.
        """
        with src.open("rb") as inf, dest.open("wb") as outf:
            prefix = inf.read(len(_BOUND_MAGIC))
            bound = prefix == _BOUND_MAGIC
            if bound != (aad is not None):
                raise ValueError("Ciphertext format does not match the authenticated manifest.")
            if not bound:
                inf.seek(0)
            salt = inf.read(_SALT_LEN)
            if len(salt) != _SALT_LEN:
                raise ValueError("Ciphertext too short.")
            key = self._derive_key(salt)
            _decrypt_chunks(key, inf, outf, aad=aad)


class KeyfileCrypto(CryptoBackend):
    """
    Input:  keyfile_path - path to a file that holds a 32-byte symmetric key
    Output: N/A (class)
    Details:
        Per-file DEK: generate a random 32-byte DEK, wrap it with the keyfile
        key via ChaCha20-Poly1305, and embed the encrypted DEK in the output.
        Wire format: kek_nonce(12) + enc_dek(48) + data_nonce(12) + ciphertext.
        The keyfile holds exactly 32 raw bytes (the Key Encryption Key).
        Raises ValueError on authentication failure.
    """

    def __init__(self, keyfile_path: str) -> None:
        self.keyfile_path = keyfile_path
        try:
            fd = os.open(
                keyfile_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                _log.warning("Refused symlinked keyfile %s", keyfile_path)
                raise ValueError(
                    f"Keyfile {keyfile_path} is a symlink. Use a regular file."
                ) from exc
            _log.exception("Could not open keyfile %s", keyfile_path)
            raise
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            mode = stat.S_IMODE(info.st_mode)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or mode & 0o077
            ):
                raise ValueError(
                    f"Keyfile {keyfile_path} has mode {mode:04o}. "
                    f"Use a regular file that you own. Run chmod 600 {keyfile_path}."
                )
            self._kek = fh.read()
        if len(self._kek) != _KEY_LEN:
            raise ValueError(
                f"Keyfile must contain exactly {_KEY_LEN} bytes; got {len(self._kek)}."
            )

    @classmethod
    def from_kek(cls, kek: bytes) -> "KeyfileCrypto":
        """
        Input:  kek - 32 raw key-encryption-key bytes
        Output: KeyfileCrypto keyed by kek
        Details:
            Used by the rescue-key unwrap path. Does not read a keyfile.
            The public constructor still takes a file path.
        """
        if len(kek) != _KEY_LEN:
            raise ValueError(
                f"Keyfile must contain exactly {_KEY_LEN} bytes; got {len(kek)}."
            )
        inst = object.__new__(cls)
        inst.keyfile_path = ""
        inst._kek = bytes(kek)
        return inst

    def encrypt(self, plaintext: bytes, aad: bytes | None = None) -> bytes:
        dek = secrets.token_bytes(_KEY_LEN)
        kek_nonce = secrets.token_bytes(_NONCE_LEN)
        enc_dek = ChaCha20Poly1305(self._kek).encrypt(kek_nonce, dek, None)
        data_nonce = secrets.token_bytes(_NONCE_LEN)
        enc_data = ChaCha20Poly1305(dek).encrypt(data_nonce, plaintext, aad)
        return (_BOUND_MAGIC if aad is not None else b"") + kek_nonce + enc_dek + data_nonce + enc_data

    def decrypt(self, ciphertext: bytes, aad: bytes | None = None) -> bytes:
        bound = is_bound_ciphertext(ciphertext)
        if bound != (aad is not None):
            raise ValueError("Ciphertext format does not match the authenticated manifest.")
        if bound:
            ciphertext = ciphertext[len(_BOUND_MAGIC):]
        header = _NONCE_LEN + _DEK_CIPHERTEXT_LEN
        if len(ciphertext) < header + _NONCE_LEN:
            raise ValueError("Ciphertext too short.")
        kek_nonce = ciphertext[:_NONCE_LEN]
        enc_dek = ciphertext[_NONCE_LEN : header]
        data_nonce = ciphertext[header : header + _NONCE_LEN]
        enc_data = ciphertext[header + _NONCE_LEN :]
        try:
            dek = ChaCha20Poly1305(self._kek).decrypt(kek_nonce, enc_dek, None)
            return ChaCha20Poly1305(dek).decrypt(data_nonce, enc_data, aad)
        except Exception as exc:
            _log.warning("Keyfile envelope authentication failed: %s", exc)
            raise ValueError("Decryption failed: authentication error.") from exc

    def encrypt_file(
        self, src: Path, dest: Path, offset: int = 0, length: int | None = None,
        digest: "hashlib._Hash | None" = None, aad: bytes | None = None,
    ) -> None:
        """
        Input:  src, dest, offset, length, digest - see CryptoBackend.encrypt_file
        Output: None
        Details:
            Wire format: kek_nonce(12) + enc_dek(48) +
            repeated[nonce(12) + ciphertext_chunk]. One fresh DEK per call,
            so a range gets its own wrapped DEK and is fully
            self-decryptable independent of any other range from the
            same source file.
        """
        dek = secrets.token_bytes(_KEY_LEN)
        kek_nonce = secrets.token_bytes(_NONCE_LEN)
        enc_dek = ChaCha20Poly1305(self._kek).encrypt(kek_nonce, dek, None)
        _encrypt_chunks(
            dek, src, dest,
            header=(_BOUND_MAGIC if aad is not None else b"") + kek_nonce + enc_dek,
            offset=offset, length=length, digest=digest, aad=aad,
        )

    def decrypt_file(self, src: Path, dest: Path, aad: bytes | None = None) -> None:
        """
        Input: source, destination, and optional authenticated blob identity
        Output: decrypted file
        Details: Check the envelope before unwrapping the DEK.
        """
        header_len = _NONCE_LEN + _DEK_CIPHERTEXT_LEN
        with src.open("rb") as inf, dest.open("wb") as outf:
            prefix = inf.read(len(_BOUND_MAGIC))
            bound = prefix == _BOUND_MAGIC
            if bound != (aad is not None):
                raise ValueError("Ciphertext format does not match the authenticated manifest.")
            if not bound:
                inf.seek(0)
            header = inf.read(header_len)
            if len(header) != header_len:
                raise ValueError("Ciphertext too short.")
            kek_nonce = header[:_NONCE_LEN]
            enc_dek = header[_NONCE_LEN:]
            try:
                dek = ChaCha20Poly1305(self._kek).decrypt(kek_nonce, enc_dek, None)
            except Exception as exc:
                _log.warning("DEK unwrap failed: %s", exc)
                raise ValueError("Decryption failed: authentication error.") from exc
            _decrypt_chunks(dek, inf, outf, aad=aad)


def _write_legacy_chunk(
    outf, aead: ChaCha20Poly1305, chunk: bytes, digest: "hashlib._Hash | None",
) -> None:
    """Input: ciphertext output, cipher, plaintext, and digest. Output: encrypted frame."""
    if digest is not None:
        digest.update(chunk)
    nonce = secrets.token_bytes(_NONCE_LEN)
    outf.write(nonce + aead.encrypt(nonce, chunk, None))


def _read_legacy_chunk(inf, remaining: int | None) -> bytes:
    """Input: plaintext source and remaining range length. Output: next plaintext chunk."""
    if remaining is not None and remaining <= 0:
        return b""
    read_size = (
        STREAM_CHUNK_SIZE
        if remaining is None
        else min(STREAM_CHUNK_SIZE, remaining)
    )
    return inf.read(read_size)


def _encrypt_chunks(
    key: bytes,
    src: Path,
    dest: Path,
    header: bytes,
    offset: int = 0,
    length: int | None = None,
    digest: "hashlib._Hash | None" = None,
    aad: bytes | None = None,
) -> None:
    """
    Input:  key    - 32-byte ChaCha20-Poly1305 key
            src    - plaintext file
            dest   - ciphertext destination
            header - bytes written before the first chunk
            offset - byte offset into src to start reading from (default 0)
            length - number of bytes to encrypt starting at offset, or None
                     to encrypt through end-of-file (default None)
            digest - optional hashlib object that each plaintext chunk
                     updates (default None)
    Output: None
    Details:
        Writes header, then nonce+ciphertext for each STREAM_CHUNK_SIZE
        plaintext sub-chunk drawn from src[offset:offset+length]. A
        zero-byte range (source or explicit length=0) writes header plus
        one empty plaintext chunk so the file is well-formed and
        round-trips. A range that is an exact multiple of
        STREAM_CHUNK_SIZE produces no trailing empty chunk. offset=0,
        length=None reproduces the original whole-file behavior exactly.
    """
    aead = ChaCha20Poly1305(key)
    if aad is not None:
        _encrypt_bound_chunks(key, src, dest, header, offset, length, digest, aad)
        return
    with src.open("rb") as inf, dest.open("wb") as outf:
        if offset:
            inf.seek(offset)
        outf.write(header)
        first = True
        remaining = length
        while True:
            chunk = _read_legacy_chunk(inf, remaining)
            if not chunk:
                if first:
                    nonce = secrets.token_bytes(_NONCE_LEN)
                    outf.write(nonce + aead.encrypt(nonce, b"", None))
                break
            first = False
            _write_legacy_chunk(outf, aead, chunk, digest)
            remaining = remaining - len(chunk) if remaining is not None else None

def _write_bound_data_frame(
    inf, outf, aead: ChaCha20Poly1305, size: int,
    digest: "hashlib._Hash | None", aad: bytes, index: int,
) -> int:
    """Input: streams, cipher, read size, digest, AAD, and index. Output: bytes framed."""
    chunk = inf.read(size)
    if not chunk:
        return 0
    if digest is not None:
        digest.update(chunk)
    nonce = secrets.token_bytes(_NONCE_LEN)
    body = aead.encrypt(nonce, chunk, aad + index.to_bytes(8, "big") + b"\x00")
    outf.write(len(body).to_bytes(4, "big") + b"\x00" + nonce + body)
    return len(chunk)


def _encrypt_bound_chunks(
    key: bytes, src: Path, dest: Path, header: bytes, offset: int,
    length: int | None, digest: "hashlib._Hash | None", aad: bytes,
) -> None:
    """
    Input: key, source, destination, header, range, digest, and blob AAD
    Output: framed stream with one authenticated final empty chunk
    Details: The final frame marks the end even at an exact chunk boundary.
    """
    aead = ChaCha20Poly1305(key)
    with src.open("rb") as inf, dest.open("wb") as outf:
        inf.seek(offset)
        outf.write(header)
        remaining = length
        index = 0
        while remaining is None or remaining > 0:
            size = STREAM_CHUNK_SIZE if remaining is None else min(remaining, STREAM_CHUNK_SIZE)
            chunk_size = _write_bound_data_frame(inf, outf, aead, size, digest, aad, index)
            if not chunk_size:
                break
            index += 1
            if remaining is not None:
                remaining -= chunk_size
        nonce = secrets.token_bytes(_NONCE_LEN)
        body = aead.encrypt(nonce, b"", aad + index.to_bytes(8, "big") + b"\x01")
        outf.write(len(body).to_bytes(4, "big") + b"\x01" + nonce + body)


def _decrypt_chunks(key: bytes, inf, outf, aad: bytes | None = None) -> None:
    """
    Input:  key  - 32-byte ChaCha20-Poly1305 key
            inf  - binary file positioned after the streaming header
            outf - binary file for plaintext
    Output: None
    Details:
        Loop: read a 12-byte nonce. An empty read means EOF.
        Read up to STREAM_CHUNK_SIZE+tag bytes of ciphertext.
        Decrypt it and write the plaintext.
        Auth failure becomes ValueError, matching decrypt().
    """
    aead = ChaCha20Poly1305(key)
    if aad is not None:
        _decrypt_bound_chunks(key, inf, outf, aad)
        return
    ct_max = STREAM_CHUNK_SIZE + _TAG_LEN
    while True:
        nonce = inf.read(_NONCE_LEN)
        if not nonce:
            return
        if len(nonce) != _NONCE_LEN:
            raise ValueError("Ciphertext too short.")
        ciphertext = inf.read(ct_max)
        if not ciphertext:
            raise ValueError("Ciphertext too short.")
        try:
            plaintext = aead.decrypt(nonce, ciphertext, None)
        except Exception as exc:
            logger.exception("Chunk decrypt failed")
            raise ValueError("Decryption failed: authentication error.") from exc
        outf.write(plaintext)

def _read_bound_frame(
    inf, aead: ChaCha20Poly1305, aad: bytes, index: int,
) -> tuple[int, bytes]:
    """Input: ciphertext source, cipher, AAD, and index. Output: flag and plaintext."""
    frame = inf.read(5)
    if len(frame) != 5:
        raise ValueError("Stream has no complete final chunk.")
    size = int.from_bytes(frame[:4], "big")
    flag = frame[4]
    if flag not in (0, 1) or not _TAG_LEN <= size <= STREAM_CHUNK_SIZE + _TAG_LEN:
        raise ValueError("Invalid stream chunk frame.")
    nonce = inf.read(_NONCE_LEN)
    body = inf.read(size)
    if len(nonce) != _NONCE_LEN or len(body) != size:
        raise ValueError("Ciphertext too short.")
    try:
        plain = aead.decrypt(nonce, body, aad + index.to_bytes(8, "big") + bytes((flag,)))
    except Exception as exc:
        _log.warning("Stream chunk authentication failed: %s", exc)
        raise ValueError("Decryption failed: authentication error.") from exc
    return flag, plain


def _decrypt_bound_chunks(key: bytes, inf, outf, aad: bytes) -> None:
    """
    Input: key, framed ciphertext stream, plaintext output, and blob AAD
    Output: plaintext only when frames end with the final frame
    Details: The frame sequence authenticates index and end-of-stream.
    """
    aead = ChaCha20Poly1305(key)
    index = 0
    while True:
        flag, plain = _read_bound_frame(inf, aead, aad, index)
        if flag == 1:
            if plain or inf.read(1):
                raise ValueError("Data follows the final stream chunk.")
            return
        if not plain:
            raise ValueError("Empty non-final stream chunk.")
        outf.write(plain)
        index += 1


def generate_keyfile(path: str) -> None:
    """
    Input:  path - destination file path
    Output: None (writes file)
    Details:
        Create a private 32-byte keyfile. Refuse an existing path.
    """
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
        )
    except FileExistsError:
        _log.warning("Keyfile already exists: %s", path)
        raise FileExistsError(f"Keyfile {path} already exists. Refuse to overwrite it.")
    try:
        try:
            key = secrets.token_bytes(_KEY_LEN)
            offset = 0
            while offset < len(key):
                written = os.write(fd, key[offset:])
                if written == 0:
                    raise OSError(f"Could not write keyfile {path}.")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        _log.exception("Could not create keyfile %s", path)
        try:
            os.unlink(path)
        except OSError:
            _log.exception("Could not remove partial keyfile %s", path)
            raise
        raise


def generate_rescue_code() -> tuple[str, bytes]:
    """
    Input:  None
    Output: (printable code, raw 32-byte secret)
    Details:
        RFC 4648 base32, no padding, uppercase, grouped in 4-character
        chunks with hyphens. 32 bytes become 52 characters, 13 groups.
    """
    raw = secrets.token_bytes(_KEY_LEN)
    encoded = base64.b32encode(raw).decode("ascii").rstrip("=")
    grouped = "-".join(
        encoded[i:i + _RESCUE_GROUP]
        for i in range(0, len(encoded), _RESCUE_GROUP)
    )
    return grouped, raw


def parse_rescue_code(code: str) -> bytes:
    """
    Input:  code - printable rescue code (hyphens and case optional)
    Output: 32 raw bytes
    Details:
        Invalid encoding raises ValueError with the same authentication
        error text as a wrong passphrase, so a typo is not a distinct path.
    """
    compact = "".join(ch for ch in code.upper() if ch.isalnum())
    pad_len = (8 - len(compact) % 8) % 8
    try:
        raw = base64.b32decode(compact + ("=" * pad_len), casefold=True)
    except Exception as exc:
        logger.exception("Rescue code decode failed")
        raise ValueError("Decryption failed: authentication error.") from exc
    if len(raw) != _KEY_LEN:
        raise ValueError("Decryption failed: authentication error.")
    return raw


def wrap_rescue_secret(rescue_raw: bytes, secret: bytes) -> dict:
    """
    Input:  rescue_raw - 32-byte rescue code
            secret     - passphrase bytes or keyfile KEK to escrow
    Output: dict with base64 wrapped blob, salt, and scrypt cost parameters
    Details:
        Derives a ChaCha20-Poly1305 key via scrypt (n=2**14, r=8, p=1)
        from the rescue code. The wrapped blob is nonce || ciphertext.
    """
    salt = secrets.token_bytes(_SALT_LEN)
    kdf = Scrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    key = kdf.derive(rescue_raw)
    nonce = secrets.token_bytes(_NONCE_LEN)
    wrapped = ChaCha20Poly1305(key).encrypt(nonce, secret, _RESCUE_AAD)
    return {
        "wrapped": base64.b64encode(nonce + wrapped).decode("ascii"),
        "salt": base64.b64encode(salt).decode("ascii"),
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
    }


def unwrap_rescue_secret(rescue_code: str, rescue_block: dict) -> bytes:
    """
    Input:  rescue_code  - printable rescue code
            rescue_block - dict stored in enc_mode.json under "rescue"
    Output: original passphrase bytes or keyfile KEK
    Details:
        Wrong code or a malformed blob raises ValueError with the same
        authentication error text as PassphraseCrypto.decrypt.
    """
    rescue_raw = parse_rescue_code(rescue_code)
    try:
        wrapped_b64 = rescue_block["wrapped"]
        salt_b64 = rescue_block["salt"]
        n = int(rescue_block["n"])
        r = int(rescue_block["r"])
        p = int(rescue_block["p"])
        blob = base64.b64decode(wrapped_b64)
        salt = base64.b64decode(salt_b64)
    except Exception as exc:
        logger.exception("Malformed rescue block")
        raise ValueError("Decryption failed: authentication error.") from exc
    if (
        n != _SCRYPT_N
        or r != _SCRYPT_R
        or p != _SCRYPT_P
        or len(blob) < _NONCE_LEN
        or len(salt) != _SALT_LEN
    ):
        _log.warning("Invalid rescue block parameters or lengths")
        raise ValueError("Decryption failed: authentication error.")
    kdf = Scrypt(
        salt=salt, length=_KEY_LEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
    )
    key = kdf.derive(rescue_raw)
    nonce = blob[:_NONCE_LEN]
    body = blob[_NONCE_LEN:]
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, body, _RESCUE_AAD)
    except Exception as exc:
        logger.exception("Rescue-key unwrap failed")
        raise ValueError("Decryption failed: authentication error.") from exc


def make_crypto(mode: str, **kwargs: object) -> CryptoBackend:
    """
    Input:  mode   - "none" | "passphrase" | "keyfile"
            kwargs - forwarded to the matching CryptoBackend constructor
    Output: CryptoBackend instance matching the requested mode
    Details:
        Factory. Raises ValueError on an unknown mode.
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
