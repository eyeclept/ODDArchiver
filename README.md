# ODDArchiver

Incremental, delta-compressed backups to write-once optical media (BD-R, M-DISC) or ISO files.

## What it does

ODDArchiver treats a BD-R disc as an append-only history. Each `sync` command scans a source directory and computes an xdelta3 binary delta for every changed file, then burns a new session that holds only the deltas. `restore` rebuilds the source tree at any point in history, using only the disc and `xdelta3`.

The tool works with no configuration for a first-time user. It adds encryption, multi-drive redundancy, and cron automation for advanced use. No feature requires the others.

## Key properties

- **Self-describing.** Every session carries a manifest. No external database or state file is needed to reconstruct history.
- **Write-once safe.** ODDArchiver only appends. It never rewrites a burned session.
- **Idempotent.** Running `init` or `sync` twice with no source changes produces no extra session and no side effects. Ctrl+C at any point leaves the disc and cache in a consistent state.
- **Encryption is optional.** Choose `none`, `passphrase` (Argon2id plus ChaCha20-Poly1305), or `keyfile` at `init` time. Later commands read the mode from the disc automatically.
- **Cron-friendly.** `sync` prints nothing and exits 0 when nothing changed. It exits 1 on any failure.
- **Testable without a drive.** `--test-iso PATH` runs the full pipeline against a local ISO file instead of a physical disc.

## Install

See [SETUP.md](SETUP.md) for full install and configuration instructions. Short version:

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
| `status` | Show disc label, space used and remaining, capacity warnings, SUSPECT sessions |

All commands accept either `--device DEV` (physical drive, default `/dev/sr0`) or `--test-iso PATH` (local ISO file).

### `init`

```sh
oddarchiver init <source> [--device DEV] [--label LABEL]
                          [--encrypt {none,passphrase,keyfile}] [--key PATH]
                          [--test-iso PATH] [--disc-size SIZE] [--prefill SIZE]
                          [--mirror DEV_OR_PATH] [--dry-run]
```

`--encrypt` and `--key` apply only to `init`. The chosen mode is stored in the disc manifest and used automatically by every later command. Running `init` on a disc that already has session 0 exits 0 with a warning, and no second session is written.

### `sync`

```sh
oddarchiver sync <source> [--device DEV] [--test-iso PATH]
                          [--disc-size SIZE] [--prefill SIZE]
                          [--no-cache] [--mirror DEV_OR_PATH] [--dry-run]
```

Exits 0 with no output when nothing changed. `--no-cache` bypasses the local cache and reads every prior blob straight from disc.

### `restore`

```sh
oddarchiver restore <dest> [--device DEV] [--test-iso PATH]
                           [--session N] [--force]
```

Restore is non-destructive by default. A file already at `dest` whose SHA-256 matches the target session is not rewritten. `--force` overwrites unconditionally. `--session N` stops replay at session N for a point-in-time restore. A file whose reconstructed checksum does not match its manifest entry is never written. ODDArchiver logs the error and continues to process the remaining files.

### `history`, `verify`, `status`

```sh
oddarchiver history [--device DEV] [--test-iso PATH]
oddarchiver verify  [--device DEV] [--test-iso PATH] [--level fast|checksum|full]
oddarchiver status  [--device DEV] [--test-iso PATH]
```

`verify` levels:

| Level | Speed | Checks |
|---|---|---|
| `fast` | seconds | Manifest checksums and session ordering. Runs automatically after every burn. |
| `checksum` | minutes | Reads and decrypts every stored blob, and compares its hash. Recommended monthly. |
| `full` | minutes to hours | Full restore to a temp directory, then a per-file hash check. Recommended before retiring a disc. |

A failed session never invalidates the others. `verify` reports pass or fail per session and exits 1 if any session failed.

### Exit codes

`0` on success or a no-change run. `1` on any error: burn failure, verify failure, a full disc, or bad arguments.

## Dry run and test ISO mode

`--dry-run` (on `init` and `sync`) runs the full pipeline except the actual burn. It scans, diffs, computes deltas, checks disc space, and prints a report. It does not touch the disc, the ISO, or the cache. Combine `--dry-run` with `--test-iso` to preview a sync against an ISO without changing it.

`--test-iso PATH` builds a real UDF ISO on the local filesystem with `genisoimage`, instead of writing to a drive. It supports the full command set, including `restore` and `verify`, and is the basis for the automated test suite. `--disc-size` sets the simulated capacity (default `25gb`). `--prefill` adds simulated used space, useful for testing capacity warnings without burning real data.

## Encryption

Encryption is optional and chosen once, at `init`.

