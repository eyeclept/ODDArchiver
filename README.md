# ODDArchiver

Incremental backups use delta compression. They go to write-once optical media (DVD, BD-R, M-DISC) or ISO files.

## What it does

ODDArchiver treats a BD-R disc as an append-only history. Each `sync` command scans a source directory and computes an xdelta3 binary delta for each changed file.
It then burns a new session that contains only the deltas.
`restore` rebuilds the source tree at any point in history. It uses only the disc and `xdelta3`.

The tool needs no configuration for first use. It also supports encryption, multi-drive redundancy, and cron automation. These features do not depend on each other.

## Key properties

- **Self-describing.** Every session has a manifest. ODDArchiver needs no external database or state file to rebuild history.
- **Write-once safe.** ODDArchiver only appends data. It never rewrites a burned session.
- **Idempotent.** Running `init` or `sync` twice with no source changes produces no extra session or side effects. Ctrl+C at any point leaves the disc and cache in a consistent state.
- **Encryption is optional.** Choose `none`, `passphrase` (Argon2id plus ChaCha20-Poly1305), or `keyfile` at `init` time. Later commands read the mode from the disc automatically.
- **Cron-friendly.** `sync` prints nothing and exits 0 when nothing changed. It exits 1 on any failure.
- **Testable without a drive.** `--test-iso PATH` runs the full pipeline against a local ISO file instead of a physical disc.

## Install

See [SETUP.md](SETUP.md) for full install and configuration instructions. In short:

```sh
pip install -e .                 # no encryption
pip install -e ".[encrypt]"      # with passphrase/keyfile encryption
```

Requires Python 3.10 or later, plus the system tools `xdelta3`, `genisoimage`, `dvd+rw-tools`, and `xorriso`.

## Quick start

```sh
# Create session 0 against a local ISO (no drive needed)
oddarchiver init ~/Documents/ToArchive --test-iso archive.iso --label MYARCHIVE

# Sync changes into a new session
oddarchiver sync ~/Documents/ToArchive --test-iso archive.iso

# Check integrity
oddarchiver verify --test-iso archive.iso --level checksum

# Restore the latest state, or a past session
oddarchiver restore /tmp/restored --test-iso archive.iso
oddarchiver restore /tmp/restored --test-iso archive.iso --session 2

# Against a physical BD-R drive: replace --test-iso with --device
oddarchiver init ~/Documents/ToArchive --device /dev/sr0 --label MYARCHIVE
oddarchiver sync ~/Documents/ToArchive --device /dev/sr0
```

## Commands

| Command | Purpose |
|---|---|
| `init` | Write session 0, a full snapshot of the source directory |
| `sync` | Append a session with only changed, new, and deleted files |
| `restore` | Rebuild a source tree from disc or ISO, optionally at a past session |
| `history` | List every session: timestamp, files, size, encryption mode |
| `verify` | Check integrity at `fast`, `checksum`, or `full` depth |
| `ls` | List archive contents from disc or ISO manifests |
| `browse` | Interactively select archive paths and restore the set |

| Flag | Command | Purpose |
|---|---|---|
| `--all-disks` | `restore`, `ls`, `browse` | Use the archive catalog. Restore requests only discs with needed blobs. |
| `--all-files` | `restore` | Select every live file. Do not combine with `--path`. |
| `--unskip-volume N` | `restore` | Clear a saved missing-disc mark. |

All commands accept either `--device DEV` (physical drive, default `/dev/sr0`) or `--test-iso PATH` (local ISO file).

### `init`

```sh
oddarchiver init <source> [--device DEV] [--label LABEL]
                          [--encrypt {none,passphrase,keyfile}] [--key PATH]
                          [--yubikey] [--yubikey-slot {1,2}]
                          [--test-iso PATH] [--disc-size SIZE] [--prefill SIZE]
                          [--mirror DEV_OR_PATH] [--dry-run] [--slow] [--skip-large]
```

