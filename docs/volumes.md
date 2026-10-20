# Multi-volume archives

A logical archive can span more than one disc. Session numbers cover the
whole archive. Each physical disc is one volume.

## Archive, volume, and session numbers

`oddarchiver.volume.VolumeMarker` is the identity record for one disc. The
file is plaintext `volume.json` at the staging root, next to `session_NNN/`.

Fields:

- `archive_id` - hex Universally Unique Identifier (UUID) shared by every
  volume in this archive
- `volume_index` - 0-based disc number
- `first_session` - first global session number stored on this disc
- `created_at` - Coordinated Universal Time (UTC) timestamp from marker
  creation
- `previous_volume_label` - Universal Disk Format (UDF) label of the disc
  just swapped out, or `""` on volume 0
- `format` - on-disc format number. New discs write `2`. A `volume.json`
  without this field is format 1.

`read_volume_marker` checks the field types. `volume_index` and
`first_session` must be non-negative integers. `format` must be 1 or 2.
A marker that fails a check reads as no marker.

`init` writes a marker on chunk 0 even when the changeset fits on one disc.
The marker has `"format": 2`. New manifests have `"version": 2`.
`archive_id` is `uuid.uuid4().hex`. Later volumes copy this ID.

Session numbers are global and contiguous. Volume 0 may hold sessions 0-41.
Volume 1 then starts at session 42 (`first_session=42`).

`sync` computes the next session as
`marker.first_session + len(manifests)` on the loaded disc. It does not
trust `dvd+rw-mediainfo` session count. BD-R SRM+POW always reports
`session_count=1`.

`read_volume_marker` returns `None` when `volume.json` is absent or
unreadable. That disc is a legacy single-volume archive. Restore does not
read or write restore-state for it.

The manifest scan lists the `session_NNN` directories at the disc root.
`ISOBackend` lists its session tree. `DiscBackend` runs `xorriso -ls /`.
The scan reads only those sessions' control files in one `read_paths()` call.
If a drive listing fails, the scan warns and probes all 1000 session numbers.
The burn path still refuses a failed listing. The scan stops at the first
missing session. If directories exist after that gap, the scan reports
the missing session instead of returning a partial list.

`volume.json` is read in the same control-file batch as the session
manifests for CLI commands.

## On-disc format 2

New discs use format 2. New manifests have `"version": 2`. Readers accept
manifest versions 1 and 2.

A disc's baseline uses format 2 when `volume.json` says `"format": 2`.
It also uses format 2 when the disc's first-session `manifest.enc`
holds a valid version 2 manifest. The baseline does not change format
when `sync` appends new sessions. If the first-session manifest is
missing or SUSPECT, the reader logs a warning and uses `volume.json`
alone to decide the baseline format.

For manifests, the first valid, encrypted version 2 manifest starts
format 2. The program refuses plaintext `manifest.json` in that
session and all later sessions. It accepts earlier plaintext manifests
on an old encrypted disc and prints a warning. A marker with
`"format": 2` forbids plaintext manifests in all sessions. A session's
`enc_mode.json` can also forbid its plaintext manifest when it declares
encryption. See [encryption.md](encryption.md) for the rules on older
encrypted discs.

## Baseline on later discs

Each disc after volume 0 gets a cumulative version 2 baseline at its root.
The baseline lists earlier discs in `volumes`. Each record has
`volume_index`, `label`, `first_session`, `last_session`, and
`encrypted`. The `encrypted` value is true or false based on the archive crypto at disc
close. A missing field means unknown. `restore --all-disks` refuses an
acquired disc that reports mode `none` when this field is true.
The `history` map lists path events in session order. Each `PathVersion`
records its session, volume, kind, checksum, full size, timestamp, and
`blob_session`. A dedup reference can point to a blob in an earlier
session on the same disc. Split files also record `volume_chunk` and
`volume_chunk_count`. Deletions have kind `deleted` and no blob session.