| Mode | Flag | How it works |
|---|---|---|
| `none` | `--encrypt none` (default) | Plaintext on disc. Readable decades from now with only `xdelta3`. |
| `passphrase` | `--encrypt passphrase` | Key derived with Argon2id, data encrypted with ChaCha20-Poly1305. Passphrase comes from `ODDARCHIVER_PASSPHRASE` or an interactive prompt. |
| `keyfile` | `--encrypt keyfile --key PATH` | A 32-byte keyfile wraps a fresh per-file data key. `--key` is required on every command after `init`, not only on `init`. |

Later commands read the encryption mode from the disc automatically. The key material itself is never stored on disc. Blob filenames are opaque SHA-256 hashes, and the manifest itself is encrypted. Mounting an encrypted disc reveals no file names, paths, or content.

All encryption and decryption works on data held in memory. Delta computation feeds the previous file version to `xdelta3` through an anonymous memory file (`os.memfd_create`). Plaintext never touches a temp file on disk.

See [SETUP.md](SETUP.md) for generating a keyfile and setting the passphrase environment variable.

## Multi-drive redundancy

`--mirror DEV_OR_PATH` on `init` or `sync` writes each session to a second drive or ISO after the primary succeeds.

- If the primary burn fails, the command exits 1 and the mirror is never attempted.
- If the primary succeeds but the mirror fails, the command exits 1 and the cache is not updated. `status` reports the mirror as `MISSING`.
- A missing mirror is not backfilled automatically. Recovering one requires a full restore from the intact primary, followed by a fresh `init` against the replacement drive.

## Idempotency

| Operation | Repeat behavior |
|---|---|
| `init` on an already-initialized disc | Exits 0 with a warning, no second session written |
| `sync` with no source changes | Exits 0, no output, no burn |
| `sync` after an interrupted burn | Cache is left unchanged. The next run re-diffs from the last confirmed disc state |
| `restore` to an already-correct destination | Skips files whose checksum already matches |

Ctrl+C during a run is safe at every stage. Staging directories are removed in a `finally` block. Any session whose disc write did not complete is simply absent on the next read.

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
    enc_mode.json          # plaintext mode indicator only, no key material
    full/<sha256-blob-id>       # new or changed files stored whole
    deltas/<sha256-blob-id>     # xdelta3 patches for changed files
```

`sync` decides delta versus full per file. If a delta exceeds 90% of the full file's size, ODDArchiver stores the full file instead. Blob names are `sha256(session_n:relative_path)`, so file names and directory structure never appear on disc.

## What to archive

ODDArchiver is built for small, critical, slow-changing data: secrets, keys, infrastructure-as-code, DNS zones, and config exports. A 2.7 GB source with infrequent small changes fits years of weekly syncs on a single 25 GB disc.

It is the wrong tool for large or fast-changing data. That includes database indices, VM disk images, container images, log archives, and media libraries.

## Troubleshooting

**`disc already initialized; skipping init`**. `init` found an existing session 0. This is expected and safe. No second session was written.

**"Decryption failed: authentication error"**. The passphrase or keyfile does not match the one used at `init`. Check `ODDARCHIVER_PASSPHRASE`, or confirm the `--key` path.

**SUSPECT sessions in `status`**. A manifest failed its checksum check. That session is skipped during restore and disc-state reconstruction. The other sessions remain usable. Check `~/logs/oddarchiver.log` for details.

**Stale staging directory warning**. Normal after an interrupted run. ODDArchiver removes the old directory and rebuilds it.

**"Staging space check failed"**. The staging area (`$XDG_RUNTIME_DIR/oddarchiver`, a RAM-backed tmpfs on most systems, capped well below a machine's real disk space -- 6.2 GiB on a workstation with 61 GiB of RAM) does not have enough free room for this run. `init` stages every file in the source as a full blob, so the whole source tree has to fit there at once, regardless of how much space the target disc or `/home` has. ODDArchiver checks this before staging any file and fails fast with a clear message instead of crashing partway through. Options: run from a session with a larger `$XDG_RUNTIME_DIR` (or a larger `RuntimeDirectorySize` for your login session), or split a very large source directory into smaller batches synced one at a time.

**Cache looks wrong after an interrupted burn**. The cache is left at the last confirmed state. Nothing is lost. To force a rebuild from disc reads, delete the cache directory (see SETUP.md).

## More

- [SETUP.md](SETUP.md): install, system dependencies, configuration file, environment variables
- [STATE_OF_THE_PROGRAM.md](STATE_OF_THE_PROGRAM.md): what is done, what is left, known issues
