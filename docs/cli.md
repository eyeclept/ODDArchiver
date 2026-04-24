# ODDArchiver — CLI Reference

```
oddarchiver <command> [flags]
```

---

## Commands

### `init`

Create session 0 — a full snapshot of the source directory.

```sh
oddarchiver init <source> [--device DEV] [--label LABEL]
                          [--encrypt {none,passphrase,keyfile}] [--key PATH]
                          [--test-iso PATH] [--disc-size SIZE] [--prefill SIZE]
                          [--dry-run]
```

| Flag | Default | Description |
|---|---|---|
| `source` | *(required)* | Directory to archive |
| `--device` | `/dev/sr0` | Block device for physical disc |
| `--label` | `ARCHIVE` | UDF volume label |
| `--encrypt` | `none` | Encryption mode: `none`, `passphrase`, or `keyfile` |
| `--key` | — | Keyfile path (required when `--encrypt keyfile`) |
| `--test-iso` | — | Path to ISO file (uses `ISOBackend` instead of a physical drive) |
| `--disc-size` | `25gb` | Simulated disc capacity (ISO mode only) |
| `--prefill` | — | Simulate used space (ISO mode only; see [disc.md](disc.md)) |
| `--dry-run` | `false` | Run pipeline without writing anything |

`--encrypt` and `--key` are only meaningful on `init`; they are silently ignored on all other commands. The mode is stored in session 0's manifest and read automatically thereafter.

---

### `sync`

Append an incremental session containing only changed files.

```sh
oddarchiver sync <source> [--device DEV] [--test-iso PATH]
                          [--disc-size SIZE] [--prefill SIZE]
                          [--no-cache] [--dry-run]
```

| Flag | Default | Description |
|---|---|---|
| `source` | *(required)* | Directory to sync |
| `--no-cache` | `false` | Bypass local cache; read all blobs directly from disc |
| `--dry-run` | `false` | Print what would change; do not burn or update cache |

Exits 0 silently when no files have changed (safe for cron).

---

### `restore`

Reconstruct the source directory from disc or ISO.

```sh
oddarchiver restore <dest> [--device DEV] [--test-iso PATH]
                           [--session N] [--force]
```

| Flag | Default | Description |
|---|---|---|
| `dest` | *(required)* | Destination directory |
| `--session N` | latest | Stop replay at session N (point-in-time restore) |
| `--force` | `false` | Overwrite existing files even when checksums match |

Non-destructive by default: a file already at `dest` with the correct checksum is not rewritten.

---

### `history`

Print a table of all sessions on disc or ISO.

```sh
oddarchiver history [--device DEV] [--test-iso PATH]
```

Output columns: session number, timestamp, files changed, session size, encryption mode.

---

### `verify`

Check the integrity of disc or ISO contents.

```sh
oddarchiver verify [--device DEV] [--test-iso PATH] [--level LEVEL]
```

| Flag | Default | Description |
|---|---|---|
| `--level` | `fast` | `fast`, `checksum`, or `full` (see [verify.md](verify.md)) |

---

### `status`

Show disc or ISO state and any warnings.

```sh
oddarchiver status [--device DEV] [--test-iso PATH]
```

Prints: label, session count, used/remaining space, capacity warnings, SUSPECT manifest entries.

---

## Mutual Exclusions

`--dry-run` and `--test-iso` may be used together. `--test-iso` selects the ISO backend; `--dry-run` skips the actual write. They are orthogonal flags.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success, or no-change run |
| `1` | Any error (parse failure, burn failure, verification failure) |

---

## Dry-Run Mode

`--dry-run` (available on `init` and `sync`) runs the full pipeline — source scan, diff, delta computation, space check — but skips the actual burn and cache update. Output mirrors rsync `-n` format: one line per file that would be written, followed by a summary. A space overage is reported but does not cause exit 1 in dry-run mode.

### What dry-run does

- Scans source and computes checksums
- Diffs against disc or ISO state
- Computes deltas and classifies each file (`delta` vs `full`, with sizes)
- Checks disc space and reports whether the session would fit
- Prints a detailed report

### What dry-run does NOT do

- Call `growisofs` or `genisoimage`
- Write anything to disc or ISO
- Update the cache or manifest on disc

### Output format

```
$ oddarchiver sync ~/Documents/ToArchive --dry-run

DRY RUN -- no disc will be written

Scanning source: /home/user/Documents/ToArchive (42 files, 2.7 GiB)
Reading disc state: ARCHIVE-01, session 4

Changes detected:
  [delta]  passwords.kdbx                           5.0 MiB → 4.1 KiB delta (99.9% reduction)
  [full]   notes/new_note.md                        2.0 KiB  (new file)
  [full]   report.pdf                               3.2 MiB  (delta 97% of full -- storing full)

  3 files to write, 2 unchanged

Session size:        3.2 MiB
Disc remaining:      16.9 GiB
Space check:         OK (session is 0.02% of remaining)

Would burn as: session_005 on ARCHIVE-01
No disc written (dry run).
```

If the session would not fit, `Space check:` shows `OVERAGE` with the shortfall, but the command still exits 0.

### Combining with `--test-iso`

`--dry-run` and `--test-iso` may be used together. `--test-iso` selects the ISO backend; `--dry-run` prevents the actual write. Use this combination to preview what a sync would do against an ISO file without modifying it.
