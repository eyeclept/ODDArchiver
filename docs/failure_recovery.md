# Partial and failed write recovery

A session interrupted mid-write on BD-R SRM+POW cannot resume. The next
successful `append()` starts a new session.

The interrupted session leaves partially written blocks behind. Those blocks
waste unrecoverable capacity, as with any write-once medium.

This document explains how oddarchiver avoids adding to that loss. The
failure modes prevent it from miscounting good sessions or burning them again
as new sessions.

## Restore file safety

A failed restore must not remove an existing destination file. A streamed
restore decrypts to a private file in the destination directory. It checks
the checksum before it replaces the destination file.

Every normal restore writes a private temp file with mode 0600 in the
destination directory. It checks the file size, then uses `os.replace` to
replace the destination file. A write error such as ENOSPC leaves the old
destination file unchanged. Restore never truncates the destination in place.

A file split across volumes uses `<name>.oddarchiver-partial` with mode
0600. Restore writes each chunk to that file. It replaces the destination
file only after the complete file passes its checksum. A write error removes
the partial file and keeps the old destination file.

Restore opens the partial file with `O_NOFOLLOW` on create and on append. It
checks the open descriptor with `fstat`. The partial file must be a regular
file owned by the current user. A symlink planted at the partial path makes
the open fail. The symlink target stays unchanged. The first chunk also uses
`O_EXCL`, so restore cannot reuse an existing link.

Restore removes private plaintext temporary files after each operation. It
logs a cleanup error and counts it as a restore failure. It never hides the
error that stopped the restore.

A suspect manifest in the requested session range makes restore exit with
status 1. The error names the session. Recovery mode can restore other
files, but it still counts each suspect session as a failure.

A blob read or decrypt error on an available disc is a failure in recovery
mode. Restore reports `missing` only when a disc is unavailable or a
volume chunk is absent.

## Recover older delta chains

Some older sync sessions stored a delta against the previous delta blob,
not against the restored file content. Restore first applies each delta to
the restored content. If that fails, it tries the previous delta blob as
the source. It accepts only a result that matches the manifest checksum.
Restore logs a warning with the path and session when it uses that source.

`restore --all-disks` keeps encrypted blobs in its spool during a pause.
On resume, it reads the blobs from the spool before it rebuilds the chain.
Keep the spool until restore finishes. If a blob is absent, restore reports
a failure instead of writing unverified bytes.

`verify --level full` uses the same replay rule. It reports a file as
failed if neither source produces the expected checksum.

## Verified 2026-09-21: this is a protocol limit, not a missing feature

The team proposed and evaluated a per-file recovery design. The design
would write the session manifest first and copy each blob atomically. It
would then verify and record each file's good/broken/unwritten status after
the write.

The goal was to recover files that landed successfully before an interruption.
For example, files 1-5 of a 10-file session could land before a kill partway
through file 6. The alternative writes off the whole session.

A real BD-R burn through `growisofs` cannot use that design. This is a
hardware/protocol constraint, not a gap in the current oddarchiver code.
This conclusion comes from primary sources, not from this document or from
earlier oddarchiver assumptions:

- `growisofs` streams the whole new session to the next writable address on
  the disc. As its literal last step, it copies the new ISO9660 Primary
  Volume Descriptor back to the fixed root location (LBA 16). Every reader
  checks that location first. The log says `copying volume descriptor(s)`.
  Source: `growisofs.c` and `growisofs_mmc.cpp` (Andy Polyakov,
  dvd+rw-tools).

- Until that one write lands, LBA 16 still names the *previous* session.
  A kill during file 6 happens before that write. No reader can then see
  file 1, file 5, or any other file from the killed session. This includes
  a normal mount, `xorriso`, and the `read_paths()` call inside oddarchiver.
  Their raw bytes may still sit in an unindexed region of the disc.

- No host-side footer or manifest scheme can make those unindexed bytes
  filesystem-visible again after the fact.

