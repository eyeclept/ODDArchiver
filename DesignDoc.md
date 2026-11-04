# ODDArchiver Design Document

## Overview

`ODDArchiver` creates incremental, delta-compressed backups on write-once optical media (BD-R / M-DISC). It provides rsync-like behavior and manages immutable, multi-session UDF filesystems.

BD-R supports appends, but not random access. Each session stores a permanent, immutable snapshot of a delta. Each append stores xdelta3 binary patches, not full file copies. The disc stores an auditable directory history. Reconstruct this history with only the disc and `xdelta3`.

**Design philosophy:** ODDArchiver supports users at any skill level. New users can burn a directory with one command and no configuration. Advanced users can add encryption and automation. Each tier works without the next.

---

## Goals

- Incremental, delta-compressed sessions on BD-R / M-DISC
- Self-describing disc: no external state required to reconstruct any version
- Local cache improves performance but is not required
- rsync-like CLI (`oddarchiver sync`, `oddarchiver restore`, etc.)
- Cron-friendly: silent on no changes, non-zero exit on failure
- Recovers from interrupted burns, missing caches, and disc-read errors
- Optional encryption. Users can choose its complexity or run without it.
- Idempotent: Safe to run repeatedly and safe to stop with Ctrl+C at any point
- Dry-run and ISO test modes validate the pipeline without using physical media

## Non-Goals

- Sub-file chunking and rolling checksums (the rsync algorithm) are out of scope. xdelta3 processes whole files.
- BD-RE and rewritable media support
- Network targets
- GUI. The ODDArchiver-server handles the web UI

---

## Constraints

| Constraint | Implication |
|---|---|
| BD-R sectors are write-once | Append only. Existing sessions cannot be patched. |
| Maximum ~153 sessions per disc | Each session uses ~20MB of overhead. Plan for this. |
| UDF merges sessions into one logical view | The filesystem layer cannot distinguish sessions. Use manifests. |
| Disc reads are slow (~18MB/s at 4x) | Cache old file versions locally to avoid disc reads during diff computation. |
| xdelta3 is single-threaded | Large files block. Run files in parallel, not within a file. |

---

## Architecture

```
oddarchiver/
|-- __main__.py          # entry point
|-- cli.py               # argparse, command dispatch
|-- disc.py              # BurnBackend interface + DiscBackend + ISOBackend
|-- manifest.py          # session manifest read/write/merge
|-- delta.py             # xdelta3 wrapper, delta vs full decision
|-- session.py           # staging directory construction
|-- cache.py             # local cache management
|-- crypto.py            # encryption layer (optional). All modes share one interface
|-- restore.py           # reconstruct any version from disc or ISO
`-- verify.py            # integrity checking
```

### External Dependencies

| Tool | Purpose | Package |
|---|---|---|
| `growisofs` | Write/append sessions to disc | `dvd+rw-tools` |
| `dvd+rw-mediainfo` | Query disc state, remaining space | `dvd+rw-tools` |
| `xdelta3` | Binary delta computation and application | `xdelta3` |
| `udftools` | Mount/read individual sessions if needed | `udftools` |
| `genisoimage` | Build UDF ISO images for test mode | `genisoimage` / `cdrtools` |

The core uses only the Python standard library. Optional dependencies require install extras:

```
pip install oddarchiver            # no encryption
pip install oddarchiver[encrypt]   # passphrase + keyfile support (adds argon2-cffi, pyage)
```

`hvac` (Vault client) is a dependency of ODDArchiver-server, not ODDArchiver. ODDArchiver does not use Vault.

---

## CLI Interface

```
oddarchiver init   <source> [--device /dev/sr0] [--label LABEL] [--encrypt MODE]
                            [--dry-run] [--test-iso /path/to/output.iso]
oddarchiver sync   <source> [--device /dev/sr0] [--dry-run] [--no-cache]
                            [--test-iso /path/to/output.iso]
oddarchiver restore <dest>  [--device /dev/sr0] [--session N] [--force]
                            [--test-iso /path/to/output.iso]
