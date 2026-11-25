# Staging sessions

`oddarchiver.session.build_staging` scans the source tree and compares it with
disc state. It reuses an existing full blob when a new or changed path has
the same content checksum. It encodes all other changed or new files to
their final on-disc blobs, then packs those blobs into one or more session
directories. Each staged file uses the checksum of the bytes staging read,
not the scan checksum (see "Source changes during staging" below).

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
appear in blob filenames. The builder writes the PID lockfile when it creates
the directory. This stops a second process from deleting an active build.

## Return contract

```
list[tuple[Path, int, bool]]
# (staging_path, session_n, is_new_volume)
```

A changeset that fits on the current medium returns one tuple with
`is_new_volume=False`. A split changeset returns two or more tuples. The
first has `is_new_volume=False` for the current medium. Each later tuple
has `is_new_volume=True` and a contiguous session number
(`session_n`, `session_n+1`, ...).

Combined file content covers every changed or new file exactly once. A file
larger than one fresh volume is split across dedicated bins (see
"Volume-split files" below).

`cli._burn_volume_chunks` burns all chunks from one `build_staging()` call
in sequence. It swaps media between chunks for a new volume. See
"Automatic staging-space chunking" below for the separate outer loop.
That loop calls `build_staging()` more than once in one `init`/`sync` invocation.

## Encoding pre-pass

The builder computes disc budgets before any `encrypt_file` call. A file
whose encoded size would exceed `full_budget` gets one range-encryption
pass and one streamed blob per volume chunk.

Other files encode as delta or full before packing. The builder writes each
ordinary ciphertext to scratch space immediately. The prepared list keeps
paths and sizes, not ciphertext for all files. New files that fit one volume
are always full blobs.

The builder checks whether the checksum of the bytes staging read matches a
reusable full blob (on disc or another new file in this run). If it matches,
the builder writes a `type=full` manifest entry that points to that blob. It
stages zero extra bytes. For a moved file, staging checks the same condition.
The old path is deleted. The new path's checksum is already on disc. The
dedup key never uses only the scan checksum.

## Source changes during staging

A file can change between the scan and staging read, such as an active log.
The archive then holds the bytes staging read, under their own sha256.
The sync does not stop or skip the file.

- A file up to `STREAM_CHUNK_SIZE` is read once into memory. The builder
  hashes those bytes and encodes the same bytes as a delta or a full blob.
- A larger file goes through `encrypt_file(..., digest=...)`. The read that
  encrypts each chunk also hashes it. There is no second read.
- A volume-split file sends every range through one digest.

When the checksum of the bytes read differs from the scan checksum, the
builder logs a warning. It also prints one line to stderr:
`Warning: <path> changed while it was read. The archive holds the version
that was read.` It sets `result_checksum` and `current_state[rel_path]` to
the checksum of the bytes read, then continues. The CLI passes its own
`current_state`. Later automatic passes and the baseline use the new
checksum. The sync exits 0 when nothing else fails.

The next sync scans the source again. If the on-disk file differs from the
archived bytes, that sync stages it as an ordinary change.

Dedup follows the same rule. The builder reads a small file, then looks up
the checksum of the bytes read. It first hashes a large file whose scan
checksum names a dedup target. On a hit, the file becomes a reference.
On a miss, the builder encodes the file. That encode hashes its own bytes.
A file that changed after the scan never shares the blob of its scan content.

When packing converts a reference or a delta to a full blob on a later
disc, a delta uses its captured encrypted bytes. A reference reads the
source again, hashes those bytes, and applies the same warning rule.

## Delta cache content

For an ordinary changed file, staging reads the source once after the scan.
The delta, delta sidecar, and self-check use those captured bytes and their
checksum. Large streamed files do not use the delta path.

Staging sends all captured delta jobs to one parallel batch. It does not
encode each file separately.

Staging computes a delta from the captured bytes. Before the burn, it
applies the delta to the checked prior bytes and compares the result with
the captured bytes' checksum. A mismatch or failed `apply_delta` is an
encoder fault. Staging logs a warning and stages a full blob of the captured
bytes instead.

After a verified burn, the cache stores each delta entry's encrypted full
result. A sidecar beside the staging root holds this copy outside the disc
image. Staging rejects a linked or foreign-owned sidecar directory. The
writer checks decrypted sidecar content against `result_checksum` before
updating the cache. It removes the sidecar after success or failure.

