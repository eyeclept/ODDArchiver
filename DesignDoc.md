# ODDArchiver Design Document

## Overview

`ODDArchiver` is a command-line tool for incremental, delta-compressed backups to write-once optical media (BD-R / M-DISC). It presents rsync-like semantics to the user while managing the constraints of immutable multi-session UDF filesystems under the hood.

The core insight: BD-R is not a random-access target, but it is an append target. Each session is a permanent, immutable snapshot of a delta. By storing xdelta3 binary patches rather than full file copies on each append, the disc becomes a self-describing, auditable history of a directory over time -- reconstructable with only the disc and `xdelta3` installed.

**Design philosophy:** ODDArchiver is usable at any skill level. A first-time user can burn a directory with one command and no configuration. Advanced users can layer in encryption and automation. No tier requires the next.

---

## Goals

- Incremental, delta-compressed sessions on BD-R / M-DISC
- Self-describing disc: no external state required to reconstruct any version
- Local cache as performance optimization, not requirement
- rsync-like CLI (`oddarchiver sync`, `oddarchiver restore`, etc.)
- Cron-friendly: silent on no changes, non-zero exit on failure
- Robust against interrupted burns, missing cache, and disc-read errors
- Optional encryption with a clear complexity gradient -- usable without it
- Idempotent: safe to run repeatedly, safe to Ctrl+C at any point
- Dry run and ISO test modes to validate pipeline without consuming physical media

## Non-Goals

- Sub-file chunking / rolling checksums (rsync algorithm) -- xdelta3 operates on whole files
- BD-RE / rewritable media support
- Network targets
- GUI (web UI is an ODDArchiver-server concern)

---

## Constraints

| Constraint | Implication |
|---|---|
| BD-R sectors are write-once | No patching existing sessions; only append |
| Max ~153 sessions per disc | Session overhead ~20MB each; budget accordingly |
| UDF merges sessions into one logical view | Cannot rely on filesystem layer to distinguish sessions; must use manifests |
| Reading disc is slow (~18MB/s at 4x) | Cache old file versions locally to avoid disc reads during diff computation |
| xdelta3 is single-threaded | Large files will block; parallelize across files, not within |

---

## Architecture

```
oddarchiver/
├── __main__.py          # entry point
├── cli.py               # argparse, command dispatch
├── disc.py              # BurnBackend interface + DiscBackend + ISOBackend
├── manifest.py          # session manifest read/write/merge
├── delta.py             # xdelta3 wrapper, delta vs full decision
├── session.py           # staging directory construction
├── cache.py             # local cache management
├── crypto.py            # encryption layer (optional); all modes share one interface
├── restore.py           # reconstruct any version from disc or ISO
└── verify.py            # integrity checking
```

### External Dependencies

| Tool | Purpose | Package |
|---|---|---|
| `growisofs` | Write/append sessions to disc | `dvd+rw-tools` |
| `dvd+rw-mediainfo` | Query disc state, remaining space | `dvd+rw-tools` |
| `xdelta3` | Binary delta computation and application | `xdelta3` |
| `udftools` | Mount/read individual sessions if needed | `udftools` |
| `genisoimage` | Build UDF ISO images for test mode | `genisoimage` / `cdrtools` |

Python stdlib only for core functionality. Optional dependencies gated behind install extras:

```
pip install oddarchiver            # no encryption
pip install oddarchiver[encrypt]   # passphrase + keyfile support (adds argon2-cffi, pyage)
```

`hvac` (Vault client) is a dependency of ODDArchiver-server, not ODDArchiver. ODDArchiver has no Vault awareness.

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

Encryption flags only required on `init` -- the mode is stored in the disc manifest and used automatically on subsequent operations.

`--dry-run` and `--test-iso` are mutually exclusive. `--test-iso` implies a real write, just not to a physical disc.

---

## Dry Run Mode

`--dry-run` mirrors rsync's `--dry-run`: the full pipeline executes except the actual `growisofs` call. No disc is written. No staging directory is committed. Cache is not updated.