oddarchiver history         [--device /dev/sr0] [--test-iso /path/to/output.iso]
oddarchiver verify          [--device /dev/sr0] [--level fast|checksum|full]
                            [--test-iso /path/to/output.iso]
oddarchiver status          [--device /dev/sr0] [--test-iso /path/to/output.iso]
```

Encryption flags are needed on `init` only. The manifest stores the mode on disc, and later operations use it automatically.

`--dry-run` and `--test-iso` cannot run together. `--test-iso` writes to an ISO file, not a physical disc.

---

## Dry Run Mode

`--dry-run` works like the rsync `--dry-run` option. The pipeline runs except for the actual `growisofs` call. It does not write to disc or commit a staging directory. It leaves the cache unchanged.

Dry run actions:
- Scan the source and compute checksums
- Compare against disc or ISO state
- Compute deltas and classify each file as delta or full, with sizes
- Check disc space and report whether the session fits
- Build the manifest in memory
- Print a detailed report of the planned write

Dry run does not:
- Call `growisofs` or `genisoimage`
- Write to disc or ISO
- Update the cache or the manifest on disc

The output format matches rsync's `-n` output:

```
$ oddarchiver sync ~/Documents/ToArchive --dry-run

DRY RUN -- no disc will be written

Scanning source: /home/user/Documents/ToArchive (42 files, 2.7GB)
Reading disc state: ARCHIVE-01, session 4

Changes detected:
  [delta]  passwords.kdbx          5.0MB -> 4.1KB delta (99.9% reduction)
  [full]   notes/new_note.md       2.0KB  (new file)
  [full]   report.pdf              3.2MB  (delta 97% of full -- storing full)

  3 files to write, 2 unchanged

Session size:        3.2MB
Disc remaining:      16.9GB
Space check:         OK (session is 0.02% of remaining)

Would burn as: session_005 on ARCHIVE-01
No disc written (dry run).
```

Dry run is safe on any disc at any time. It never accesses the ODD except to read the manifest. Use it to check the pipeline before you commit a session. Use it to estimate the space a sync will use.

---

## Test ISO Mode

`--test-iso /path/to/output.iso` runs the complete pipeline and performs the write. It writes to a local ISO file instead of a physical disc. The ISO is a valid UDF image. Mount and inspect it, or use it with `restore` and `verify`.

Use this mode to:
- Validate the full pipeline without using a disc
- Test ODDArchiver in CI/CD
- Practice restore procedures before using a real disc

### How It Works

`disc.py` uses a `BurnBackend` interface with two implementations:

```python
class BurnBackend:
    def init(self, staging: Path, label: str) -> None: ...
    def append(self, staging: Path, label: str) -> None: ...
    def mediainfo(self) -> DiscInfo: ...
    def read_path(self, path: str) -> bytes: ...
```

```python
class DiscBackend(BurnBackend):
    # uses growisofs, dvd+rw-mediainfo, real /dev/srN

class ISOBackend(BurnBackend):
    # uses genisoimage to build ISO, loop-mounts for reads
    # appends sessions by rebuilding the ISO with all prior
    # sessions plus the new one (UDF multi-session in a file)
```

Both backends use the same delta computation, encryption, manifest writing, and verification. They differ only in the final write and read-back.

### ISO Backend Detail

`genisoimage` from the `cdrtools`/`wodim` package builds UDF ISO images:

```bash
# First session (init):
genisoimage -udf -R -J -V ARCHIVE-TEST \
  -o output.iso staging/

# Subsequent sessions (append):
# Read prior sessions from existing ISO, merge with new staging, rebuild
genisoimage -udf -R -J -V ARCHIVE-TEST \
  -M output.iso -C $(isoinfo -d -i output.iso | grep "session") \
  -o output.iso.tmp staging/
mv output.iso.tmp output.iso
```

The ISO backend uses a configurable limit to simulate disc capacity. The default is 23GB for a 25GB disc and 93GB for a 100GB BDXL disc. Space checks work as they do for the disc backend.

### Mounting the Test ISO

```bash
# Mount for inspection
sudo mount -o loop output.iso /mnt/test-disc

