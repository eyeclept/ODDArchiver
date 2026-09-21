# Rescue-key recovery

Passphrase mode and keyfile mode each lock the archive with one secret.
The passphrase is the only durable secret in `PassphraseCrypto`.
The 32-byte keyfile is the only durable secret in `KeyfileCrypto`.
Lose that secret and every volume of the archive stays ciphertext.

A multi-volume archive makes that worse: one lost keyfile locks every disc,
not one disc. The rescue key is a second way to recover the primary secret.
It is not a bypass of ChaCha20-Poly1305 on the blobs.

## What init prints

`init --encrypt passphrase` and `init --encrypt keyfile` generate a 256-bit
code with `secrets.token_bytes(32)`. `--encrypt none` skips this. There is
no secret to wrap.

The code uses RFC 4648 base32, with no padding, in uppercase.
Hyphens split it into 4-character chunks (13 groups, 52 characters).
Example shape: `MFRG-GZDF-MY7I-...` (the real value is random each init).

`init` prints that code once, after a successful burn, with a warning to
store it offline. The process then drops it. Nothing in this tool writes
the code to the archive, the cache, the ledger, or the log.

Store the printed line on paper or in a password manager.
Do not keep it in the same store as the primary passphrase or keyfile.

## What the disc holds

`session_000/enc_mode.json` is plaintext on purpose so later commands can
read the mode without the primary secret. Encrypted init now also stores
a `"rescue"` object there:

```json
{
  "mode": "passphrase",
  "rescue": {
    "wrapped": "<base64 nonce||ciphertext>",
    "salt": "<base64 16-byte salt>",
    "n": 16384,
    "r": 8,
    "p": 1
  }
}
```

For passphrase mode the wrapped bytes are `PassphraseCrypto._passphrase`.
For keyfile mode the wrapped bytes are `KeyfileCrypto._kek` (the 32 raw
bytes from the keyfile). After unwrap, restore builds the same backend
the primary secret would have built and decrypts blobs as usual.

Plumbing: `_make_init_crypto` hangs `_rescue_block` and `_rescue_code` on
the crypto object. `_encryption_block` copies `_rescue_block` into the
manifest `encryption` dict. `write_manifest` writes it onto
`enc_mode.json`. `_patch_manifest` is unchanged.

## Why scrypt, not Argon2id

The primary passphrase path derives a per-blob key with Argon2id
(`m=65536`, `t=3`, `p=4`). Each ciphertext embeds its own random salt.
The rescue path derives the wrapping key with scrypt (`n=2**14`, `r=8`,
`p=1`) from the 256-bit rescue code.

The two paths use different KDFs on purpose. A bug or a weak parameter
set in one KDF does not also open the other path. The wrapping cipher is
still ChaCha20-Poly1305, the same AEAD the rest of this file uses.

scrypt cost is modest because the rescue code is 256 bits of independent
entropy. Brute force is not a practical attack on that input. The KDF
still runs once per init and once per rescue restore, not per blob.

## Restore with the rescue key

```
oddarchiver restore /dest --test-iso archive.iso --rescue-key MFRG-GZDF-...
```

Omit the code to get a single `getpass` prompt (`Rescue key:`), the same
single-prompt style restore uses for a passphrase. Init still confirms a
new passphrase twice. Restore does not.

`--rescue-key` does not need the original passphrase or `--key`. A wrong
code fails the same way a wrong passphrase fails: `ValueError` from the
AEAD unwrap (`Decryption failed: authentication error.`).

An archive with no `"rescue"` object (created before this feature, or
`--encrypt none`) fails with a specific error:

```
this archive has no rescue key; it predates this feature
```

That is not a generic decrypt failure.

## If you lose both

The rescue key recovers the primary secret. It does not replace blob
encryption. If you lose the passphrase or keyfile *and* the rescue key,
the archive is unrecoverable by design. This tool has no back door.