`--encrypt` and `--key` apply only to `init`.
`init` stores the chosen mode in the disc manifest.
Every later command uses this mode automatically.
`--yubikey` adds a hardware factor. It requires `--encrypt passphrase` or `--encrypt keyfile`.
`--yubikey-slot` picks the challenge-response slot. The default slot is 2.
See [docs/encryption.md](docs/encryption.md) for the YubiKey details.
Running `init` on a disc that already has session 0 exits 0 with a warning. ODDArchiver does not write a second session.

### `sync`

```sh
oddarchiver sync <source> [--device DEV] [--test-iso PATH]
                          [--disc-size SIZE] [--prefill SIZE]
                          [--no-cache] [--mirror DEV_OR_PATH] [--dry-run] [--slow]
                          [--skip-large]
```

Exits 0 with no output when nothing changed.
`--no-cache` reads every prior blob from the disc, not from the local cache (also with `--dry-run`). Use it when the local cache is missing, damaged, or from another host. Blobs read from the disc do not enter the cache. After a verified burn, the cache still records the new session. If the prior version of a changed file is a delta on the disc, `sync` stores a full copy. The disc holds only the delta for that version.
`--slow` on `init` and `sync` checksums every blob after the burn.
`--skip-large` on `init` and `sync` leaves out and lists two kinds of new or changed file.
The first kind does not fit on one disc. The second kind is larger than the local staging space.
Without the flag, a file larger than one disc splits across volumes. A file larger than the staging space stops the run with an error.
A skipped changed file keeps its previous version on the archive, and the next run tries it again.
The default post-burn verify is `fast` and does not read blob bytes.

### `restore`

```sh
oddarchiver restore <dest> [--device DEV] [--test-iso PATH]
                           [--all-disks] [--all-files | --path REL_PATH ...]
                           [--skip-volume N] [--unskip-volume N]
                           [--session N] [--force]
```

Restore is non-destructive by default.
ODDArchiver does not rewrite a file already at `dest` when its SHA-256 matches the target session.
`--force` overwrites files unconditionally.
`--session N` stops replay at session N for a point-in-time restore.
ODDArchiver never writes a file whose reconstructed checksum does not match its manifest entry.
ODDArchiver logs the error and continues to process the remaining files.

### `history`, `verify`, `status`

```sh
oddarchiver history [--device DEV] [--test-iso PATH]
oddarchiver verify  [--device DEV] [--test-iso PATH] [--level fast|checksum|full]
oddarchiver status  [--device DEV] [--test-iso PATH]
```

`verify` levels:

| Level | Speed | Checks |
|---|---|---|
| `fast` | seconds | Manifest checksums and session ordering. Default after every burn. Does not read blob bytes. |
| `checksum` | minutes | Reads and decrypts every stored blob, and compares its hash. `init --slow` and `sync --slow` use this after the burn. |
| `full` | minutes to hours | Full restore to a temp directory, then a per-file hash check. Recommended before retiring a disc. |

A failed session does not invalidate the others. `verify` reports pass or fail for each session and exits 1 if any session failed.

### Exit codes

`0` means success or a no-change run. `1` means an error: burn failure, verify failure, a full disc, or bad arguments. `3` means you paused a multi-disc `restore --all-disks` with `q` or end of input. Run the same command again to continue.

A `restore --all-disks` with missing discs exits 0. It reports the affected files as `stale` or `missing`. A corrupt blob on an available disc counts as a failure and gives exit 1.

## Dry run and test ISO mode

`--dry-run` (on `init` and `sync`) runs the full pipeline except the actual burn. It scans, diffs, computes deltas, checks disc space, and prints a report. It does not touch the disc, the ISO, or the cache. Combine `--dry-run` with `--test-iso` to preview a sync against an ISO without changing it.

`--test-iso PATH` builds a real Universal Disk Format (UDF) ISO on the local filesystem with `genisoimage`, instead of writing to a drive. It supports the full command set, including `restore` and `verify`. It is the basis for the automated test suite.

`--disc-size` sets the simulated capacity (default `25gb`). Pass a raw byte count, a unit-suffixed size (`25gb`, `93GiB`), or a named media preset. Preset names match case-insensitively.