# Use with oddarchiver commands directly
oddarchiver restore /tmp/restored --test-iso output.iso
oddarchiver verify --test-iso output.iso --level full
oddarchiver history --test-iso output.iso
```

### Simulated Disc Capacity

```bash
# Simulate a 25GB disc (default)
oddarchiver init ~/ToArchive --test-iso test.iso

# Simulate a 100GB BDXL disc
oddarchiver init ~/ToArchive --test-iso test.iso --disc-size 100gb

# Simulate a nearly-full disc (useful for testing capacity warnings)
oddarchiver init ~/ToArchive --test-iso test.iso --disc-size 25gb --prefill 20gb
```

`--prefill` writes synthetic padding sessions to the ISO. It simulates a disc with N GB already used, without running that many sync sessions. Use it to test the 80%/95% capacity warnings.

### Configuration

```toml
[test]
default_disc_size = "25gb"    # disc size simulated by --test-iso
iso_capacity_margin = 0.92    # simulate the actual usable capacity of BD-R vs rated
```

---

## Encryption

Encryption is optional. The three modes range from simple to enterprise. Each mode uses the same internal interface (`crypto.py`), so the burn pipeline stays the same.

### Mode 0: No Encryption (default)

No flags are needed. The system writes plaintext deltas to disc. Use this mode when:
- The disc is physically secured in a safe deposit box or locked cabinet
- Data sensitivity is low
- Long-term readability without key management is the priority

You can reconstruct data in 50 years with only `xdelta3`. This mode has no dependencies.

### Mode 1: Passphrase (recommended for most users)

```bash
oddarchiver init ~/ToArchive --encrypt passphrase
# prompts: Enter passphrase: ****
# prompts: Confirm passphrase: ****
```

- Derive the key from the passphrase with **Argon2id** (memory-hard, GPU-resistant)
- Cipher: **ChaCha20-Poly1305** (authenticated encryption, no padding oracle risk)
- The manifest stores the salt on disc. The passphrase is not stored.
- Prompt for the passphrase on each run, or read it from env var `ODDARCHIVER_PASSPHRASE` for automation
- Work offline forever. No external services or key files are required.

Store the passphrase in KeePassXC. Include that KeePassXC database on the disc through your existing backup. This creates a self-referential backup, but it is sound.

### Mode 2: Key File

```bash
oddarchiver init ~/ToArchive --encrypt keyfile --key /path/to/archive.key
```

- The key file contains a 256-bit random key in age format
- Generate the key file with `age-keygen -o archive.key`
- Store the key file on a LUKS volume, Apricorn USB, or other secured storage
- Use this mode for automation without Vault and without a passphrase prompt
- If you lose the key file, you permanently lose access to the disc. Keep multiple copies.

### Mode 3: Key File via ODDArchiver-server (enterprise)

Vault is intentionally **not** an ODDArchiver encryption mode. Vault is an online service. If Vault becomes unavailable or disappears in 20 years, a disc that needs Vault for decryption becomes permanently unreadable. This violates the archival guarantee.

ODDArchiver-server handles Vault integration. The server uses Vault to get a keyfile path or passphrase, then supplies it to ODDArchiver at burn time. ODDArchiver always receives a passphrase or keyfile. It does not know about Vault. See the ODDArchiver-server design document for details.

ODDArchiver supports keyfile as its highest encryption mode (Mode 2). Keep the keyfile on a LUKS volume, an Apricorn USB, or another physically secured medium, not in an online service.

### YubiKey extra factor (passphrase or keyfile)

`init --yubikey` adds a hardware factor on top of passphrase or keyfile mode.
`--yubikey-slot {1,2}` picks the challenge-response slot. The default slot is 2.

At `init`, ODDArchiver generates a 64-byte random challenge and reads a
20-byte HMAC-SHA1 response from the YubiKey. It mixes the response into
the primary secret:

```
SHA-256(b"oddarchiver-yubikey-v1\x00" + primary_secret + 20-byte HMAC-SHA1)
```

The mixed value replaces the passphrase or keyfile secret. `enc_mode.json`
stores the public challenge: `alg` (`hmac-sha1`), `slot`, and `challenge`
in base64. The challenge is public on disc. The HMAC secret stays on the
YubiKey. Init prints: `This archive needs the YubiKey for later commands
except --rescue-key.`

Every later command reads the block, gets a new HMAC response, and mixes
it the same way. `--rescue-key` unwraps the mixed secret from the rescue
block and does not call the YubiKey. Discs without a `yubikey` block stay
passphrase or keyfile only.

---

## Encryption Pipeline

The pipeline is the same in every mode. **xdelta3 processes plaintext in memory only. Plaintext never touches the filesystem.**

```
Disc (at rest):
  encrypted delta blob or encrypted full file

  -> decrypt to memory only (ChaCha20-Poly1305)

