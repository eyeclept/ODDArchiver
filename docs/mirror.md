# Mirror Mode

ODDArchiver can write each session to two drives simultaneously using `--mirror`. Both drives receive identical content; the second drive acts as a redundant copy.

---

## Flag usage

`--mirror` is accepted by `init` and `sync`. Pass the path to the second drive or ISO:

```bash
# Physical drives
oddarchiver init /my/source --device /dev/sr0 --mirror /dev/sr1 --label ARCHIVE

oddarchiver sync /my/source --device /dev/sr0 --mirror /dev/sr1

# ISO files (--test-iso mode)
oddarchiver init /my/source --test-iso primary.iso --mirror mirror.iso

oddarchiver sync /my/source --test-iso primary.iso --mirror mirror.iso
```

When `--test-iso` is active the mirror path is treated as a second ISO file. When writing to physical drives the mirror path is a device node.

---

## Burn order

1. Primary burn completes.
2. Mirror burn is attempted with the same staging content.
3. If the mirror burn fails, the command exits 1 and logs ERROR. The primary disc already holds the session; the cache is **not** updated.
4. On success, post-burn fast verify runs against the primary disc, then the cache is updated.

---

## Failure behaviour

If the primary burn fails, the command exits 1 immediately and the mirror is not attempted.

If the primary burn succeeds but the mirror fails:

- `ERROR` is logged: `Mirror burn failed for session N: <reason>`
- The command exits 1.
- The primary disc holds the new session with a manifest that lists both intended drives.
- `oddarchiver status` detects the missing mirror and reports it.

To recover a missing mirror, re-run `sync` with `--mirror` against the intact primary drive. Because the session was already written to the primary, the sync detects no source changes and exits 0. The mirror is **not** retroactively populated in this case — a full restore to a new disc is required to create a second copy of historical sessions.

---

## Manifest drives field

Each session manifest includes a `drives` list recording the drive identifiers written at burn time:

```json
{
  "session": 0,
  "drives": ["/dev/sr0", "/dev/sr1"],
  ...
}
```

For ISO mode the paths are filesystem paths. For physical drives they are device nodes. This field is written before any burn so both copies carry the same manifest.

---

## Status and mirror health

`oddarchiver status` reads the `drives` field from each manifest and checks whether all listed drives are accessible:

```
Mirror health:
  session_000: /path/to/mirror.iso [OK]
  session_001: /path/to/mirror.iso [MISSING]
```

For ISO paths, accessibility is determined by file existence. For device paths (`/dev/...`), the status command reports the path without probing the device.

A `MISSING` entry means the mirror ISO file no longer exists or was never created (burn failure). An ERROR is also logged to the log file.

---

## Recovering a missing mirror

1. Restore from the intact primary disc to a temporary directory.
2. Run `init` against the replacement mirror with `--test-iso new_mirror.iso`.
3. Sync each session's source snapshot manually if needed, or treat the new ISO as a fresh archive going forward.

For full redundancy reconstruction, contact the docs for `restore` and `verify`.

---

## See also

- [docs/quickstart.md](quickstart.md) — basic init and sync walkthrough
- [docs/restore.md](restore.md) — recovering files from a single drive
- [docs/verify.md](verify.md) — checking integrity after a mirror burn