The `state` and `origin` maps keep the live checksums and last-write
sessions from version 1. An unversioned `volume_baseline.json` remains
readable. Its older paths have unknown volume and size in version 2
history. Their origin session serves as a placeholder blob session.

### Older discs

Format 1 baselines do not give a full session range for each earlier disc.
The catalog does not guess which disc holds an unverified version.
Restore reads the next older disc when it cannot locate a session.
Each disc's manifests replace the unverified versions in that disc's range.
Restore then updates the file plans. It keeps an incomplete delta chain
in the encrypted spool until it reads the base disc.
If a needed disc is missing, restore reports the file as stale or missing.
Restore reads each disc once unless a fallback needs a disc it read earlier.
A format 2 baseline contains verified versions and uses the usual disc plan.
New baselines store each version's `verified` value. Readers treat a
missing `verified` field as true in older format 2 baselines.

Archives without encryption store `volume_baseline.json`. Encrypted
archives store `volume_baseline.enc` and do not store the plaintext file.
The program encrypts this file with the same key and method as a manifest.
The reader tries the encrypted file first.

`read_volume_baseline` returns `None` only when the disc has no baseline
file. A baseline file that does not decrypt, does not parse, or has a bad
shape raises `BaselineReadError`. The error names the file and tells the
user to check the key and the disc. `sync` stops before it burns
anything. This prevents it from writing a next-disc baseline without the earlier
history.

On a disc with `volume_index` greater than 0 and format 2, a missing
baseline is an error for `sync` and for `--all-disks`. A format 1 later
disc with no baseline prints a warning and continues.

The reader checks each version 2 record. Session numbers, volume
indexes, sizes, and chunk numbers must be non-negative integers.
Checksums, labels, and timestamps must be strings. `kind` must be
`full`, `delta`, or `deleted`. A blob path in a manifest must start with
`session_NNN/`. `volume.parse_blob_session` reads that session number
and raises `ValueError` for any other path.

New discs hold their own file blobs and delta bases. A changed file gets
a full blob when its earlier version is on another disc. A dedup
reference cannot point to an earlier disc. The packer converts a delta
or reference to a full blob if packing moves it to a new disc. It packs
again after conversion because full blobs can use more space. This rule
can increase disc use, but it lets each new disc serve its file versions
without an older disc.

`build_staging` encodes every new or changed file first. It then packs
blobs, sorted by path, into as many session directories as needed.

- Chunk 0 uses remaining space on the loaded medium times `SPACE_SAFETY_MARGIN` (0.95).
- Later chunks use a full fresh medium minus the encoded baseline bound, then times the same margin.
- The bound starts from the prior baseline size, or 0, plus a per-pending-path allowance.
- `ISOBackend.disc_size` (`--disc-size`) is that fresh budget when it is a positive int.
- Otherwise the budget is `disc.BDXL_DISC_BYTES` (93 GiB).
- [media.md](media.md) lists named presets and write engines.

A file larger than one disc's blob budget uses separate volume chunks.
Each chunk has its own blob. The first session of a split records deletions.

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
If the program cannot read the baseline of the full disc, it stops before the swap.
Earlier chunks stay burned. Run sync to resume.

The newly inserted medium must report `session_count == 0`. A non-blank
disc raises `RuntimeError`. Volume k+1 cannot overwrite volume k.

Before `backend.init()` on the new disc, the next chunk gets a
`VolumeMarker`. `volume_index` increments. `first_session` equals the
session number of that chunk. `previous_volume_label` equals the label
just swapped out.

`--test-iso` / `ISOBackend` skips the prompt. Overflow opens
`<original>.vol{k}.iso` with the same `disc_size`. The original path is
the stem: `archive.iso` volume 2 is `archive.vol2.iso`, never
`archive.vol1.vol2.iso`.

`--mirror` plus a 2+ chunk split raises `RuntimeError`. Mirror burns only
run on a single-volume pass.