xdelta3 sees:
  plaintext old file (from memory)
  plaintext new file (read from source)

  -> xdelta3 via stdin/stdout pipes (no temp files)

xdelta3 produces:
  plaintext delta (in memory)

  -> encrypt from memory (ChaCha20-Poly1305, fresh nonce)

Disc (written):
  encrypted delta blob
```

xdelta3 runs through `subprocess.Popen` with `stdin=PIPE, stdout=PIPE`. ODDArchiver sends old file content through stdin and reads the delta from stdout. The diff uses no temporary files. The staging directory holds only encrypted blobs.

The local cache stores encrypted blobs that match the disc. ODDArchiver decrypts blobs in memory when it reads the cache for diffing. ODDArchiver recommends LUKS encryption for the cache directory, but does not require it.

---

## On-Disc Layout

Each disc session contains one directory:

```
session_NNN/
|-- manifest.json
|-- full/
|   `-- <relative/path/to/new_file>         # new files (encrypted if mode != 0)
`-- deltas/
    `-- <relative/path/to/file>.xdelta      # patches for changed files (encrypted)
```

The UDF filesystem merges all sessions into one logical view:

```
/mnt/disc/
|-- session_000/
|-- session_001/
|-- session_002/
`-- ...
```

---

## Manifest Format

`session_NNN/manifest.json`:

```json
{
  "version": 1,
  "session": 3,
  "timestamp": "2026-04-18T02:00:00Z",
  "source": "/home/user/Documents/ToArchive",
  "label": "ARCHIVE",
  "based_on_session": 2,
  "encryption": {
    "mode": "keyfile",
    "cipher": "chacha20-poly1305",
    "kdf": "argon2id",
    "kdf_params": { "m": 65536, "t": 3, "p": 4 },
    "salt": "a1b2c3..."
  },
  "entries": [
    {
      "path": "passwords.kdbx",
      "type": "delta",
      "source_checksum": "a3f1c2...",
      "result_checksum": "d9e2f4...",
      "delta_file": "session_003/deltas/passwords.kdbx.xdelta",
      "delta_size_bytes": 4120,
      "full_size_bytes": 5242880,
      "encrypted_dek": "age1..."
    },
    {
      "path": "notes/new_note.md",
      "type": "full",
      "result_checksum": "b8d4e1...",
      "file": "session_003/full/notes/new_note.md",
      "full_size_bytes": 2048,
      "encrypted_dek": "age1..."
    }
  ],
  "deleted": ["old_file.txt"],
  "manifest_checksum": "f1e2d3..."
}
```

ODDArchiver omits `encrypted_dek` when `encryption.mode` is `"none"`. In passphrase and keyfile modes, this field contains the age-encrypted DEK.

`manifest_checksum` is the sha256 of the manifest when that field is `""`. `verify` uses it to detect manifest corruption independently of file corruption.

Checksums (`source_checksum`, `result_checksum`) always cover **plaintext content**, regardless of encryption mode. Verification must decrypt the data first. This confirms the full pipeline, not only the stored bytes.

---

## Sync Algorithm

```
1. DISC CHECK
   - Confirm disc/ISO present and appendable
   - Parse dvd+rw-mediainfo (disc) or ISO metadata (ISO): remaining blocks, session count
   - Record session count N at start of run (used for double-burn guard)

