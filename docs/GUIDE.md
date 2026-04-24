# ODDArchiver User Guide

This guide covers the full workflow from installation through daily use. For command flag details see [cli.md](cli.md); for the manifest schema see [manifest.md](manifest.md).

---

## Install

### Requirements

- Python 3.10+
- `xdelta3` — binary delta computation
- `genisoimage` — ISO building (`--test-iso` mode and tests)
- `dvd+rw-tools` — physical BD-R writes and mediainfo queries

Install system tools:

```sh
# Fedora/RHEL
sudo dnf install xdelta genisoimage dvd+rw-tools

# Debian/Ubuntu
sudo apt install xdelta3 genisoimage dvd+rw-tools
```

### Python package

```sh
# Basic install (no encryption)
pip install -e .

# With encryption support
pip install -e ".[encrypt]"
```

The `[encrypt]` extra adds `argon2-cffi` and `cryptography`, required for `passphrase` and `keyfile` modes. See [Encryption modes](#encryption-modes) below.

Verify:

```sh
oddarchiver --help
```

---

## First archive

Create session 0, a full snapshot of the source directory.

### With a test ISO (no physical drive needed)

```sh
oddarchiver init ~/Documents/ToArchive --test-iso archive.iso --label MYARCHIVE
```

This writes all source files to `archive.iso` (and a sibling `archive.iso.d/` directory). No physical drive is required.

### With a physical BD-R drive

Insert a blank BD-R disc, then:

```sh
oddarchiver init ~/Documents/ToArchive --device /dev/sr0 --label MYARCHIVE
```

Replace `/dev/sr0` with your actual drive device if different.

### Options at init time

| Option | Example | Purpose |
|--------|---------|---------|
| `--label` | `--label PHOTOS_2026` | UDF volume label |
| `--encrypt` | `--encrypt passphrase` | Encryption mode (see below) |
| `--key` | `--key /path/to/key.bin` | Keyfile path (keyfile mode) |
| `--disc-size` | `--disc-size 50gb` | Simulated capacity (ISO mode) |
| `--dry-run` | `--dry-run` | Preview without writing |
| `--mirror` | `--mirror /dev/sr1` | Write session to a second drive or ISO |

Encryption mode is set permanently at `init` time and read automatically by all subsequent commands.

---

## Daily sync

After the initial archive, run `sync` to append an incremental session containing only changed, new, or deleted files.

```sh
# ISO
oddarchiver sync ~/Documents/ToArchive --test-iso archive.iso

# Physical disc
oddarchiver sync ~/Documents/ToArchive --device /dev/sr0
```

`sync` exits 0 silently when no files have changed — safe for cron:

```cron
0 2 * * * oddarchiver sync /data/source --device /dev/sr0
```

### Preview changes before burning

```sh
oddarchiver sync ~/Documents/ToArchive --test-iso archive.iso --dry-run
```

Prints a report of what would be written (with delta sizes) but does not modify the ISO. `--dry-run` and `--test-iso` may be combined freely. See [cli.md](cli.md) for the full output format.

### Check history

```sh
oddarchiver history --test-iso archive.iso
```

Prints a table of all sessions with timestamps, file counts, session sizes, and encryption mode.

### Check disc state

```sh
oddarchiver status --test-iso archive.iso
```

Shows label, session count, used/remaining space, and any SUSPECT manifest entries. See [logging.md](logging.md) for capacity thresholds.

### Mirror redundancy

Pass `--mirror` to write each session to a second drive or ISO simultaneously:

```sh
oddarchiver init ~/Documents/ToArchive --test-iso primary.iso --mirror mirror.iso
oddarchiver sync ~/Documents/ToArchive --test-iso primary.iso --mirror mirror.iso
```

If the primary burn succeeds but the mirror fails, the command exits 1 and the cache is not updated. `oddarchiver status` will report the mirror as MISSING. See [mirror.md](mirror.md) for failure recovery.

---

## Restore

Reconstruct the source directory from a disc or ISO.

```sh
# Restore latest state
oddarchiver restore /tmp/restored --test-iso archive.iso

# Point-in-time restore (stop at session 3)
oddarchiver restore /tmp/restored --test-iso archive.iso --session 3

# Physical disc
oddarchiver restore /tmp/restored --device /dev/sr0
```

Restore is non-destructive by default: a file already at the destination whose SHA-256 matches the target session is not rewritten. Use `--force` to overwrite unconditionally.

Files with checksum mismatches are never written. All other files continue to be processed regardless of per-file failures. Exit code is 1 if any file failed.

See [restore.md](restore.md) for the full restore procedure and SUSPECT manifest handling.

---

## Verify

Check the integrity of disc or ISO contents at three levels of depth.

```sh
# Fast: manifest checksums only (default; also runs automatically after every burn)
oddarchiver verify --test-iso archive.iso --level fast

# Checksum: read and decrypt every stored blob
oddarchiver verify --test-iso archive.iso --level checksum

# Full: restore all sessions to a temp directory and verify SHA-256 of every file
oddarchiver verify --test-iso archive.iso --level full
```

| Level | Speed | What is checked |
|-------|-------|----------------|
| `fast` | Seconds | Manifest checksums, session ordering |
| `checksum` | Minutes | Blob read + decrypt + hash comparison |
| `full` | Minutes–hours | Complete restore + per-file SHA-256 |

A failed session does not invalidate others; per-session status is always reported. Exit 0 if all sessions pass; exit 1 on any error.

`--level fast` runs automatically after every burn. To upgrade the automatic post-burn verify to checksum level, add to `~/.config/oddarchiver/config.toml`:

```toml
post_burn_verify = "checksum"
```

See [verify.md](verify.md) for the full output format and parameter reference.

---

## Encryption modes

Encryption mode is chosen once at `init` time and applied automatically to all subsequent commands.

| Mode | Flag | Description |
|------|------|-------------|
| `none` | `--encrypt none` | No encryption (default) |
| `passphrase` | `--encrypt passphrase` | Argon2id KDF + ChaCha20-Poly1305 |
| `keyfile` | `--encrypt keyfile --key /path/to/key.bin` | Per-file DEK wrapped with a 32-byte keyfile |

### Passphrase mode

```sh
export ODDARCHIVER_PASSPHRASE="correct horse battery staple"
oddarchiver init ~/ToArchive --test-iso archive.iso --encrypt passphrase
oddarchiver sync ~/ToArchive --test-iso archive.iso   # passphrase read from env
```

If `ODDARCHIVER_PASSPHRASE` is not set, the passphrase is prompted interactively (input is hidden).

### Keyfile mode

Generate a keyfile:

```sh
python3 -c "
from oddarchiver.crypto import generate_keyfile
generate_keyfile('/path/to/keyfile.bin')
"
```

Then initialize with the keyfile:

```sh
oddarchiver init ~/ToArchive --test-iso archive.iso --encrypt keyfile --key /path/to/keyfile.bin
```

Keep the keyfile secure; it is required for all decrypt operations.

See [encryption.md](encryption.md) for wire formats, KDF parameters, and the in-memory plaintext guarantee.

---

## Troubleshooting

### "disc not initialized; run 'init' first"

`sync` requires at least session 0 on disc. Run `oddarchiver init` first.

### "disc already initialized; skipping init"

`init` on a disc that already has session 0 exits 0 with this warning. No second session is written. This is safe to ignore if re-running init accidentally.

### "Decryption failed: authentication error"

The passphrase or keyfile does not match what was used at init time. Check `ODDARCHIVER_PASSPHRASE` or verify you are pointing at the correct keyfile.

### SUSPECT manifest entries

`oddarchiver status` reports sessions whose manifest checksum failed. SUSPECT sessions are skipped during restore and disc-state reconstruction. Remaining sessions are still usable. Check the log:

```sh
grep SUSPECT ~/logs/oddarchiver.log
```

### Stale staging directory warning

If a previous run was interrupted, a warning like the following may appear:

```
WARNING [oddarchiver.session] Stale staging dir found for session 001; removing and rebuilding.
```

This is normal. The stale directory is removed automatically and a fresh one is created.

### Cache inconsistency after interrupted burn

If a burn was interrupted (power loss, kill signal), the cache is left at the previous session's state. The next `sync` re-diffs from the last confirmed disc state. No data is lost or duplicated. To force a cache rebuild from disc reads:

```sh
rm -rf ~/.cache/oddarchiver
```

### Disc capacity warnings

Capacity thresholds (from [logging.md](logging.md)):

| Used % | Level | Action |
|--------|-------|--------|
| < 80% | INFO | Normal |
| ≥ 80% | WARNING | Plan for a new disc |
| ≥ 95% | ERROR | Sync will fail if session does not fit |

### Log file location

The default log is `~/logs/oddarchiver.log`. Override in `~/.config/oddarchiver/config.toml`:

```toml
log_file = "~/logs/oddarchiver.log"
```

See [logging.md](logging.md) and [configuration.md](configuration.md) for full details.
