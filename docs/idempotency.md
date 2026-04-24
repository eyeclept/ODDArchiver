# Idempotency

ODDArchiver is designed so that every command can be run more than once safely.
Re-running a command that has already completed produces the same result without
side effects or data corruption.

---

## Command-by-Command Behaviour

### `init`

- If the disc or ISO already has a session 0, `init` exits 0 with a warning:
  ```
  warning: disc already initialized; skipping init.
  ```
- No second session is written.
- Safe to run in automation where the disc state may be uncertain.

### `sync`

- If the source directory is identical to the last committed disc state (no
  changed, new, or deleted files), `sync` exits 0 silently.
- No staging directory is created, no burn occurs, and no output is produced —
  safe for cron jobs.
- Running `sync` N times with an unchanged source produces exactly one session
  on disc (the initial `init` session).

### `restore`

- Non-destructive by default: a file in the destination whose sha256 matches
  the target session is not re-written.
- Use `--force` to overwrite unconditionally.

### `verify` / `history` / `status`

- Read-only; always safe to re-run.

---

## Interrupted-Burn Recovery

If a burn is interrupted (SIGKILL, power loss, `growisofs` failure), the cache
is **not** updated.  On the next `sync`, oddarchiver re-diffs from the last
confirmed disc state (the most recent successfully burned and fast-verified
session), so no data is silently lost or duplicated.

The cache is updated only after:
1. `backend.init()` or `backend.append()` returns without error, **and**
2. Post-burn fast verify passes.

If either step fails, cache remains at the previous session's state.

---

## Stale Staging Directory Cleanup

`build_staging` uses a deterministic staging directory name:

```
{tmpdir}/oddarchiver_staging_{NNN}/
```

where `NNN` is the zero-padded session number.

If that directory already exists when `build_staging` is called (left over from
a prior crash), it is logged as a warning and removed before a fresh directory
is created:

```
WARNING [oddarchiver.session] Stale staging dir found for session 001; removing and rebuilding.
```

This guarantees that staging content always reflects the current run, never a
partial prior attempt.

---

## Cron Usage Guide

A typical cron entry for daily incremental backup:

```cron
0 2 * * * oddarchiver sync /data/source --device /dev/sr0
```

- No output on no-change runs (cron will not send mail).
- Exit 0 on no change, exit 1 on any error (cron can alert on non-zero exit).
- If the disc fills up, exit 1 with an error message; the next run re-tries
  after the disc is changed.