2. READ DISC STATE
   - Mount disc or loop-mount ISO (merged UDF view)
   - Find all session_NNN/manifest.json ascending
   - Build last known state: {path: result_checksum}
     by replaying manifest checksums only (no file I/O)

3. SCAN SOURCE
   - Walk <source>, compute sha256 for each file
   - Build current state: {path: checksum}

4. DIFF
   - changed = checksum mismatch vs last known state
   - new     = in current, not in last known state
   - deleted = in last known state, not in current (logged, not acted on)
   - If changed + new == 0: log "no changes", exit 0

5. STAGE DELTAS
   For each changed file:
     a. Retrieve old version into memory:
        - Check local cache first (decrypt to memory)
        - Cache miss: read off disc/ISO, reconstruct via delta chain to memory
        - Update cache with new encrypted blob after burn (step 8)
     b. Feed old version and new file to xdelta3 via stdin/stdout pipes
        (plaintext only in memory, no temp files)
     c. Receive delta from xdelta3 stdout into memory buffer
     d. If delta_size > DELTA_THRESHOLD * full_size (default 0.90):
          store full copy instead
     e. Encrypt buffer with ChaCha20-Poly1305
     f. Write encrypted blob to staging/session_NNN/deltas/

   For each new file:
     a. Read file into memory
     b. Encrypt
     c. Write to staging/session_NNN/full/

6. SPACE CHECK
   staging_bytes = du -sb staging/
   remaining_bytes = remaining_blocks * 2048
   If staging_bytes >= remaining_bytes * SPACE_SAFETY_MARGIN (0.95):
     log ERROR, exit 1

7. WRITE MANIFEST
   Build manifest.json for session N+1
   Write atomically (write to .tmp, rename) to staging/

8. BURN
   If --dry-run: print report, exit 0 (steps 8-10 skipped entirely)
   Re-read disc/ISO session count -- if != N, abort (double-burn guard)
   DiscBackend: growisofs -Z/-M <device> -R -J -T -V <label> staging/
   ISOBackend:  genisoimage rebuild with prior sessions + staging/
   On non-zero exit: log ERROR, do NOT update cache, exit 1

9. POST-BURN VERIFY (automatic, --level fast)
   Read session_NNN/manifest.json off disc/ISO
   Verify manifest_checksum matches staged copy
   On failure: log SUSPECT, do NOT update cache, exit 1

10. UPDATE CACHE
    Copy encrypted blobs for changed/new files to cache
    Update cache manifest

11. CLEANUP
    rm -rf staging/ (in finally block -- runs even on Ctrl+C)