What dry run does:
- Scans source and computes checksums
- Diffs against disc or ISO state
- Computes deltas and classifies each file (delta vs full, with sizes)
- Checks disc space (reports whether the session would fit)
- Builds the manifest in memory
- Prints a detailed report of what would be written

What dry run does not do:
- Call `growisofs` or `genisoimage`
- Write anything to disc or ISO
- Update the cache or manifest on disc

Output format mirrors rsync's `-n` output:

```
$ oddarchiver sync ~/Documents/ToArchive --dry-run

DRY RUN -- no disc will be written

Scanning source: /home/user/Documents/ToArchive (42 files, 2.7GB)
Reading disc state: ARCHIVE-01, session 4

Changes detected:
  [delta]  passwords.kdbx          5.0MB → 4.1KB delta (99.9% reduction)
  [full]   notes/new_note.md       2.0KB  (new file)
  [full]   report.pdf              3.2MB  (delta 97% of full -- storing full)

  3 files to write, 2 unchanged

Session size:        3.2MB
Disc remaining:      16.9GB
Space check:         OK (session is 0.02% of remaining)

Would burn as: session_005 on ARCHIVE-01
No disc written (dry run).
```

Dry run is safe to run on any disc at any time -- it never touches the ODD beyond reading the manifest. Useful for verifying the pipeline is working before committing a session, and for checking how much space a sync would consume.

---

## Test ISO Mode

`--test-iso /path/to/output.iso` runs the complete pipeline -- including the actual write -- but targets an ISO file on the local filesystem instead of a physical disc. The ISO is a valid UDF image that can be mounted, inspected, and used as the source for `restore` and `verify` commands.

This is the correct mode for:
- Validating the full pipeline end-to-end without using a disc
- CI/CD testing of ODDArchiver itself
- Practicing restore procedures before trusting a real disc

### How It Works

`disc.py` wraps the burn backend behind a `BurnBackend` interface with two implementations:

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

The rest of the pipeline -- delta computation, encryption, manifest writing, verification -- is identical in both backends. Only the final write and the read-back differ.

### ISO Backend Detail

`genisoimage` (part of `cdrtools`/`wodim` package) builds UDF ISO images:

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

The ISO backend simulates disc capacity using a configurable limit (default 23GB for 25GB disc, 93GB for 100GB BDXL). Space checks work identically to the disc backend.

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

`--prefill` writes synthetic padding sessions to the ISO to simulate a disc that already has N GB used, without having to actually run that many sync sessions. Useful for testing the 80%/95% capacity warning paths.

### Configuration

```toml
[test]
default_disc_size = "25gb"    # disc size simulated by --test-iso
iso_capacity_margin = 0.92    # simulate BD-R's actual usable capacity vs rated
```

---

## Encryption

Encryption is entirely optional. The three modes form a gradient from simple to enterprise. Each mode uses the same internal interface (`crypto.py`) so the burn pipeline is unchanged regardless of mode.

### Mode 0: No Encryption (default)

No flags needed. Plaintext deltas written to disc. Appropriate when:
- The disc is physically secured (safe deposit box, locked cabinet)
- Data sensitivity is low
- Long-term readability without any key management is the priority

Reconstructable in 50 years with only `xdelta3`. No dependencies.

### Mode 1: Passphrase (recommended for most users)

```bash
oddarchiver init ~/ToArchive --encrypt passphrase
# prompts: Enter passphrase: ****
# prompts: Confirm passphrase: ****
```

- Key derived via **Argon2id** from the passphrase (memory-hard, GPU-resistant)
- Cipher: **ChaCha20-Poly1305** (authenticated encryption, no padding oracle risk)
- Salt stored in disc manifest; passphrase never stored anywhere
- Passphrase prompted on each run (or read from env var `ODDARCHIVER_PASSPHRASE` for automation)
- Offline forever -- no external services, no key files to lose

Store the passphrase in KeePassXC. That KeePassXC database goes on the disc via your existing backup. Self-referential but sound.

### Mode 2: Key File

```bash
oddarchiver init ~/ToArchive --encrypt keyfile --key /path/to/archive.key
```