## Multi-volume restore

Use `--all-disks` to restore the newest version of each selected file.
Start with the newest disc that you have. The catalog on that disc identifies the versions and the discs that hold their blobs.

```
oddarchiver restore /tmp/out --all-disks --test-iso archive.vol2.iso
oddarchiver restore /tmp/out --all-disks --device /dev/sr0
```

For an ISO, restore opens the needed sibling files automatically. For a drive, insert any disc from the prompt list.
The program reads each needed disc once. It does not request discs that hold only older, superseded versions.
Use `--path P` more than once to select files or directories. Use `--all-files` to select the whole live tree.
Without either flag, restore also selects the whole live tree. Do not combine `--path` and `--all-files`.
Use `--session N` to select the archive state at session N.

Without `--all-disks`, restore uses only the inserted disc. It never asks for another disc.
It restores each file's newest version on that disc. If an old delta needs another disc, restore names that disc for the file.
A marked disc prints a notice because another disc can hold a newer version. A legacy unmarked disc keeps its original behavior.
Restore skips a destination file whose restore-state record comes from a newer session only when the destination checksum matches that record.

`restore --all-disks` honors `--read-mode`. Use `--read-mode sequential` to
extract one session at a time. Use `--read-mode full-copy` to extract every
session up front. Without the flag, restore picks the mode from the local
free space. Single-disc `restore` and `verify` use `sequential` when you do not
give the flag. The declared manifest size is not the only guard. Restore
counts real extracted bytes and stops before the bytes pass the budget.
A size that is too small in the manifest cannot fill the disk.

Restore hashes each destination file at most once per decision. It reads
the hash input in 1 MiB chunks. It reuses the digest from the resume check.

Restore extracts blobs straight into the local work directory. It does not
hold every blob of a batch in a dict of bytes. `read_paths` still returns
bytes for small control files.

Restore writes streamed files to a private file in the destination directory.
It checks the file before it replaces an existing destination file.
Every normal restore writes a private 0600 temp file in the destination
directory first. It replaces the destination only after the write and the
size check. A failed write leaves the old destination file unchanged.
For a file split across volumes, restore keeps a private `<name>.oddarchiver-partial` file until it checks the complete file.
An interrupted split restore does not replace the existing destination file.

Restore-state is outside the destination at `$XDG_STATE_HOME/oddarchiver/restore-<digest>.state.json`.
Without `XDG_STATE_HOME`, the state directory is `~/.local/state/oddarchiver/`.
The digest is `sha256("{archive_id}:{resolved dest}")[:16]`. The state records the selection, completed files, read discs, and missing discs.
The restore-state file uses mode 0600. The state directory and the spool directory use mode 0700.
Restore does not use a spool path that is a symlink, that is not a directory, or that this user does not own.
Type `q` or send EOF at a disc prompt to save progress. The process then exits with code 3.
Run the same restore command to continue. The next run requests only discs that still hold needed blobs.
A completed file in restore-state is restored again if the destination file is missing or has the wrong checksum.
Restore refuses a disc file whose checksum, size, or kind does not match the catalog. It does not write that file.
Restore measures blob size on the extracted files. It stops if the real size exceeds the local space margin.
Restore reports a suspect manifest in the selected session range as a failure.
The error names the session, even when restore can recover other files.

An old archive can have a delta whose base is on another disc. Restore stores only encrypted blobs in a local spool under the state directory.
It reconstructs the file after it gets every blob in the chain. It removes the spool after restore completes.

### Trust rules for acquired discs

Restore learns path events only from verified manifests. These rules apply
to each disc that restore reads, and also to the starting disc.

- The disc run starts at `first_session` in `volume.json`. It ends at the
  last loaded session slot. Restore learns events only from a non-SUSPECT
  manifest whose session is in this run. It ignores a manifest outside the
  run and a second manifest for the same session. It ignores an event that
  conflicts with a verified event at the same session. It also ignores an
  entry with an invalid blob path.
