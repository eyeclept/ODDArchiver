# Partial and failed write recovery

A session interrupted mid-write on BD-R SRM+POW cannot be resumed. The next
successful `append()` starts a new session. The interrupted session's
partially written blocks are wasted, unrecoverable capacity, like any
write-once medium.

This document is about never compounding that loss. The failure modes
below exist so oddarchiver does not miscount already-good sessions or
re-burn them as if they were new.

## What a power cut or USB reset leaves behind

growisofs `-M` was writing session N. The process died from a signal, or
the drive reported a SCSI Unit Attention / "Power on, reset, or bus
device reset" fault.

- Sessions `000` through `N-1` are still on the disc. They were closed
  before this burn started.
- Session N may have a directory, a truncated manifest, or nothing the
  ISO tree can name. Those blocks cannot be reused.
- The drive can then return a torn read of an *earlier* session. That
  looks like `FileNotFoundError` on `session_000/manifest.enc` even
  though the file is there.

The 2026-09-16 incident was this last case. Session 002 failed mid-append.
The next `sync` scanned manifests, treated a torn read of session 000 as
"this archive has zero sessions", and was about to restage all 7578 source
files as new. A follow-up `history` and a scratch restore both showed
sessions 000 and 001 intact.

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
cure for a torn *read*, but it is also easy to do before you know whether
the last burn closed.

## The last-known-good ledger

Path: `~/.local/state/oddarchiver/<label>.ledger.json`
(or `$XDG_STATE_HOME/oddarchiver/<label>.ledger.json`).

```json
{"confirmed_sessions": 2, "last_verified": "2026-09-16T12:00:00Z", "label": "ARCHIVE"}
```

The file is written only after a session's post-burn fast verify passes.
It is not the cache. `--no-cache` and a cache wipe do not remove it.

If a later scan returns fewer readable sessions than `confirmed_sessions`,
that is a read failure, not a new archive. `sync`, `init`, `status`,
`history`, `restore`, and `verify` raise instead of falling through to a
from-scratch diff.

A missing or corrupt ledger does not crash. It degrades to cautious: an
initialized disc that yielded zero readable manifests is still a loud
failure. The ledger does not protect against a first-ever run on a
machine that has never verified this disc, if the disc also looks blank
to `mediainfo`.

The ledger does not resume a torn write. It only stops you from treating
already-confirmed sessions as absent.

## `--recover-scan`

Off by default on `init`, `sync`, and `status`.

Use it only after you have confirmed that the missing sessions are
genuinely gone. It logs a WARNING naming the unconfirmed sessions and
treats the readable set as authoritative.

Do not pass `--recover-scan` to "just make sync work" after a USB glitch.
Eject, reinsert, and retry first.

## Worked example (the 2026-09-16 incident)

1. Sessions 000 and 001 were on the disc (1244 files / 2.0 GiB, then
   5260 files / 5.0 GiB). The ledger, after those verifies, held
   `confirmed_sessions: 2`.
2. `sync` for session 002 died mid-`growisofs -M` from a power/USB reset.
   oddarchiver now raises `BurnInterrupted`, re-reads sessions 000-001,
   logs ERROR for session 002, and runs `verify --level fast` before it
   returns.
3. The next `sync` hit a torn read of `session_000/manifest.enc`. The
   old scanner caught `FileNotFoundError` and returned an empty list.
   `build_disc_state` then saw no files, so every source path looked new.
4. With this recovery path, that empty list is compared to the ledger.
   Coverage is short of 2. `sync` exits nonzero:

   ```
   error: Read failure for session 000: cannot treat as absent; 2 session(s)
   (000-001) should exist ...
   ```

   No staging, no third burn, no rewrite of the 7 GiB already on the disc.
5. After an eject/reinsert (or a later clean read), `history` and
   `verify --level fast` see both confirmed sessions again. The next
   `sync` writes session 003, not a replay of 000-001. Session 002's
   partial blocks stay wasted.
