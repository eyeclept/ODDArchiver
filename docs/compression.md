# Blob compression

The writer compresses each new or changed blob with LZMA (xz, preset 6)
before encryption. Compression is on by default. When a payload does not
shrink below 95 percent of the source size, the archive stores the source
bytes and the manifest records that choice.

## Manifest fields

Two optional fields on each entry describe the stored payload.

| Field | Meaning |
|---|---|
| `codec` | `lzma` or `none`. A missing field means `none`. |
| `original_size` | Plaintext size that the entry restores. |

The writer omits both fields when `codec` is `none`. A manifest written
before compression therefore loads and verifies without changes.

## Split files

The writer splits a file above the 3 GiB blob cap or the volume budget.

1. Compress the whole file.
2. Cut the compressed payload into chunk lengths.
3. Encrypt each slice with its own key material.

This step order gives the same on-disc shape as before compression: one
ciphertext per chunk.

Restore decrypts each chunk and appends the plaintext slices. It decodes
once after the last chunk. Verify runs the same steps before it hashes
the file.

The codec and size must match the entry. The plaintext hash must match
`result_checksum`.

## Dedup and deltas

A dedup entry shares the stored bytes of another entry. It therefore
carries the `codec` and `original_size` of that entry. A same-run dedup
reference inherits both after all entries exist.

A delta entry compresses the delta stream. Restore decodes the delta
stream before it applies the delta to the base file.

## What to expect

| Content | Stored size |
|---|---|
| Text, source, logs, JSON | 3 to 10 times smaller |
| PDF and office files | 5 to 15 percent smaller |
| JPEG, ISO images, model weights, random data | Unchanged, stored raw |

Compression costs CPU. At preset 6, plan on roughly 5 to 10 MB per second
per core. A 3 GiB chunk needs about 10 minutes.