```

---

## Idempotency

ODDArchiver can run repeatedly without side effects.

| Operation | Idempotent? | Mechanism |
|---|---|---|
| `init` on already-initialized disc/ISO | Yes | Reads session count. If session 0 exists, exits 0 with warning |
| `sync` with no changes | Yes | Checksum diff finds nothing. No staging, no burn |
| `sync` run twice after same change | Yes | Second run: disc state matches source. Diff is empty |
| `sync` after interrupted burn | Yes | Cache not updated on failure. Re-diffs from last confirmed disc state |
| `restore` to already-correct dest | Yes | Per-file checksum verified before overwrite. Skip if match |
| `verify` | Yes | Read-only |
| `history` | Yes | Read-only |

**Additional idempotency protections:**

- **Session index collision**: ODDArchiver deletes and rebuilds the staging directory for N+1 if a previous crashed run left it in place
- **Double-burn guard**: ODDArchiver reads the disc/ISO session count again immediately before writing. It aborts if the count has advanced.
- **Authoritative state is always disc/ISO**: The cache affects performance, not correctness

**Restore behavior:**

- **`restore` is non-destructive by default**: ODDArchiver overwrites files only when their checksum differs from the target session. Pass `--force` to override.

---

## Ctrl+C / SIGINT Safety

ODDArchiver installs a SIGINT handler at startup. The handler sets a flag. `finally` blocks run cleanup. Ctrl+C can occur at five points:

**1. During scan, diff, or staging (pre-burn)**
No disc write has occurred. The `finally` block cleans the staging directory. The next run rebuilds it. [OK]

**2. During `growisofs` / `genisoimage` (mid-burn)**
The backend process receives SIGINT. Two outcomes are possible:

- *Lead-out not written*: UDF does not show the session. The next run compares data again and retries. [OK]
- *Lead-out written, manifest corrupt or absent*: UDF shows the session, but its manifest is invalid. The next run reads the manifest and checks its checksum. It logs invalid sessions as SUSPECT and ignores them when it computes disc state. [OK]

**3. Between burn and post-burn verify**
The next run reads the disc/ISO and checks the manifest. It adopts the session if valid, then updates the cache. [OK]

**4. Between verify and cache update**
The cache is one session behind. The next run treats the disc as authoritative, finds no changes, and updates the cache. [OK]

**5. During cache update**
The cache is partly written. The next run treats it as a cache miss and reads from disc/ISO. [OK]

Worst case, one burn is wasted and the log has a SUSPECT entry. Data on disc is never modified.

---

## Write Verification

### Levels

**`--level fast`** (automatic after every burn)
- Read the manifest from disc/ISO again and verify its checksum
- Confirm that the session index and timestamp match
- Runtime: seconds

**`--level checksum`** (recommended monthly)
- Read every stored blob from disc/ISO
- Verify each blob's sha256 against its manifest entry
- Do not decrypt or reconstruct. Verify the raw stored content.
- Detect bit rot, partial writes, and surface defects
- Runtime: proportional to disc volume at ~18MB/s

**`--level full`** (recommended before retiring a disc)
- Run a full `restore` to a temporary directory
- Verify every reconstructed file against `result_checksum`
- Test the complete delta chain and encryption round-trip
- Runtime: minutes to tens of minutes

Post-burn fast verification is required. Set `post_burn_verify = "checksum"` in config to enable checksum verification after every burn.

### Verify Output

```
$ oddarchiver verify --level checksum

Session 000: 38 files -- OK
Session 001:  2 files -- OK
Session 002:  1 file  -- OK
Session 003:  3 files -- FAIL
  CHECKSUM MISMATCH: session_003/deltas/passwords.kdbx.xdelta
    expected: d9e2f4a1...
    got:      f1c3e8b2...
Session 004:  2 files -- OK

Result: 1 error across 5 sessions.
Exit code: 1
```

A failed session does not invalidate other sessions. `restore` skips corrupt delta files. It uses the last known-good version of the affected file and logs an ERROR.

---

## Restore Algorithm

```
1. Read all session manifests from disc/ISO ascending (0..N)
2. If --session S: stop replaying at session S
3. Build per-file chain: [full, delta, delta, ...]
4. For each file:
   a. Apply session where type == "full":
      decrypt blob -> write to <dest>
   b. For each subsequent delta in order:
      decrypt blob -> feed to xdelta3 with current file -> write output
   c. Verify sha256(<dest_file>) == result_checksum for that session
      abort on mismatch (unless --skip-corrupt, which uses last good version)