- UDF 2.60 SS 6.15 and 6.16.3 describe the same all-or-nothing
  publication rule for Pseudo-OverWrite media. Thomas Schmitt
  (libburnia/xorriso) independently confirms it. On POW BD-R, the
  on-disc superblock points at the newer tree only after that tree is
  fully written and overwrites the old pointer.

- This same mechanism keeps sessions `000` through `N-1` safe from a failed
  append. At that one write, the published disc state flips atomically from
  "session N-1 is current" to "session N is current".

- Therefore, a failed append cannot corrupt a prior session or expose a
  partial one. The two guarantees are the same guarantee.

This limitation applies to `DiscBackend` (real optical media) only.

`--test-iso` (`ISOBackend`) publishes each session with the same
all-or-nothing rule. It copies the session into a temp directory
`.session_NNN.partial-<random>` next to the session directories. Then it
renames that directory to `session_NNN` in one step. Root files such as
`volume.json` also use a temp file and one rename.

- A failed or killed `--test-iso` write leaves no partial `session_NNN`.
  The command exits nonzero. The session count does not change. Prior
  sessions do not change.

- A leftover `.partial-` item never counts as a session. The program never
  reads it. The next `init` or `sync` on that ISO removes it and logs a
  warning.

- To recover, run the same command again.

Do not treat `--test-iso` behavior in this area as evidence for what real
media can do, in either direction.

## What a power cut or USB reset leaves behind

growisofs `-M` wrote session N. A signal killed the process, or the drive
reported a SCSI Unit Attention / "Power on, reset, or bus device reset" fault.

- Sessions `000` through `N-1` remain on the disc. This burn started after
  the previous burn closed them.

- Session N may have a directory, a truncated manifest, or nothing the
  ISO tree can name. No write can reuse those blocks.

- The drive can then return a torn read of an *earlier* session. That looks
  like `FileNotFoundError` on `session_000/manifest.enc`, although the file
  is there.

In the 2026-10-16 BD-R SRM (POW) test, a signal killed growisofs during
`sync`. The write consumed about 161 MB (82,688 blocks), but left no visible
new session. The prior session remained intact. A plain retry of the same
`sync` appended after those unused blocks, and verification passed.

The 2026-09-16 incident was this last case. Session 002 failed mid-append.

The next `sync` scanned manifests and treated a torn read of session 000 as
"this archive has zero sessions". It was about to restage all 7578 source
files as new. A follow-up `history` and a scratch restore showed sessions
000 and 001 intact.

## How status and verify surface it

`oddarchiver verify --level fast` reports a trailing dangling session as:

```
Session 002: INCOMPLETE (interrupted burn, not corruption)
```

That line is not `FAIL`. `FAIL` means a complete session is corrupted.
`INCOMPLETE` means the last burn did not finish.

`oddarchiver status` prints the same state and tells you:

```
Do not eject; run 'oddarchiver verify --level checksum' before another sync
```

Do not eject until you have a checksum pass. A tray cycle is the usual
cure for a torn *read*. It is also easy to cycle the tray before you know
whether the last burn closed.

## The last-known-good ledger

Path: `~/.local/state/oddarchiver/<label>.ledger.json`
(or `$XDG_STATE_HOME/oddarchiver/<label>.ledger.json`).

```json
{"confirmed_sessions": 2, "last_verified": "2026-09-16T12:00:00Z", "label": "ARCHIVE"}
```

oddarchiver writes the file only after a session passes the post-burn fast
verify. The file is not the cache. `--no-cache` and a cache wipe do not
remove it.

If a later scan returns fewer readable sessions than `confirmed_sessions`,
that is a read failure, not a new archive. `sync`, `init`, `status`,
`history`, `restore`, and `verify` raise instead of starting a from-scratch
diff.

A missing or corrupt ledger does not crash the program. It falls back to
cautious behavior. An initialized disc with zero readable manifests still
causes a loud failure.