- Key file is a 256-bit random key in age format
- Generate: `age-keygen -o archive.key`
- Key file lives on LUKS volume, Apricorn USB, or other secured storage
- Suitable for automation without Vault -- no passphrase prompt needed
- Lose the key file = lose access to disc permanently; keep multiple copies

### Mode 3: Key File via ODDArchiver-server (enterprise)

Vault is intentionally **not** an ODDArchiver encryption mode. Vault is an online service -- if it is unavailable or gone in 20 years, any disc whose decryption depends on Vault becomes unreadable permanently. This violates the core archival guarantee.

Vault integration lives in ODDArchiver-server, which uses Vault to securely retrieve and supply a keyfile path or passphrase to ODDArchiver at burn time. From ODDArchiver's perspective, it always receives either a passphrase or a keyfile -- it has no awareness of Vault. See the ODDArchiver-server design document for details.

The highest encryption mode ODDArchiver itself supports is keyfile (Mode 2). Keep the keyfile on a LUKS volume, an Apricorn USB, or another physically secured medium -- not in an online service.

---

## Encryption Pipeline

Regardless of mode, the pipeline is identical. The key insight: **xdelta3 always operates on plaintext, which exists only in memory. Plaintext never touches the filesystem.**

```
Disc (at rest):
  encrypted delta blob or encrypted full file

  ↓ decrypt to memory only (ChaCha20-Poly1305)

xdelta3 sees:
  plaintext old file (from memory)
  plaintext new file (read from source)

  ↓ xdelta3 via stdin/stdout pipes (no temp files)

xdelta3 produces:
  plaintext delta (in memory)

  ↓ encrypt from memory (ChaCha20-Poly1305, fresh nonce)

Disc (written):
  encrypted delta blob
```

Implementation: xdelta3 is invoked with `subprocess.Popen` using `stdin=PIPE, stdout=PIPE`. Old file content is fed via stdin; delta is read from stdout. No temp files on disk at any point during the diff. The staging directory contains only encrypted blobs.

The local cache stores encrypted blobs matching what is on disc. Decryption happens in memory when the cache is read for diffing. LUKS encryption of the cache directory is recommended but not required by ODDArchiver itself.

---

## On-Disc Layout

Each session written to disc contains a single directory:

```
session_NNN/
├── manifest.json
├── full/
│   └── <relative/path/to/new_file>         # new files (encrypted if mode != 0)
└── deltas/
    └── <relative/path/to/file>.xdelta      # patches for changed files (encrypted)
```

The UDF filesystem merges all sessions into one logical view:

