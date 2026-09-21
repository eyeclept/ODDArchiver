# Staging sessions

`oddarchiver.session.build_staging` scans the source tree, diffs it against
disc state, encodes every changed or new file to its final on-disc blob,
then packs those blobs into one or more session directories.

## Staging directory layout

Each returned chunk is a staging root:

```
oddarchiver_staging_NNN/
  .lock
  session_NNN/
    full/<blob_id>
    deltas/<blob_id>
    manifest.json
```

`blob_id` is `sha256("{session}:{rel_path}")`. Original path names do not
appear in the blob filenames. The PID lockfile is written as soon as the
directory is created so a second process cannot delete an in-flight build.

## Return contract

```
list[tuple[Path, int, bool]]
# (staging_path, session_n, is_new_volume)
```

A changeset that fits on the current medium returns exactly one tuple with
`is_new_volume=False`. A changeset that needs a split returns two or more
tuples: the first stays `is_new_volume=False` (current medium), every later
tuple is `is_new_volume=True` with contiguous session numbers
(`session_n`, `session_n+1`, ...). Combined file content covers every
changed or new file exactly once. A file larger than one fresh volume is
split across dedicated bins (see "Volume-split files" below).


`cli._burn_volume_chunks` burns every chunk from one `build_staging()` call
in sequence, swapping media between them when a chunk is a new volume. See
"Automatic staging-space chunking" below for the separate, outer loop that
calls `build_staging()` more than once in a single `init`/`sync` invocation.

## Encoding pre-pass

Disc budgets are computed before any `encrypt_file` call. A file whose
encoded size would exceed `full_budget` is range-encrypted in one pass,
one streamed blob per volume chunk. Other files still encode as delta or
full before packing. New files that fit one volume are always full blobs.

## Packing budgets

Ordinary blobs pack greedily, largest first (ties broken by path).

- First chunk: `backend.mediainfo().remaining_bytes * SPACE_SAFETY_MARGIN`
  (`SPACE_SAFETY_MARGIN` is 0.95). This is remaining space on the disc or
  ISO that is already loaded.
- Every later chunk: a full fresh medium times the same margin.
  `ISOBackend.disc_size` (the configured `--disc-size`) is used when it is a
  positive int. `DiscBackend` and anything without an int `disc_size` fall
  back to `disc.BDXL_DISC_BYTES` (93 GiB, BDXL usable headroom).

Named presets and write engines are listed in [media.md](media.md).

## Volume-split files

A file that cannot fit on one fresh volume is split at disc-capacity
boundaries (minus `SPACE_SAFETY_MARGIN`). Chunk 0 uses the budget that is
active when the file is reached (`first_budget` on the current medium,
`full_budget` after). Later chunks use `full_budget`. The last chunk takes
the remainder.

Each chunk is its own `_PreparedBlob` (`kind="full"`, `streamed=True`)
with `volume_chunk` / `volume_chunk_count` set. `encrypt_file` writes each
range as a complete streamed blob. `decrypt_file` needs no extra params.
`full_size_bytes` and `result_checksum` on every chunk are the original
whole file, so `ls` / `browse` / `history` show the real total.

Every split chunk gets its own dedicated bin. It does not share a bin with
another file or with another chunk of the same file. That keeps chunk order
ascending even when encoded sizes tie. It wastes space when a small file
could have filled the rest of a chunk's disc.

A previously split file that later fits one disc re-encodes as a single
blob with no `volume_chunk` fields. Split files never take the delta path.

Deletions are recorded only on the first chunk's manifest.

## Staging-filesystem check

Before any blob is encoded, `build_staging` estimates source bytes for every
new or changed file and compares that to free space on the staging
filesystem (often a small tmpfs under `$XDG_RUNTIME_DIR`). If the estimate
is at or above `free * SPACE_SAFETY_MARGIN`, it raises `SystemExit(1)`.
That check is separate from disc/ISO packing.

