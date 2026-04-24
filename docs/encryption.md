# Encryption

ODDArchiver supports three encryption modes. The mode is chosen at `init` time and read from the disc manifest on all subsequent commands.

## Modes

| Mode | Class | Description |
|---|---|---|
| `none` | `NullCrypto` | No encryption; plaintext stored as-is |
| `passphrase` | `PassphraseCrypto` | Argon2id KDF + ChaCha20-Poly1305 |
| `keyfile` | `KeyfileCrypto` | Per-file DEK wrapped with a 32-byte keyfile key |

Select at init: `oddarchiver init --encrypt passphrase` or `--encrypt keyfile --key /path/to/keyfile`.

## NullCrypto (mode 0)

Identity transform. `encrypt(b)` returns `b`; `decrypt(b)` returns `b`. Use for archival media where confidentiality is not required.

## PassphraseCrypto

**Dependencies:** `argon2-cffi`, `cryptography`

**KDF:** Argon2id with parameters `m=65536, t=3, p=4`, output length 32 bytes.

**Cipher:** ChaCha20-Poly1305 (authenticated encryption).

**Wire format:** `salt(16) | nonce(12) | ciphertext`

The 16-byte salt is generated fresh per encrypt call. The Argon2id KDF derives a 32-byte key from the passphrase and salt. The 12-byte nonce is random per call.

**Passphrase source:** CLI prompt (not yet wired) or environment variable `ODDARCHIVER_PASSPHRASE`.

```
export ODDARCHIVER_PASSPHRASE="correct horse battery staple"
oddarchiver init --device /dev/sr0 --encrypt passphrase /path/to/source
```

If neither the argument nor the env var is set, `PassphraseCrypto()` raises `RuntimeError`.

## KeyfileCrypto

**Dependencies:** `cryptography`

**Keyfile format:** exactly 32 raw bytes (the Key Encryption Key, KEK). Generate with:

```
python3 -c "
from oddarchiver.crypto import generate_keyfile
generate_keyfile('/path/to/keyfile.bin')
"
```

**Per-file DEK:** a random 32-byte Data Encryption Key is generated for each `encrypt()` call. The DEK is wrapped (ChaCha20-Poly1305) with the KEK and embedded in the output.

**Wire format:** `kek_nonce(12) | enc_dek(48) | data_nonce(12) | ciphertext`

- `kek_nonce`: random nonce used to wrap the DEK
- `enc_dek`: ChaCha20-Poly1305 encryption of the 32-byte DEK (32 bytes data + 16 byte tag = 48 bytes)
- `data_nonce`: random nonce used for the file payload
- `ciphertext`: ChaCha20-Poly1305 encryption of the plaintext

**Argon2id parameters:**

| Parameter | Value |
|---|---|
| Memory (m) | 65536 KiB (64 MiB) |
| Iterations (t) | 3 |
| Parallelism (p) | 4 |
| Hash length | 32 bytes |

These are tuned for interactive use (sub-second on modern hardware). Increase `t` or `m` for higher security at the cost of initialization time.

## In-memory guarantee

All encrypt and decrypt operations work on `bytes` in memory. No plaintext is written to the filesystem or to temporary files at any point.