5. Report files restored, verification failures
```

By default, `restore` does not overwrite correct files in `<dest>`. It overwrites a file only if its checksum differs. Pass `--force` to overwrite all files.

---

## Logging

### Log File

Default path: `~/logs/oddarchiver.log`. The file contains structured plaintext, with one event per line:

```
2026-04-18T02:00:01Z INFO    [sync] Starting sync: source=/home/user/Documents/ToArchive device=/dev/sr0
2026-04-18T02:00:02Z INFO    [sync] Disc: session=3 used=6.1GB remaining=18.4GB (25% used)
2026-04-18T02:00:03Z INFO    [sync] Scanned 42 files (2.7GB)
2026-04-18T02:00:04Z INFO    [sync] Changes: 2 modified, 1 new, 0 deleted
2026-04-18T02:00:04Z INFO    [delta] passwords.kdbx: delta 4.1KB vs full 5.0MB -- storing delta
2026-04-18T02:00:04Z INFO    [delta] report.pdf: delta 3.1MB vs full 3.2MB -- threshold exceeded, storing full
2026-04-18T02:00:05Z INFO    [stage] Staged session_004: 3.2MB encrypted
2026-04-18T02:00:05Z INFO    [burn] Burning session 4 via growisofs -M
2026-04-18T02:01:12Z INFO    [burn] Burn complete (67s)
2026-04-18T02:01:13Z INFO    [verify] Post-burn manifest checksum OK
2026-04-18T02:01:14Z INFO    [cache] Cache updated
2026-04-18T02:01:14Z INFO    [sync] Done. Session 4 written. Disc 27% full.
```

### Log Levels

| Level | When |
|---|---|
| `INFO` | Normal operation milestones |
| `WARN` | Recoverable: cache miss, disc >80% full, delta threshold exceeded frequently |
| `ERROR` | Failures causing non-zero exit |
| `SUSPECT` | Disc anomalies not causing immediate failure: orphaned session, manifest with bad checksum |

`oddarchiver status` always shows `SUSPECT` entries. It does not hide them.

### Disc Capacity Warnings

| Used | Level | Action |
|---|---|---|
| < 80% | INFO | Normal |
| >= 80% | WARN | "~N sessions remaining at current delta size" |
| >= 95% | ERROR | "Next sync may fail. Insert new disc soon." |

The remaining-session estimate uses the rolling average of the last 3 session sizes, not a fixed count.

At >=80%, cron output includes a WARN line. Cron sends email if configured to mail on output. No external notification system is required.

---

## Configuration

`~/.config/oddarchiver/config.toml`:

```toml
[defaults]
device = "/dev/sr0"
cache_dir = "~/.cache/oddarchiver"
staging_dir = "/tmp/oddarchiver_staging"
delta_threshold = 0.90
space_safety_margin = 0.95
log_file = "~/logs/oddarchiver.log"
post_burn_verify = "fast"   # "fast" | "checksum"

[encryption]
mode = "passphrase"         # "none" | "passphrase" | "keyfile"
# keyfile_path = "~/.config/oddarchiver/archive.key"
# When run via ODDArchiver-server, the server supplies the keyfile path or
# passphrase at invocation time -- ODDArchiver does not need to know about Vault.

