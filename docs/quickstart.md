# ODDArchiver Quickstart

## Install

```bash
pip install -e .                  # stdlib-only (no encryption)
pip install -e ".[encrypt]"       # with PassphraseCrypto / KeyfileCrypto
```

## Commands

All commands accept either `--device /dev/sr0` (physical disc) or `--test-iso path/to/output.iso` (local ISO file for testing). Examples below use `--test-iso`.

---

### init — create session 0 (full snapshot)

```bash
oddarchiver init ~/Documents/ToArchive --test-iso archive.iso --label MYARCHIVE
```

Options:
- `--encrypt none|passphrase|keyfile` (default: `none`)
- `--key PATH` — keyfile path (keyfile mode only)
- `--disc-size SIZE` — simulated disc capacity, e.g. `25gb`, `93gib` (ISO mode)
- `--prefill SIZE` — pre-fill simulated used space (ISO testing)
- `--dry-run` — print what would be staged without burning

Passphrase is read from `ODDARCHIVER_PASSPHRASE` env var; if unset, prompted interactively.

---

### sync — burn an incremental session

```bash
oddarchiver sync ~/Documents/ToArchive --test-iso archive.iso
```

Exits 0 silently if no files changed. Options: `--no-cache`, `--dry-run`.

---

### history — list all sessions

```bash
oddarchiver history --test-iso archive.iso
```

Output:
```
Session  Timestamp              Files       Size Encryption
-------  ---------------------  ------  -------- ----------
000      2026-04-23T00:00:00Z        3    1.2 GiB none
001      2026-04-24T10:00:00Z        1   45.0 KiB none
```

---

### status — disc/ISO state and warnings

```bash
oddarchiver status --test-iso archive.iso
```

Shows label, session count, used/remaining space, and any SUSPECT manifest entries.

---

### restore — reconstruct source directory

```bash
oddarchiver restore /tmp/restored --test-iso archive.iso
oddarchiver restore /tmp/restored --test-iso archive.iso --session 2  # point-in-time
oddarchiver restore /tmp/restored --test-iso archive.iso --force       # overwrite existing
```

Non-destructive by default: existing files whose checksum matches the target session are skipped.

---

### verify — integrity check

```bash
oddarchiver verify --test-iso archive.iso --level fast       # manifests only (fastest)
oddarchiver verify --test-iso archive.iso --level checksum   # read and hash all blobs
oddarchiver verify --test-iso archive.iso --level full       # full restore + checksum
```

Exits 0 if all sessions pass; exits 1 on any error.
Fast verify runs automatically after every burn.

---

## Physical Disc

Replace `--test-iso path.iso` with `--device /dev/sr0` (or your drive path).

```bash
oddarchiver init ~/ToArchive --device /dev/sr0 --label ARCHIVE
oddarchiver sync ~/ToArchive --device /dev/sr0
oddarchiver verify --device /dev/sr0 --level checksum
oddarchiver restore /tmp/out --device /dev/sr0
```

## Encryption Example

```bash
export ODDARCHIVER_PASSPHRASE="correct horse battery staple"
oddarchiver init ~/ToArchive --test-iso archive.iso --encrypt passphrase
oddarchiver sync ~/ToArchive --test-iso archive.iso   # passphrase read from env
```
