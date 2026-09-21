# Multi-volume archives

A logical archive can span more than one disc. Sessions number across the
whole archive. Each physical disc is one volume.

## Archive, volume, and session numbers

`oddarchiver.volume.VolumeMarker` is the identity record for one disc. The
file is plaintext `volume.json` at the staging root, next to `session_NNN/`.

Fields:

- `archive_id` - hex Universally Unique Identifier (UUID) shared by every
  volume of this archive
- `volume_index` - 0-based disc number
- `first_session` - first global session number stored on this disc
- `created_at` - Coordinated Universal Time (UTC) timestamp from marker
  creation
- `previous_volume_label` - Universal Disk Format (UDF) label of the disc
  just swapped out, or `""` on volume 0

`init` writes a marker on chunk 0 even when the changeset fits on one disc.
`archive_id` is `uuid.uuid4().hex`. Later volumes copy that id.

Session numbers are global and contiguous. Volume 0 may hold sessions 0-41.
Volume 1 then starts at session 42 (`first_session=42`).

`sync` computes the next session as
`marker.first_session + len(manifests)` on the loaded disc. It does not
trust `dvd+rw-mediainfo` session count. BD-R SRM+POW always reports
`session_count=1`.

`read_volume_marker` returns `None` when `volume.json` is missing or
unreadable. That disc is a legacy single-volume archive. Restore does not
read or write restore-state for it.

`build_staging` encodes every new or changed file first. It then packs
blobs, sorted by path, into as many session directories as needed.

- Chunk 0 uses remaining space on the loaded medium times `SPACE_SAFETY_MARGIN` (0.95).
- Later chunks use a full fresh medium times the same margin.
- `ISOBackend.disc_size` (`--disc-size`) is that fresh budget when it is a positive int.
- Otherwise the budget is `disc.BDXL_DISC_BYTES` (93 GiB).
- Named presets and write engines are listed in [media.md](media.md).

A blob larger than its chunk budget raises `RuntimeError` and names the file.
One file never spans two discs. The first chunk records deletions on its
manifest.

The return value is `list[tuple[Path, int, bool]]`:
`(staging_path, session_n, is_new_volume)`. Chunk 0 has
`is_new_volume=False`. Every later chunk has `is_new_volume=True` and the
next session number.

## Disc-swap during init and sync

`cli._burn_volume_chunks` burns each chunk, then swaps media when more
chunks remain.

On a real drive (`DiscBackend`) the prompt is:

```
Disc <label> is full ({used}/{total}). Insert a new disc for volume {k+1} and press Enter (or 'q' to abort; already-burned data is durable and this sync is resumable).
```

Enter continues. `q` or end of file (EOF) on `input()` returns exit 1.
Chunks already burned stay on disc and in the cache. The next `sync`
re-diffs from disc state and stages only leftover files.

The newly inserted medium must report `session_count == 0`. A non-blank
disc raises `RuntimeError`. Volume k+1 cannot overwrite volume k.

Before `backend.init()` on the new disc, the next chunk gets a
`VolumeMarker`. `volume_index` increments. `first_session` equals the
session number of that chunk. `previous_volume_label` equals the label
just swapped out.

`--test-iso` / `ISOBackend` skips the prompt. Overflow opens
`<original>.vol{k}.iso` with the same `disc_size`. The original path is
always the stem: `archive.iso` volume 2 is `archive.vol2.iso`, never
`archive.vol1.vol2.iso`.

`--mirror` plus a 2+ chunk split raises `RuntimeError`. Mirror burns only
run on a single-volume pass.

## Multi-volume restore

Restore is one disc per invocation, oldest first.

```
oddarchiver restore /tmp/out --device /dev/sr0
oddarchiver restore /tmp/out --test-iso archive.iso
oddarchiver restore /tmp/out --test-iso archive.vol1.iso
```

`restore()` reads the `VolumeMarker` and the manifests on the loaded disc.
File chains come from this disc only. A delta whose full blob lived on an
earlier applied volume uses dest bytes plus the replay dict as the base.

