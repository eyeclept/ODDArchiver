# Encrypted disc metadata

This page describes how the program reads metadata files on an
encrypted archive. It covers on-disc format 2 and older discs.

## Files on an encrypted disc

Each session holds `manifest.enc` and `enc_mode.json`. The manifest is
encrypted. `enc_mode.json` is plaintext. It holds only the mode
(`passphrase` or `keyfile`) and an optional rescue block. See
[rescue_key.md](rescue_key.md).

Each disc after volume 0 also holds `volume_baseline.enc`. Every disc
holds a plaintext `volume.json` marker. See [volumes.md](volumes.md).

## Session manifest version 3

New sessions use manifest version 3. The encrypted manifest binds its bytes to
the session slot. The reader checks the slot before it accepts the manifest.
The manifest checksum covers its entries and any baseline checksum.

Each new blob name uses a random 64-character hexadecimal ID. Chunked blob
names add the `_i` suffix. The manifest records the blob path and its original
session. Deduplicated entries keep that identity
when they reuse a blob. A new encrypted blob binds its type, name, and original
session to its ciphertext. A copied blob keeps this identity across the cache
and volumes.

New streamed blobs bind each frame to its index and to its final-frame flag.
The reader requires a final frame. It refuses missing, repeated, reordered,
or extra frames.

A new encrypted volume baseline binds its archive ID and volume index to its
ciphertext. The first session on the volume records the baseline SHA-256 in
its authenticated manifest. The reader checks this hash before it reads the
baseline. This check also stops a swapped legacy baseline.

The reader still accepts legacy manifests and blobs in their original format.
It uses the authenticated manifest version to select the checks. A new
manifest cannot use a legacy ciphertext envelope. A new session can refer to
a legacy blob on an older disc without changing that blob.

## Mode detection

`cli._crypto_for_disc` reads `enc_mode.json` of the first session on the
disc. The file must be a JSON object with a `mode` of `none`,
`passphrase`, or `keyfile`. Any other content is an error. The command
stops and names the file. It does not continue as an unencrypted disc.

A disc without `enc_mode.json` cannot get its mode from `manifest.json`.
Encrypted metadata shows that the disc is encrypted. The program uses
the local disc record to select the mode when the record exists.

The program refuses an encryption downgrade:

1. You supplied `--key`, `--rescue-key`, or `ODDARCHIVER_PASSPHRASE`, and
   the disc reports mode `none` or has no mode. The command stops.
2. The disc reports mode `none` but it holds `manifest.enc` or
   `volume_baseline.enc`. The command stops.
3. `restore --all-disks` reads `encrypted` from the catalog. If that
   field is true and the acquired disc reports mode `none`, the command
   refuses the disc.
4. The local disc record `archive-<id>.json` stores the encryption mode
   after a successful encrypted manifest read, a plaintext `init`, or
   interactive plaintext confirmation. A later disc with a different
   mode stops. The error names the record. Move the record aside only
   if you trust the disc.
5. `sync` does not write plaintext onto a disc whose memory or catalog
   says the archive is encrypted.

## Format 2 discs

New discs use format 2. A disc's baseline uses format 2 when
`volume.json` has `"format": 2`. It also uses format 2 when the disc's
first-session `manifest.enc` has a valid version 2 manifest. Later
sessions do not change the baseline format. If the first-session
manifest is missing or SUSPECT, the reader logs a warning and uses
`volume.json` alone to decide the baseline format.

For manifests, the program refuses every plaintext `manifest.json` when
it reads with a real key. This applies to old and new encrypted discs.
The error names the plaintext file. A plaintext archive still uses
`manifest.json`.

On an encrypted format 2 baseline, the program refuses
`volume_baseline.json`. The error names the file.

## Older encrypted discs

Old encrypted discs (format 1) can have a version 1 plaintext
`volume_baseline.json` with only `state` and `origin`. The program reads
this baseline as a hint. It prints a warning that names the file.
An encrypted restore target must come from an encrypted manifest event.
A plaintext baseline on an encrypted disc must have version 1.
Any other version is an error.

## Discs with no known mode

When a disc reports no encryption and this host has no saved mode, the
program requires confirmation. This applies when you do not supply a key,
rescue key, or environment secret. If stdin and stdout are terminals, the
program asks whether the named archive is plaintext. Answer `y` to save
mode `none` and continue. Any other answer stops with exit code 1. Without
a terminal, the command stops with exit code 1. Run the same command once
from a terminal to confirm that the disc is unencrypted. A plaintext
archive created with `init` saves mode `none`, so later reads do not ask.

## Errors

A metadata file that is on the disc but cannot be read is an error. It
is never treated as absent. This applies to a baseline that does not
decrypt, does not parse, or has a bad shape. `read_volume_baseline` then
raises `BaselineReadError`. The message names the file and tells the user
to check the key and the disc. A command that hits this error exits with
code 1. `sync` stops before it burns anything.

A manifest that does not decrypt or parse stays a SUSPECT manifest. The
`status` command lists SUSPECT sessions. See the README troubleshooting
section.