```
/mnt/disc/
├── session_000/
├── session_001/
├── session_002/
└── ...
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

`encrypted_dek` is omitted when `encryption.mode` is `"none"`. For passphrase and keyfile modes, it contains the age-encrypted DEK.

`manifest_checksum` is sha256 of the manifest with that field set to `""`. Allows `verify` to detect manifest corruption independently of file corruption.

Checksums (`source_checksum`, `result_checksum`) are always of **plaintext content** regardless of encryption mode. This means verification requires decryption first -- it proves the full pipeline is intact, not just that bytes were stored.

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

ODDArchiver is designed to be run repeatedly without side effects.

| Operation | Idempotent? | Mechanism |
|---|---|---|
| `init` on already-initialized disc/ISO | Yes | Reads session count; if session 0 exists, exits 0 with warning |
| `sync` with no changes | Yes | Checksum diff finds nothing; no staging, no burn |
| `sync` run twice after same change | Yes | Second run: disc state matches source; diff empty |
| `sync` after interrupted burn | Yes | Cache not updated on failure; re-diffs from last confirmed disc state |
| `restore` to already-correct dest | Yes | Per-file checksum verified before overwrite; skip if match |
| `verify` | Yes | Read-only |
| `history` | Yes | Read-only |

**Additional idempotency protections:**

- **Session index collision**: staging dir for N+1 is deleted and rebuilt if it already exists from a prior crashed run
- **Double-burn guard**: disc/ISO session count re-read immediately before write; if it has advanced, abort
- **Authoritative state is always disc/ISO**: cache influences performance, never correctness
- **`restore` is non-destructive by default**: files only overwritten if checksum doesn't match target session; pass `--force` to override

---

## Ctrl+C / SIGINT Safety

ODDArchiver installs a SIGINT handler at startup. The handler sets a flag; cleanup runs in `finally` blocks. Ctrl+C can land at five points:

**1. During scan, diff, or staging (pre-burn)**
No disc write has occurred. `finally` block cleans staging. Next run rebuilds from scratch. ✅

**2. During `growisofs` / `genisoimage` (mid-burn)**
Backend process receives SIGINT. Two outcomes:

- *Lead-out not written*: UDF does not surface the session. Next run re-diffs and retries. ✅
- *Lead-out written, manifest corrupt or absent*: Session surfaced by UDF but manifest invalid. Next run reads manifest, validates checksum -- invalid sessions are logged as SUSPECT and ignored when computing disc state. ✅

**3. Between burn and post-burn verify**
Next run reads disc/ISO, validates manifest, adopts session if valid, updates cache. ✅

**4. Between verify and cache update**
Cache stale by one session. Next run: disc authoritative, diff shows no changes, cache brought forward. ✅

**5. During cache update**
Cache partially written. Treated as cache miss on next run; falls back to disc/ISO read. ✅

Worst case: one wasted burn and a SUSPECT entry in the log. Data already on disc is never modified.

---

## Write Verification

### Levels

**`--level fast`** (automatic after every burn)
- Re-reads manifest off disc/ISO, verifies checksum
- Confirms session index and timestamp consistency
- Runtime: seconds

**`--level checksum`** (recommended monthly)
- Reads every stored blob off disc/ISO
- Verifies sha256 of each blob against manifest entry
- Does not decrypt or reconstruct -- verifies raw stored content
- Detects bit rot, partial writes, surface defects
- Runtime: proportional to disc volume at ~18MB/s

**`--level full`** (recommended before retiring a disc)
- Full `restore` to temp directory
- Verifies every reconstructed file against `result_checksum`
- Exercises complete delta chain and encryption round-trip
- Runtime: minutes to tens of minutes

Post-burn fast verify is not optional. `post_burn_verify = "checksum"` in config enables checksum verify after every burn.

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

A failed session does not invalidate others. `restore` skips corrupt delta files and uses the last known-good version of the affected file, logging at ERROR.

---

## Restore Algorithm

```
1. Read all session manifests from disc/ISO ascending (0..N)
2. If --session S: stop replaying at session S
3. Build per-file chain: [full, delta, delta, ...]
4. For each file:
   a. Apply session where type == "full":
      decrypt blob → write to <dest>
   b. For each subsequent delta in order:
      decrypt blob → feed to xdelta3 with current file → write output
   c. Verify sha256(<dest_file>) == result_checksum for that session
      abort on mismatch (unless --skip-corrupt, which uses last good version)