[test]
default_disc_size = "25gb"
iso_capacity_margin = 0.92
```

The config file is optional. All values have defaults. Flags for each invocation override config values.

---

## Cron Integration

```cron
0 2 * * 0 oddarchiver sync ~/Documents/ToArchive --device /dev/sr0
```

Exit codes:
- `0` -- success (changes burned, or no changes detected)
- `1` -- error (disc absent, full, burn failed, verify failed)

Runs with no changes produce no output. Errors go to stderr and the log file. Cron can email stderr output.

---

## Disc Capacity Planning

### 25GB BD-R (single layer)

| Sessions | Approx used | Notes |
|---|---|---|
| 1 (init) | ~2.7GB + 20MB | Full snapshot of 2.7GB source |
| 2-5 | +10-50MB/session | Typical small-file deltas (kdbx, configs) |
| 6-8 | approaching capacity | Depends on delta sizes |

A Tier 1 homelab archive (keys, configs, and small documents) can support **years of weekly syncs** before it fills. A 2.7GB source with infrequent, small changes may produce <10MB deltas per session.

### 100GB BDXL (triple layer)

One 100GB BDXL disc can store all of Tier 1 and Tier 2, with years of delta history before you need another disc.

---

## What to Archive (Target Data)

### Tier 1 -- Archive This (small, critical, slow-changing)

These files are critical. Losing them is catastrophic, and rebuilding them can take weeks.

**Secrets and credentials**
- KeePassXC `.kdbx` / Vaultwarden export
- SSH private keys (`~/.ssh/`)
- GPG private keys (`~/.gnupg/`)
- TLS/CA private keys and internal PKI roots (Step-CA, cert-manager)
- WireGuard / OpenVPN configs and private keys
- LUKS header backups (`cryptsetup luksHeaderBackup`) -- loss = permanent data loss
- Age / SOPS master keys

**Identity and auth**
- Authentik / Keycloak / Authelia config and realm exports
- LDAP / FreeIPA database exports
- Vault unseal keys and root token
- 2FA backup codes

**Infrastructure as Code**
- Ansible playbooks and inventory
- Terraform state files (`.tfstate`) and variable files
- Helm values files
- Docker Compose files
- Kickstart / cloud-init / preseed configs

**Network and DNS**
- Internal DNS zone files (Bind, PowerDNS, AdGuard)
- Firewall ruleset exports (pfSense/OPNsense XML, iptables dumps)
- VLAN and network topology configs

**Platform-specific configs**
- OpenStack Keystone credentials and `clouds.yaml`
- Custom flavor/quota definitions
- Project/tenant structure exports

**Monitoring and security rules**
- Prometheus alerting and recording rules
- Grafana dashboard JSON exports
- OpenSearch index templates and ILM policies
- Wazuh custom rules and decoders
- Suricata / Snort custom rules
- Fleet query packs and agent configs
- Shuffle SOAR workflow exports

**Estimated Tier 1 size: 1-5GB.** A single 25GB disc is generous.

### Tier 2 -- Consider It (medium value, worth archiving if it fits)

These items can be recovered, but manual reconstruction is difficult.

- Gitea / Forgejo database dump (SQLite = single file, small PostgreSQL installs)
- Nextcloud `config.php` and encryption keys (not user data -- too large)
- Home Assistant `/config/` (minus large media)
- Wiki.js / BookStack database exports
- Vaultwarden data directory
- Paperless-ngx database and search index
- Immich database dump (metadata only, photos go elsewhere)
- Jellyfin metadata database

**Estimated Tier 2 size: 2-20GB, depending on installed software.**

- 25GB disc: Tier 1 + small Tier 2 installs
- 100GB disc: Tier 1 + all Tier 2 + years of delta history

### Tier 3 -- Wrong Medium (do not use BD-R for these)

| Data | Reason | Better medium |
|---|---|---|
| OpenSearch / Elasticsearch indices | Large, fast-changing, rebuilt from source | Object storage / cold HDD |
| Ceph data | Ceph is already redundant | Ceph |
| VM disk images | Too large. Use hypervisor snapshots | SAN / NAS |
| Container images | Pull from registry | Registry with backup |
| Log archives | Too large, append-only already | Object storage |
| Nextcloud user data | Too large | NAS + Restic |
| Jellyfin / Immich media | Too large. Needs separate archival strategy | HDD / cold storage |

---

## Implementation Notes

- Python 3.10+
- Use `pathlib` throughout. Do not use `os.path`.
- Run xdelta3 with `subprocess.Popen` and `stdin=PIPE, stdout=PIPE`. Do not use temporary files.
- Use `concurrent.futures.ThreadPoolExecutor` to process files in parallel (I/O bound)
- Create staging directories with `tempfile.mkdtemp` and clean them up in `finally`
- Route all disc reads through one `BurnBackend` interface. Mock it for tests and use `ISOBackend` in test mode.
- Write manifests atomically: write to `.tmp`, then rename
- Use `shutil.disk_usage` for staging space. Parse `dvd+rw-mediainfo` stdout with regex to get disc space.
- Use `argon2-cffi` for Argon2id KDF (passphrase mode)
- Use `pyage` or the `age` CLI subprocess for age key operations
- Do not add a Vault dependency. ODDArchiver-server handles Vault integration.
- The `BurnBackend` interface in `disc.py` separates real disc and ISO backends. Pipeline code above the backend does not depend on the mode.

---

## Future Considerations

- **Multi-drive redundancy** (`--mirror /dev/sr1`): burn identical sessions to two drives simultaneously. Store one on-site, one off-site. Stretch goal.
- **PAR2 recovery blocks**: write PAR2 data into each session for bit-rot correction beyond what verify detects
- **`oddarchiver diff --session A --session B`**: show what changed between two sessions
- **Shell completion**: bash/zsh/fish completions for all commands
- **ODDArchiver-server integration**: ODDArchiver exposes a Python API (not just CLI) for ODDArchiver-server to call directly