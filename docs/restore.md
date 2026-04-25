# Restore

## Overview

`oddarchiver restore` reconstructs the source directory from a disc or ISO. It replays session manifests in ascending order, applies delta chains, decrypts blobs, and verifies every file's sha256 before writing.

## Usage

```
oddarchiver restore <dest> --device /dev/sr0
oddarchiver restore <dest> --test-iso archive.iso
oddarchiver restore <dest> --test-iso archive.iso --session 2
oddarchiver restore <dest> --device /dev/sr0 --force
```

## Flags

| Flag | Description |
|---|---|
| `--session S` | Stop replay at session S (point-in-time restore); files added after S are absent |
| `--force` | Overwrite existing dest files even if their checksum already matches the target |

## Restore procedure

1. Read `mediainfo()` to determine how many sessions are on the disc.
2. Read each session's manifest (`manifest.enc` if encrypted, else `manifest.json`) up to the target session. Decrypts using the passphrase/keyfile read from `enc_mode.json`.
3. Build a per-file chain from all manifest entries (skipping SUSPECT sessions with a WARNING).
4. For each file in the final state:
   - If dest file exists and already matches the target checksum, skip (unless `--force`).
   - Reconstruct: start from the last "full" blob, apply subsequent delta entries in order.
   - Decrypt each blob with the active `CryptoBackend`.
   - Verify sha256 against `result_checksum` from the manifest.
   - Write to `<dest>/<rel_path>` only if checksum passes.

## Non-destructive default

By default, restore skips any dest file whose sha256 already matches the target session checksum. Safe to re-run after a partial restore.

## `--force`

Rewrites every file unconditionally, even when dest checksum matches. Use after manually editing restored files to reset them.

## Checksum mismatch handling

If the reconstructed bytes do not match `result_checksum`:

- `ERROR` is logged with the expected and actual checksums.
- The file is **not** written to dest.
- Other files continue to be processed.
- The return value `failure_count` is incremented.

Corrupt blobs are never silently written.

## SUSPECT manifests

Sessions whose `manifest_checksum` fails validation are marked SUSPECT and skipped during chain construction. A WARNING is logged. Gaps in the session chain caused by SUSPECT entries may leave files at an older version.

## Exit codes

The CLI layer translates `failure_count > 0` to exit 1. A successful restore exits 0.

## Example commands

```bash
# Full restore to /tmp/out from a test ISO
oddarchiver restore /tmp/out --test-iso archive.iso

# Point-in-time: restore as of session 3
oddarchiver restore /tmp/out --test-iso archive.iso --session 3

# Force overwrite of all files
oddarchiver restore /tmp/out --device /dev/sr0 --force
```