| Preset | Nominal | Usable bytes | Write engine |
|---|---|---|---|
| `cd` | 700 MB | 737,280,000 | none (simulation only) |
| `dvd-sl` | 4.7 GB | 4,700,372,992 | growisofs plus genisoimage |
| `dvd-dl` | 8.5 GB | 8,547,991,552 | growisofs plus genisoimage |
| `bd-r` | 25 GB | 24,696,061,952 | growisofs plus genisoimage |
| `bd-dl` | 50 GB | 49,392,123,904 | growisofs plus genisoimage |
| `bdxl` | 100 GB | 99,857,989,632 | growisofs plus genisoimage |

ODDArchiver refuses CD media. A CD burn works, but ODDArchiver cannot keep a CD appendable.
An archive on a CD could never take a second session.
The `cd` preset remains for `ISOBackend` simulation. See [docs/media.md](docs/media.md) for the evidence.

Millennial Disc (M-DISC) uses the matching DVD or BD preset. ODDArchiver has no M-DISC flag. A drive must support M-DISC to write it.

See [docs/media.md](docs/media.md) for M-DISC details and which media have real hardware on hand.

`--prefill` adds simulated used space. Use it to test capacity warnings without burning real data.

## Encryption

Encryption is optional. Choose a mode once, at `init`.

| Mode | Flag | How it works |
|---|---|---|
| `none` | `--encrypt none` (default) | Plaintext on disc. Readable decades from now with only `xdelta3`. |
| `passphrase` | `--encrypt passphrase` | Key derived with Argon2id, data encrypted with ChaCha20-Poly1305. Passphrase comes from `ODDARCHIVER_PASSPHRASE` or an interactive prompt. |
| `keyfile` | `--encrypt keyfile --key PATH` | A 32-byte keyfile wraps a fresh per-file data key. Every command after `init` requires `--key`, and `init` does too. |

`init --yubikey` adds a YubiKey HMAC-SHA1 factor on top of the passphrase or keyfile. The secret becomes the mixed value. Later commands then need both the secret and the YubiKey. `--rescue-key` alone unlocks such an archive without the YubiKey. See [docs/encryption.md](docs/encryption.md).

Later commands read the encryption mode from the disc automatically. ODDArchiver never stores the encryption key itself on disc. The encrypted manifest hides file names, paths, and content. Encrypted discs still show:

- The encryption mode and rescue-block KDF parameters in `enc_mode.json`.
- The archive id, volume index, first session, creation time, and previous volume label in `volume.json`.
- The ISO volume label and ISO timestamps.
- Blob sizes and counts, and which blobs are full files or deltas.

Delta computation feeds the previous file version to `xdelta3` through an anonymous memory file (`os.memfd_create`). During restore, ODDArchiver writes decrypted data to private (0600) temp files in the destination directory. It then renames them atomically.

One keyfile or passphrase protects every volume in an archive. The rescue block stores a way to recover the same secret. ODDArchiver does not rotate keys. If the key leaks, every disc burned with it remains exposed. To change keys, start a new archive with `init` and a new key.

See [SETUP.md](SETUP.md) for generating a keyfile and setting the passphrase environment variable.

## Multi-drive redundancy

`--mirror DEV_OR_PATH` on `init` or `sync` writes each session to a second drive or ISO after the primary succeeds.

- If the primary burn fails, the command exits 1 and does not attempt the mirror.
- If the primary succeeds but the mirror fails, the command exits 1 and does not update the cache.
`status` reports the mirror as `MISSING`.
- ODDArchiver does not backfill a missing mirror automatically. To recover a missing mirror, do a full restore from the intact primary. Then run a fresh `init` against the replacement drive.

## Idempotency

| Operation | Repeat behavior |
|---|---|
| `init` on an already-initialized disc | Exits 0 with a warning, no second session written |
| `sync` with no source changes | Exits 0, no output, no burn |
| `sync` after an interrupted burn | Cache is left unchanged. The next run re-diffs from the last confirmed disc state |
| `restore` to an already-correct destination | Skips files whose checksum already matches |