- Restore authenticates an acquired disc before it learns the disc. With a
  key, the disc must have one or more non-SUSPECT manifests whose session
  equals the directory slot. When the catalog or the local disc record says
  that the archive is encrypted, restore requires a key. Restore refuses a
  disc that fails a check.
- If restore cannot read `volume.json` or load the manifests of an acquired
  disc, it refuses only that disc. A plaintext `manifest.json` under a key
  is one such error. Restore prints `Refuse this disc (disc N): <reason>`.
  When restore cannot read the disc number, the message says `unknown disc`.
  For an ISO sibling, restore marks the disc missing and makes a new plan.
  For a drive, restore asks for a disc again. A load error on the starting
  disc stops the restore with exit code 1.
- Restore writes a file only from a chain with a verified full base. Each
  version in the chain must come from a learned manifest. A version 1
  baseline entry is a hint. A hint can send restore to a disc, but it
  cannot be a restore target. Restore asks for older discs until the chain
  has a verified full base, or until you mark the needed disc missing.
  Restore then reports the file as `stale` or `missing`.
- A hint has the kind `full`, but the real version can be a delta. When a
  chain starts at a hint, the chain starts at the nearest older verified
  full instead. Restore can then use the blobs of each disc on the first
  insert, in any disc order.
- Restore checks the destination file before it skips a file. A
  restore-state record from a newer session skips the file only when the
  destination checksum matches that record. If not, restore removes the
  record and restores the file again.

## Recovery mode

At the first prompt, enter missing disc numbers. Press Enter if you have every disc.
EOF at that prompt pauses the restore and saves state. It does not mean that every disc is present.
Enter disc numbers with at most six digits. Invalid input prints an error and asks again.
At an insert prompt, type `m N` to mark disc N missing. Use repeatable `--skip-volume N` to mark discs before restore starts.
The mark stays in restore-state. Use `--unskip-volume N` to clear it on a later run.
An ISO sibling that does not exist counts as a missing disc.
An ISO sibling that is from another archive, has the wrong index, or has
no readable marker also counts as missing after one rejection. The loop
stops. A drive keeps the re-prompt. Restore handles an acquired disc that
it refuses in the same way. See "Trust rules for acquired discs" above.
A SUSPECT manifest on a collected disc counts as a failure. The message names the session and the disc.
When the disc has sessions and every manifest is SUSPECT, restore exits 1.
The message tells you to check the key. The disc may be damaged.

When a disc is missing, restore looks for the newest complete version of each file on available discs.
It reports the file as `stale` at that version's session. It reports `missing` when no complete version remains.
These results do not count as failures. A corrupt blob on an available disc does count as a failure.
Restore can still report an older version as `stale` when a newer blob is corrupt.
If a fallback needs a disc that restore read before, insert it again.
This fallback is the only case when restore asks for the same disc twice.
On a single-disc restore, `--skip-volume` keeps its existing recovery-mode behavior for that disc.

## Worked example: three discs

Disc 0 holds a full version of `notes.txt` at session 0 and a full version of `keep.txt`.
Disc 1 holds a newer full version of `notes.txt` at session 1 and the only version of `lost.txt`.
Disc 2 holds a full version of `new.txt` at session 2. Its catalog includes the history of discs 0 and 1.

Start with disc 2 and request only `new.txt`:

```
oddarchiver restore /tmp/new --all-disks --test-iso archive.vol2.iso --path new.txt
```

Restore uses disc 2 and does not read discs 0 or 1. To restore all files with disc 1 lost, run:

```
oddarchiver restore /tmp/out --all-disks --test-iso archive.vol2.iso --skip-volume 1
```

Restore reads discs 2 and 0. It restores `notes.txt` from session 0 and reports it as stale.
It restores `keep.txt` and `new.txt`. It reports `lost.txt` as missing.