The ledger does not protect a first-ever run on a machine that has never
verified this disc if the disc also looks blank to `mediainfo`.

The ledger does not resume a torn write. It only stops you from treating
already-confirmed sessions as absent.

## `--recover-scan`

Off by default on `init`, `sync`, and `status`.

Use it only after you confirm that the missing sessions are genuinely gone.
It logs a WARNING naming the unconfirmed sessions and treats the readable
set as authoritative.

Do not pass `--recover-scan` to "just make sync work" after a USB glitch.
Eject, reinsert, and retry first.

## Worked example (the 2026-09-16 incident)

1. Sessions 000 and 001 were on the disc (1244 files / 2.0 GiB, then
   5260 files / 5.0 GiB). After those verifies, the ledger held
   `confirmed_sessions: 2`.

2. `sync` for session 002 died mid-`growisofs -M` from a power/USB reset.
   oddarchiver now raises `BurnInterrupted` and re-reads sessions 000-001.
   It logs ERROR for session 002 and runs `verify --level fast` before it
   returns.

3. The next `sync` hit a torn read of `session_000/manifest.enc`. The
   old scanner caught `FileNotFoundError` and returned an empty list.
   `build_disc_state` then saw no files, so every source path looked new.

4. With this recovery path, `sync` compares that empty list to the ledger.
   Coverage is short of 2. `sync` exits nonzero:

   ```
   error: Read failure for session 000: cannot treat as absent; 2 session(s)
   (000-001) should exist ...
   ```

   No staging, no third burn, no rewrite of the 7 GiB already on the disc.

5. After an eject/reinsert (or a later clean read), `history` and
   `verify --level fast` see both confirmed sessions again. The next
   `sync` writes session 003, not a replay of 000-001. Partial blocks
   from session 002 stay wasted.

## Embedded restore software

`init` and `sync` accept `--embed-software minimal` or `--embed-software all`.
The flag is off by default. A disc burned without it does not carry the tool.

If you pass the flag, oddarchiver writes a plain directory named
`oddarchiver-software/` at the disc root. Those files are not encrypted.
A file browser can open them.

### How to find the software directory

1. Insert the disc.
2. Open the disc root in a file browser, or mount the disc.
3. Open the `oddarchiver-software` directory.
4. Read `README.txt`. It names the mode and the install steps.

### How to restore from that directory

Need Python 3.10 or later.

The oddarchiver package imports argon2-cffi and cryptography when it starts.
Install those libraries before you run restore or verify.

#### minimal mode

The directory holds the `oddarchiver` package. It can restore and verify.
It does not include `usb_reauth.py`.

1. Copy `oddarchiver-software/` to a working directory on the recovery machine.
2. Set `PYTHONPATH` to that directory. That directory is the parent of the
   `oddarchiver` package.
3. Install the crypto libraries:

   ```
   pip install oddarchiver[encrypt]
   ```

   You can also install the argon2-cffi and cryptography system packages.

4. Restore:

   ```
   python3 -m oddarchiver restore DEST --device /dev/sr0
   ```

   Replace DEST with the restore destination.
   For an ISO file, pass `--test-iso PATH` instead of `--device`.

5. Verify:

   ```
   python3 -m oddarchiver verify --device /dev/sr0
   ```

#### all mode

The directory holds the full source tree plus a `vendor/` directory.

1. Copy `oddarchiver-software/` to a working directory on the recovery machine.
2. Set `PYTHONPATH` to that directory.
3. Install the crypto libraries from `vendor/` with no network:

   ```
   pip install --no-index --find-links=vendor/ argon2-cffi cryptography
   ```

4. Restore and verify with the same commands as minimal mode.

### When a later sync rewrites the directory

A later `sync --embed-software ...` rewrites the directory only when the
installed oddarchiver version differs from `oddarchiver-software/VERSION`.
An unchanged version adds zero extra bytes on that sync.