Ctrl+C during a run is safe at every stage. ODDArchiver removes staging directories in a `finally` block. Any session whose disc write did not complete is absent on the next read.

## Architecture

```
oddarchiver/
    cli.py       argument parsing and command dispatch
    disc.py      BurnBackend interface: DiscBackend (physical) and ISOBackend (test)
    manifest.py  session manifest read/write/merge, disc-state replay
    delta.py     xdelta3 wrapper: compute, apply, threshold selection
    session.py   staging directory construction (scan, diff, encrypt, space check)
    cache.py     local encrypted-blob cache with disc-read fallback
    crypto.py    NullCrypto, PassphraseCrypto, KeyfileCrypto
    restore.py   reconstruct a source tree from disc or ISO
    verify.py    integrity checking at fast, checksum, and full depth
    config.py    loads ~/.config/oddarchiver/config.toml
    log.py       structured logging, capacity threshold checks
```

Each session on disc is one directory:

```
session_NNN/
    manifest.enc (or manifest.json if unencrypted)
    enc_mode.json          # plaintext mode and rescue-block KDF parameters
    full/<blob-id>       # new or changed files stored whole
    deltas/<blob-id>     # xdelta3 patches for changed files
```

`sync` decides delta versus full per file. If a delta exceeds 90% of the full file size, ODDArchiver stores the full file instead. New sessions (manifest version 3) name each blob with a random 256-bit id. Older sessions use `sha256(session_n:relative_path)` names. Someone with the disc could test a guessed path against those names. See [docs/encryption.md](docs/encryption.md).

## What to archive

ODDArchiver targets small, critical, slow-changing data: secrets, keys, infrastructure-as-code, DNS zones, and config exports. A 2.7 GB source with infrequent small changes fits years of weekly syncs on a single 25 GB disc.

ODDArchiver is the wrong tool for large or fast-changing data. This includes database indices, VM disk images, container images, log archives, and media libraries.

## Troubleshooting

**`disc already initialized; skipping init`**. `init` found an existing session 0. This message is normal and safe. ODDArchiver wrote no second session.

**"Decryption failed: authentication error"**. The passphrase or keyfile does not match the one used at `init`. Check `ODDARCHIVER_PASSPHRASE`, or confirm the `--key` path.

**SUSPECT sessions in `status`**. A manifest failed its checksum check. Restore and disc-state reconstruction skip that session. The other sessions remain usable. Check `~/logs/oddarchiver.log` for details.

**Stale staging directory warning**. This is normal after an interrupted run. ODDArchiver removes the old directory and rebuilds it.

**"Staging space check failed"**. The staging area (`$XDG_RUNTIME_DIR/oddarchiver`) does not have enough free room for this run.
On most systems, this area is a RAM-backed tmpfs, capped well below the real disk space of the machine.
An example: 6.2 GiB on a workstation with 61 GiB of RAM.

`init` stages every source file as a full blob. The whole source tree must fit there at once.
This applies regardless of free space on the target disc or in `/home`.
ODDArchiver checks this before it stages any file.
It fails fast with a clear message instead of crashing partway through.

Options: run from a session with a larger `$XDG_RUNTIME_DIR` (or a larger `RuntimeDirectorySize` for your login session).
Or split a very large source directory into smaller batches, and sync one batch at a time.

**Cache looks wrong after an interrupted burn**. The cache stays at the last confirmed state. Nothing is lost. To force a rebuild from disc reads, delete the cache directory (see SETUP.md).

## More

- [SETUP.md](SETUP.md): install, system dependencies, configuration file, environment variables
- [docs/media.md](docs/media.md): media presets, write engines, M-DISC, hardware vs ISO tests
- [docs/verify.md](docs/verify.md): verify levels and post-burn `--slow`
- [STATE_OF_THE_PROGRAM.md](STATE_OF_THE_PROGRAM.md): what is complete, what is left, known issues