5. Report files restored, verification failures
```

`restore` is non-destructive by default -- existing files in `<dest>` are only overwritten if their checksum doesn't match. Pass `--force` to overwrite unconditionally.

---

## Logging

### Log File

Default: `~/logs/oddarchiver.log`. Structured plaintext, one event per line:

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

`SUSPECT` is always surfaced in `oddarchiver status` -- never silently buried.

### Disc Capacity Warnings

| Used | Level | Action |
|---|---|---|
| < 80% | INFO | Normal |
| ≥ 80% | WARN | "~N sessions remaining at current delta size" |
| ≥ 95% | ERROR | "Next sync may fail. Insert new disc soon." |

Remaining-session estimate uses rolling average of last 3 session sizes, not a fixed number.

At ≥80%, cron output contains a WARN line -- triggers email if cron is configured to mail on output. No external notification system required.

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

Config file is optional. All values have sensible defaults. Per-invocation flags override config.

---

## Cron Integration

```cron
0 2 * * 0 oddarchiver sync ~/Documents/ToArchive --device /dev/sr0
```

Exit codes:
- `0` -- success (changes burned, or no changes detected)
- `1` -- error (disc absent, full, burn failed, verify failed)

No output on no-change runs. Errors go to stderr and log file. Compatible with cron email-on-stderr behavior.

---

## Disc Capacity Planning

### 25GB BD-R (single layer)

| Sessions | Approx used | Notes |
|---|---|---|
| 1 (init) | ~2.7GB + 20MB | Full snapshot of 2.7GB source |
| 2–5 | +10–50MB/session | Typical small-file deltas (kdbx, configs) |
| 6–8 | approaching capacity | Depends on delta sizes |

Realistic lifespan for a Tier 1 homelab archive (keys, configs, small docs): **years of weekly syncs** before filling. A 2.7GB source with infrequent small changes may produce <10MB deltas per session.

### 100GB BDXL (triple layer)

Fits all of Tier 1 and Tier 2 comfortably with years of delta history before requiring a new disc.

---

## What to Archive (Target Data)

### Tier 1 -- Archive This (small, critical, slow-changing)

These are the files where loss is catastrophic and reconstruction from scratch is measured in weeks.

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

**Estimated Tier 1 size: 1–5GB.** A single 25GB disc is generous.

### Tier 2 -- Consider It (medium value, worth archiving if it fits)

These are recoverable but painful to reconstruct manually.

- Gitea / Forgejo database dump (SQLite = single file; small PostgreSQL installs)
- Nextcloud `config.php` and encryption keys (not user data -- too large)
- Home Assistant `/config/` (minus large media)
- Wiki.js / BookStack database exports
- Vaultwarden data directory
- Paperless-ngx database and search index
- Immich database dump (metadata only; photos go elsewhere)
- Jellyfin metadata database

**Estimated Tier 2 size: 2–20GB depending on installs.**

- 25GB disc: Tier 1 + small Tier 2 installs
- 100GB disc: Tier 1 + all Tier 2 + years of delta history

### Tier 3 -- Wrong Medium (don't use BD-R for these)

| Data | Reason | Better medium |
|---|---|---|
| OpenSearch / Elasticsearch indices | Large, fast-changing, rebuilt from source | Object storage / cold HDD |
| Ceph data | Ceph is already redundant | Ceph |
| VM disk images | Too large; use hypervisor snapshots | SAN / NAS |
| Container images | Pull from registry | Registry with backup |
| Log archives | Too large, append-only already | Object storage |
| Nextcloud user data | Too large | NAS + Restic |
| Jellyfin / Immich media | Too large; separate archival strategy | HDD / cold storage |

---

## Implementation Notes

- Python 3.10+
- `pathlib` throughout; no `os.path`
- `subprocess.Popen` with `stdin=PIPE, stdout=PIPE` for xdelta3 (no temp files)
- `concurrent.futures.ThreadPoolExecutor` for per-file parallelism (I/O bound)
- Staging uses `tempfile.mkdtemp` with cleanup in `finally`
- All disc reads go through a single `BurnBackend` interface (mockable for tests; swapped for `ISOBackend` in test mode)
- Manifests written atomically (write to `.tmp`, rename)
- `shutil.disk_usage` for staging space; `dvd+rw-mediainfo` stdout parsed with regex for disc space
- `argon2-cffi` for Argon2id KDF (passphrase mode)
- `pyage` or `age` CLI subprocess for age key operations
- No Vault dependency -- Vault integration is ODDArchiver-server's concern
- `BurnBackend` interface in `disc.py` abstracts real disc vs ISO; all pipeline code above the backend is mode-agnostic

---

## Future Considerations

- **Multi-drive redundancy** (`--mirror /dev/sr1`): burn identical sessions to two drives simultaneously; store one on-site, one off-site. Stretch goal.
- **PAR2 recovery blocks**: write PAR2 data into each session for bit-rot correction beyond what verify detects
- **`oddarchiver diff --session A --session B`**: show what changed between two sessions
- **Shell completion**: bash/zsh/fish completions for all commands
- **ODDArchiver-server integration**: ODDArchiver exposes a Python API (not just CLI) for ODDArchiver-server to call directly