# Rescue-key recovery

Passphrase mode and keyfile mode each use one secret to lock the archive.
In `PassphraseCrypto`, the passphrase is the only durable secret.
In `KeyfileCrypto`, the 32-byte keyfile is the only durable secret.
If you lose that secret, every archive volume remains ciphertext.

A multi-volume archive makes a lost keyfile more serious. One lost keyfile
locks every disc, not just one disc. The rescue key gives you a second way
to recover the primary secret. It does not bypass ChaCha20-Poly1305 on the blobs.

## What init prints

`init --encrypt passphrase` and `init --encrypt keyfile` generate a 256-bit
code with `secrets.token_bytes(32)`. `--encrypt none` skips code generation.
It has no secret to wrap.

The code uses RFC 4648 base32 without padding and in uppercase.
Hyphens split it into 4-character chunks (13 groups, 52 characters).
Example shape: `MFRG-GZDF-MY7I-...` (the real value is random each init).

After a successful burn, `init` prints the code once and warns you to store
it offline. The process then drops it. This tool does not write the code to
the archive, cache, ledger, or log.

Store the printed line on paper or in a password manager.
Do not keep the rescue key in the same store as the primary passphrase or keyfile.

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

In passphrase mode, the wrapped bytes are `PassphraseCrypto._passphrase`.
In keyfile mode, the wrapped bytes are `KeyfileCrypto._kek` (the 32 raw
bytes from the keyfile). After unwrap, restore builds the same backend
that the primary secret would build. It then decrypts blobs as usual.

Plumbing: `_make_init_crypto` attaches `_rescue_block` and `_rescue_code`
to the crypto object. `_encryption_block` copies `_rescue_block` into the
manifest `encryption` dict. `write_manifest` writes it to
`enc_mode.json`. `_patch_manifest` stays the same.

## Why scrypt, not Argon2id

The primary passphrase path derives a per-blob key with Argon2id
(`m=65536`, `t=3`, `p=4`). Each ciphertext embeds its own random salt.
The rescue path derives the wrapping key from the 256-bit rescue code with
scrypt (`n=2**14`, `r=8`, `p=1`).

The two paths use different KDFs on purpose. A bug or weak parameter
set in one KDF does not also open the other path. The wrapping cipher is
still ChaCha20-Poly1305, the same AEAD this file uses elsewhere.

scrypt cost is modest because the rescue code has 256 bits of independent
entropy. Brute force is not a practical attack on that input. The KDF
runs once per init and once per rescue restore, not per blob.

## Restore with the rescue key

```sh
oddarchiver restore /dest --test-iso archive.iso --rescue-key
```

If stdin is a terminal, enter the code at the hidden `Rescue key: ` prompt.
If stdin is not a terminal, the command reads one line from stdin.
You can pipe the code into the command. Do not put the code in command arguments.
Init still confirms a new passphrase twice. Restore does not.

`--rescue-key` does not need the original passphrase or `--key`. A wrong
code fails the same way as a wrong passphrase. It raises `ValueError` from
the AEAD unwrap (`Decryption failed: authentication error.`).

An archive with no `"rescue"` object (created before this feature, or
`--encrypt none`) fails with a specific error:

```
this archive has no rescue key; it predates this feature
```

That is not a generic decrypt failure.

## If you lose both

The rescue key recovers the primary secret. It does not replace blob
encryption. If you lose both the passphrase or keyfile and the rescue key,
the archive is unrecoverable by design. This tool has no back door.