The cache uses opaque SHA-256 names in `blobs/`. File modes are `0600`, and
directory modes are `0700`. The `0600` cache index holds the original
session and path keys. On first use, the cache moves blobs from the old
`blobs/<session>/<path>.blob` layout to opaque names. It checks each old
blob's size and drops bad entries with a warning. It resumes a migration if
the process stops after a move but before the index commit.

Full entries keep their encrypted on-disc blobs in the cache. A cache miss
reads the on-disc entry blob. A delta entry puts raw delta bytes in the
cache, not the rebuilt file. Before staging or dry-run delta encoding, the
writer checks decrypted prior bytes against `disc_state[rel_path]`. If they
differ, it logs the path and stages a full blob. This guard also catches
cache entries from older runs that contain delta bytes.

## Packing budgets

Ordinary blobs pack greedily, largest first, with ties broken by path.
A dedup reference has encoded size 0.
It uses only its manifest-entry budget.
Same-run references stay in the origin blob bin.

- First chunk: `backend.mediainfo().remaining_bytes * SPACE_SAFETY_MARGIN`
  (`SPACE_SAFETY_MARGIN` is 0.95). This is the free space on the disc or
  ISO that is already loaded. A medium with no image yet (`used_bytes == 0`)
  first loses the ISO scaffolding allowance.

- Every later chunk uses a full fresh medium, minus the ISO scaffolding
  allowance and encoded baseline bound, then multiplied by the same margin.
  `ISOBackend.disc_size` (the configured `--disc-size`) applies when it is a
  positive int. `DiscBackend` and any backend without an int `disc_size` use
  `disc.BDXL_DISC_BYTES` (93 GiB, BDXL usable headroom).

With fill enabled, the first chunk uses the oldest measured appendable volume.
That volume must have remaining space that can hold every planned blob.
The planned blobs include split chunks.
The burn appends to that volume.
It does not init a blank disc.
A blob that fits none of the measured volumes uses the inserted disc.
It then uses a new volume, as today.


Each packed file costs more than its encoded size:

- File data rounds up to a 2048-byte ISO9660 block.
- UDF and Rock Ridge directory records add `ISO_FILE_METADATA_BYTES`
  (2560 bytes) per file.
- The session manifest gains one entry. The builder measures that entry
  with the real path and largest field values, then adds the encryption
  bound.

The ISO scaffolding allowance is `ISO_SCAFFOLD_BYTES` (1 MiB).
genisoimage writes about 885 KiB of control structures before file data.
A medium of 1 MiB or less cannot hold a real image. For that medium, the
builder applies no scaffolding, block rounding, or UDF record cost. The
file cost is the encoded size plus the manifest entry.

Before packing new discs, the builder reserves the encoded baseline size.
Init and sync reserve this space on every new disc. The builder serializes
the closing disc baseline once. For each pending path event, it adds a
measured event size. That size is the measured JSON framing of one history
version, its state and origin entries, three JSON-escaped path lengths, and
two checksum lengths. It also reserves 4 measured volume records, plus
`VOLUME_CONTROL_BYTES` (1024 bytes) per record for `volume.json` and
`enc_mode.json`. The bound includes the encryption header and tags. An
archive whose baseline cannot fit needs a larger disc.

If the builder cannot remove a staging or scratch directory, it prints a
warning with the path to stderr. Remove that directory by hand because it
can hold decrypted plaintext.


[media.md](media.md) lists named presets and write engines.

## Volume-split files

A file stays whole when it fits one disc.
Pass `--split-large` on `init` or `sync` to restore greedy splitting.
That fills leftover space on the current disc.
A file still splits when it cannot fit on one fresh volume.
It also splits when its encoded size exceeds the 3 GiB blob cap (`BLOB_SIZE_CAP`).

A file that cannot fit on one fresh volume is split at disc-capacity
boundaries, minus `SPACE_SAFETY_MARGIN`.
Chunk 0 uses the active budget (`first_budget` on the current medium, `full_budget` after).
Later chunks use `full_budget`.
The last chunk takes the remainder.

Each chunk is its own `_PreparedBlob` (`kind="full"`, `streamed=True`)
with `volume_chunk` / `volume_chunk_count` set.
`encrypt_file` writes each range as a complete streamed blob.
`decrypt_file` needs no extra params.
`full_size_bytes` and `result_checksum` on each chunk describe the whole
file.
The chunks hold it, so `ls` / `browse` / `history` show the real total.

A split chunk shares the current bin when its cost stays under that bin budget.
Chunk N cannot land in an earlier bin than chunk N-1.
After every split chunk is placed, ordinary blobs fill leftover space in each bin.
They pack largest first.
Ties use the path.