Restore-state does not live inside dest. The path is
`$XDG_STATE_HOME/oddarchiver/restore-<digest>.state.json`, or
`~/.local/state/oddarchiver/` when `XDG_STATE_HOME` is unset. The digest
is `sha256("{archive_id}:{resolved dest}")[:16]`. dest stays a
byte-for-byte mirror of the source.

The state file records `archive_id`, dest, the `--session` target,
applied `volume_index` values, per-volume session ranges and labels, and
a compact per-file replay dict (checksum, last session, `has_full`).

Re-running restore on the same dest skips volumes already in
`applied_volumes`. `--force` still means "always rewrite dest files". It
does not redo already-applied volumes.

A delta with no dest base is `unresolved`, not a failure. The
command-line interface (CLI) prints:

```
Restore needs volume 1 (label VOL1, sessions 1-1) next. Insert it and re-run 'oddarchiver restore'.
```

Exit code stays 0 when `failures == 0`. Insert the named volume and run
restore again.

A volume commits to restore-state only after a zero-failure pass with no
unresolved files.

## Recovery mode for a lost volume

`--skip-volume N` is repeatable (`type=int`, `action=append`, dest
`skip_volumes`). At least one value activates recovery mode for that
invocation. There is no separate `--recover` flag.

```
oddarchiver restore /tmp/out --test-iso archive.iso --skip-volume 1
oddarchiver restore /tmp/out --test-iso archive.vol2.iso --skip-volume 1
```

Restore never reports a skipped volume as still needed. The walk back
from the current marker treats it as gone and continues to an earlier
volume that is not skipped and not yet applied. If none remain, `needed`
is `None`.

Without `--skip-volume`, a final checksum mismatch is still a hard
`failed`. Recovery mode does not change that path.

With `--skip-volume` set, restore retries a chain that cannot reach its
target checksum, one entry at a time:

1. A `full` entry restarts from that blob. The bytes must hash to the
   `result_checksum` on that entry.
2. A `delta` entry applies only if `sha256(current)` equals
   `entry.source_checksum`. After apply, the bytes must hash to
   `result_checksum`.
3. The first failed precondition or post-apply check stops the chain.

Three result buckets:

- `restored` - dest matches the target session checksum
- `stale` - dest holds an older internally consistent version. The pair
  is `(path, session recovered at)`, the last validated entry, not the
  target session
- `missing` - no validated prefix exists. Restore leaves dest unchanged
  for that path

`stale` and `missing` do not increment `failures`. They do not change the
exit code. Restore does not raise on a missing file.

`RestoreResult` still unpacks as `(restored, failures)`. `stale` and
`missing` are attributes only.

When every earlier unapplied volume is skipped, a file that 21d would
leave `unresolved` becomes `missing` so the current volume can still
commit.

The CLI prints a per-file listing after restore:

```
  stale:
    photos/cat.jpg  (session 0)
  missing:
    notes/secret.kdbx
```

## Worked example: 3-volume archive, middle volume lost

Volume 0 (session 0, label `VOL0`) holds `kept.txt` and `stale.txt` as
full blobs.

Volume 1 (session 1, label `VOL1`) stores a delta for `stale.txt` and a
new full blob `missing.txt`.

Volume 2 (session 2, label `VOL2`) stores deltas for `stale.txt` and
`missing.txt` against volume 1, plus a new full blob `later.txt`.

You no longer have volume 1. Restore volume 0, then volume 2:

```
oddarchiver restore /tmp/out --test-iso vol0.iso --skip-volume 1
oddarchiver restore /tmp/out --test-iso vol2.iso --skip-volume 1
```

- `kept.txt` restores from volume 0. Volume 1 never touched it.
- `later.txt` restores from volume 2. It is a full blob on that disc.
- `stale.txt` cannot apply the volume-2 delta: `source_checksum` is the
  volume-1 hash. dest keeps the volume-0 bytes. The file is `stale` at
  session 0.
- `missing.txt` has no dest base. Its only full blob was on volume 1.
  The file has no recoverable version and stays absent from `/tmp/out`.

`failures` is 0. `needed` is `None` because volume 1 is skipped and
volume 0 is applied. The same restore without `--skip-volume` still
reports `Restore needs volume 1` and treats the unreadable delta as
failed or unresolved, matching 21d.