`cli.py`'s `init`/`sync` handlers no longer let this check fire in the
normal case (see "Automatic staging-space chunking" below): they partition
the changeset ahead of time so each `build_staging()` call only ever
receives a group that already fits. The check itself is unchanged and stays
in place as a safety net for any direct caller of `build_staging` that
bypasses that outer loop -- tests, scripts, or a future caller.

## SIGINT

A SIGINT handler is installed at entry and only sets a flag. Scan, encode,
and write loops check the flag and raise `KeyboardInterrupt`. Every staging
directory created by the call is removed in that case.

## Lockfile

Staging paths are deterministic (`oddarchiver_staging_{session:03d}`). If
the directory exists, `_check_staging_lock` reads `.lock`:

- Missing or non-integer contents: stale, safe to remove.
- PID that no longer exists: stale, safe to remove.
- PID that still exists: raise `RuntimeError` and leave the directory.

## Automatic staging-space chunking

`session.partition_by_staging_budget(paths, source, staging_root=None)`
greedily bin-packs a set of source-relative paths, sorted for determinism
and ordered by `(source / p).stat().st_size`, into groups whose running
total stays under `shutil.disk_usage(staging_root or
_default_staging_root()).free * SPACE_SAFETY_MARGIN`. A changeset that
fits returns `[sorted(paths)]`: one group, indistinguishable from the
ungrouped path this replaces. A single path whose own size exceeds the
whole budget raises `RuntimeError` naming that file: the local staging
budget cannot grow between groups the way a fresh medium grants fresh
headroom to a later disc-capacity chunk, so an oversized file is
unrecoverable without freeing local staging space.

`cli.py`'s `_run_init` and `_run_sync` call this once, on `new_files |
changed`, after scanning the source and diffing it against disc state.
Two or more groups print a one-line notice
(`Source needs ~X; staging has ~Y free. Splitting into N automatic
passes.`) and then loop: each group gets its own `build_staging()` call
plus its own `cli._burn_volume_chunks()` call, appended as an ordinary
sequential session on the current volume, no user interaction, no new
`VolumeMarker`. Group 0 keeps whatever parameters the triggering command
(`init` or `sync`) already used; every later group behaves like an
ordinary sync append regardless of which command started the run.

Each group's `build_staging()` call needs a `disc_state` view scoped to
only that group's own paths, not the real, whole-archive `disc_state`:
otherwise every later group would re-see every earlier group's files as
new or changed again. `cli._mask_diff_to_group(current_state, disc_state,
group_paths, group_index)` builds that view: a path outside the group is
faked as already matching `current_state` (hidden from this pass), a path
inside the group keeps its true prior value or is left absent if it is
genuinely new, and a deleted path (present in `disc_state`, absent from
`current_state`) keeps its true value only on group 0 and is omitted
entirely from every later group, so a deletion lands in exactly one
session's manifest.

This is orthogonal to the disc-capacity chunking described above under
"Packing budgets": that logic splits when the *target disc/ISO* doesn't
have room and produces new volumes needing a physical swap. Staging-space
chunking splits when the *local staging filesystem* doesn't have room and
produces additional sessions on the *same* volume. The two compose without
conflict: the outer, staging-space loop decides how many files enter each
`build_staging()` call; the already-shipped disc-capacity packing inside
that call independently decides whether that group's own encoded blobs
additionally need a disc swap.

Worked example: an 8 GB staging budget against a 12 GB source. `init`
partitions the changeset into two groups, burns the first as session 0
exactly as an ordinary `init` always has, prints the splitting notice, then
burns the second as session 1 on the same volume with no prompt -- the
same end state as running `init` followed by `sync` by hand, in one
command.

A non-zero return from any group's burn stops the loop immediately.
Already-burned earlier groups stay committed and cached; a later plain
`sync` picks up whatever remains via the normal disc-state re-diff.