If a previously split file later fits one disc, it re-encodes as one blob
with no `volume_chunk` fields.
Split files never use the delta path.

The writer records deletions only on the manifest of the first chunk.

## Staging-filesystem check

Before encoding any blob, `build_staging` estimates source bytes for each
new or changed file that still needs encoding. A file whose scan checksum
names a blob on disc counts as zero. The builder compares the estimate with
free space on the staging filesystem, often a small tmpfs under
`$XDG_RUNTIME_DIR`. If the estimate is at or above
`free * SPACE_SAFETY_MARGIN`, it raises `SystemExit(1)`. This check is
separate from disc/ISO packing.

The `init` and `sync` handlers in `cli.py` prevent this check from firing
in normal cases (see "Automatic staging-space chunking" below). They
partition the changeset ahead of time. Each `build_staging()` call then
receives a group that fits.

The check remains as a safety net for direct callers of `build_staging`
that bypass the outer loop, such as tests, scripts, or future callers.

## SIGINT

The program installs a SIGINT handler at entry. The handler only sets a
flag. Scan, encode, and write loops check the flag and raise
`KeyboardInterrupt`.

The handler then removes every staging directory that the call created.

## Lockfile

Staging paths are deterministic (`oddarchiver_staging_{session:03d}`). If
the directory exists, `_check_staging_lock` reads `.lock`:

- Missing or non-integer contents mean the lock is stale and safe to remove.
- A PID that no longer exists is stale and safe to remove.
- A PID that still exists causes `RuntimeError`. The program leaves the directory.

## Automatic staging-space chunking

`session.partition_by_staging_budget(paths, source, staging_root=None)`
greedily bin-packs source-relative paths. It sorts them for determinism and
orders them by `(source / p).stat().st_size`. The running total in each
group stays under `shutil.disk_usage(staging_root or
_default_staging_root()).free * SPACE_SAFETY_MARGIN`. A changeset that
fits returns `[sorted(paths)]`: one group, indistinguishable from the
ungrouped path this replaces.

If one path exceeds the whole budget, the function raises `RuntimeError`
and names that file. The local staging budget cannot grow between groups.
In contrast, a fresh medium gives a later disc-capacity chunk fresh headroom.
An oversized file cannot proceed without more local staging space.

The `_run_init` and `_run_sync` functions in `cli.py` call this once on
`new_files |
changed`, after scanning the source and comparing it with disc state.
state. Two or more groups print a one-line notice:
(`Source needs ~X; staging has ~Y free. Splitting into N automatic
passes.`) The functions then loop. Each group gets its own
`build_staging()` call and `cli._burn_volume_chunks()` call. The program
appends each group as a sequential session on the current volume, without
user interaction or a new `VolumeMarker`.

Group 0 keeps the parameters that the triggering command (`init` or `sync`)
already used. Every later group behaves like an ordinary sync append,
regardless of which command started the run.

Each group's `build_staging()` call needs a `disc_state` view scoped only to
that group's paths, not the real whole-archive `disc_state`. Otherwise,
each later group would see the files of every earlier group as new or
changed again.

`cli._mask_diff_to_group(current_state, disc_state,
group_paths, group_index)` builds that view. The view treats a path outside
the group as already matching `current_state`, so this pass hides it. It
keeps a path inside the group at its true prior value. It leaves a genuinely
new path absent.

A deleted path (present in `disc_state`, absent from `current_state`) keeps
its true value only in group 0. The view omits it from every later group.
Thus, the deletion appears in exactly one session manifest.

This differs from disc-capacity chunking under "Packing budgets". That
logic splits when the *target disc/ISO* lacks space and creates new volumes
that need a physical swap. Staging-space chunking splits when the *local
staging filesystem* lacks space and creates more sessions on the *same*
volume.

The two methods work together without conflict. The outer staging-space
loop decides how many files enter each `build_staging()` call. Disc-capacity
packing inside that call independently decides whether the group's encoded
blobs need a disc swap.

Worked example: an 8 GB staging budget and a 12 GB source. `init` divides
the changeset into two groups. It burns the first as session 0, as an
ordinary `init` does. It prints the splitting notice, then burns the second
as session 1 on the same volume without a prompt. One command reaches the
same end state as running `init` followed by `sync` by hand.

A non-zero return from any group's burn stops the loop immediately. Earlier
burned groups remain committed and cached. A later plain `sync` stages any
remaining changes through the normal disc-state re-diff.
