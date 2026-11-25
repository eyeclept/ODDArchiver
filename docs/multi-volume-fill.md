# Fill free space on earlier volumes

A logical archive can span more than one disc.
Without `--fill`, `sync` appends only to the inserted disc.
Overflow opens a new volume.
Leftover space on an earlier disc stays unused.

`--fill` appends a new session to an earlier volume that still has free
space.
Fill is off by default in every run, including a TTY.
Only `--fill` enables it.
`--no-fill` stays accepted.
Where this document says `--fill`, read "a run with fill enabled".

`ISOBackend` rebuilds the image from every `session_NNN` directory.
It does not call `growisofs -M`.
Drive burns still do.

## Fill rules

These rules are in force.

### Fill flag

Fill is off unless the user passes `--fill`.
An interactive terminal does not turn fill on.
The program still accepts `--no-fill`.
That flag has the same effect as omitting `--fill`.

### Recorded free space

Each session catalog snapshot records the last measured free space of
every known volume. The fields are `remaining_bytes`, `used_bytes`,
`appendable`, and `measured_at`.

Fill merges those on-disc records with local measurements.
The newest `measured_at` value wins.
Fill then asks only for discs whose free space can hold at least one planned blob.
It skips the rest without a prompt.
It prints that skip.

### Catalog snapshot on a fill session

A fill session on an older volume writes a catalog snapshot.
The snapshot reuses `volume_baseline` plus a `layout` object.
It holds the entries of every session on other volumes.
That disc alone can supply the full change-set baseline for the next `sync`.
The local cache is a blob index.
It does not hold manifests.

### Two-phase fill stubs

A fill starts on the newest catalog disc. The program plans the files
there. If that disc has room for the planned files, it burns them there.
It does not ask for other volumes for those files.

If the plan needs an older volume, the program writes an intent stub on
the newest disc first.
Then it asks for the target volume and burns the fill session.
Then it asks for the newest disc again and writes an outcome stub.

The intent prompt on a drive is:

```
Fill: write an intent stub on the newest disc first.
A stub session uses about 15.9 MiB (8144 blocks).
Press Enter to write it.
Type yes to skip it. Skip reopens the quit-after-burn gap.
```

After that it offers an outcome stub on every other
appendable volume that has room, oldest first.
Type `s` to skip one disc.
Type `q` to stop.
`--test-iso` writes stubs with no prompt.

A stub is not a numbered archive session.
Intent and outcome live in
`volume.json` as `fill_intent` / `fill_intent_enc` and `fill_outcome` /
`fill_outcome_enc`.
The catalog snapshot still uses `catalog_snapshot`
or `catalog_snapshot_enc` in the same file.

The intent records a fill attempt.
It stores the target volume index and label, the global session number, the planned files
(path and checksum), and the time.

The outcome records what happened.
Status is `landed`, `failed`, or `not_confirmed`.
A landed outcome stores the session, the volume, the file list, and the layout after the fill.

On a real DVD-R, one stub session moved the next writable address by
8144 blocks (about 15.9 MiB: about 436 blocks of data plus session
overhead).
Two-phase fill adds one extra stub on the newest disc.

If the newest disc has no room, or the 4 GiB rule closes it for `growisofs -M`,
the program skips the intent and outcome stubs on it.
It prints that skip.
The fill session layout lists that volume in `not_stubbed`.

The intent stub is skippable only by explicit confirmation.
Skip reopens the gap if you quit after the target burn.

If a later `sync` or `restore` reads an intent with no matching outcome,
it does not trust or ignore the fill.
It reports a fill attempt to volume X session N.
It asks for volume X.
It checks whether session N is on that disc.

Then it writes the outcome stub before it plans new work.
If volume X is not available, it refuses and tells you to insert that disc.

When a fill prompt opens the tray and the answer is `s` or `q`, the
program tells you to close the tray.
It waits for Enter before it uses the drive again.

The fill session also carries that layout and the not-stubbed list as
known at its burn time.

### Sync start

`sync` accepts any disc whose catalog snapshot or layout stub makes the
baseline complete.
Complete means the snapshot covers every session up to the highest known session.
The program does not read a sibling ISO to hide a gap.
It does not plan against an incomplete baseline.

When the inserted disc is stale, the program names the disc to insert.
Stale means local memory or a stub names a newer layout than this disc holds.

Empty-current-disc skip, the 4 GiB append rule, tail resume, cache
invalidation on swap, and new-volume behavior stay as they are.


## Current numbering

Session numbers are global.

Volume 0 can hold sessions 0-41. Volume 1 then starts at session 42.
`VolumeMarker.first_session` is the first global number on that disc.

`catalog.next_archive_session` sets the next session to one more than the
highest known session for the whole archive.
`cli._run_sync` and `_dry_run_disc_state` call that function.
Known sessions come from the local archive record and the inserted disc.

Old code used `marker.first_session + len(manifests)` on the loaded disc.
That old formula matches the archive-wide next number
only while each volume holds one contiguous prefix of the global
sequence.
It also matches only while the user inserts the newest volume.

`session.build_staging` packs leftover blobs into later bins. Each later
bin gets `session_n + i` and `is_new_volume=True`.
`cli._burn_volume_chunks` inits those bins on blank media.

`catalog.volume_for_session` maps a session with one `[first, last]`
range per disc. `learn_disc` treats the disc run as
`first_session` through `first_session + len(manifests) - 1`.

## Session numbering

Fill writes a later global session onto an earlier disc. Volume 0 can
then hold sessions 0-41 and 51. Volume 1 still holds 42-50.

Three options.

### Per-volume directory numbers plus a global sequence

Each disc uses `session_000`, `session_001`, and so on as local slots.
The manifest still stores a global `session` field for archive order.

Tradeoffs:

- Local directories stay contiguous. `load_session_manifests` can keep
  its stop-at-first-gap scan for the directory slot.

- Blob paths use the local slot. `volume.parse_blob_session` then returns
  a number that is not unique across discs.

- Cache keys, AEAD strings, and `blob_id` already hash the session
  integer. They would need the global field, not the path.

- Two discs both show `session_003/` in logs and `xorriso -ls`. Operators
  cannot tell them apart without the volume index.

- Restore `--session N` still needs the global field. The code then keeps
  two integers for every session.

Recommendation: reject this option. Dual numbering will drift.

### Explicit `(volume_index, session)` key

Every path event, cache entry, and restore flag uses a pair. Session
numbers can restart per disc, or stay local forever.

Tradeoffs:

- The pair names the disc without a catalog range lookup.

- `restore --session N`, `based_on_session`, history, and verify become
  pair-valued. That is a breaking CLI change.

- Encrypted manifests bind AEAD to `manifest_aad(session)` today. A pair
  would need a new AEAD string and a format bump.

- Existing discs store a single integer. Readers would invent volume 0
  for old manifests.

Recommendation: reject this option. Session is already unique archive
wide. Fill does not need a new primary key.

### Global sequence, gaps allowed on a disc (recommended)

Keep one monotonically increasing global session number. Keep on-disc
directories named `session_NNN` with that global number. Allow a disc to
hold a set of numbers, not one closed interval.

Next session is `max(known_sessions) + 1` for the whole archive. It is
not `first_session + len(manifests)` on the inserted disc.

Identity of a session stays the integer. Membership is
`(archive_id, volume_index, session)`. Catalog storage becomes a list of
disjoint ranges per disc, or an explicit session list.

Tradeoffs:

- Restore `--session N`, history, cache, and AEAD stay as they are.

- Existing discs are already in this form. A one-range disc is a list of
  length one.

- `load_session_manifests` must list real `session_NNN` directories. It
  must not stop at a gap and then raise because a later directory exists.

- A torn write that skips a slot on the same disc still has to fail.
  The loader uses catalog membership plus the directory list, not "gap
  means error" alone.

- `session_NNN` remains three digits. The archive still stops at session
  999. Fill does not raise that cap.

Recommendation: use this option.

### Modules that assume contiguous global numbers that rise with volume

These `oddarchiver/*.py` files encode the old contiguous-number assumption.
A fill implementation has to change the listed behavior, or it will mis-number
or drop sessions.

`volume.py` uses `VolumeMarker.first_session`. `VolumeRecord` stores one
`first_session`/`last_session` pair. `parse_blob_session` reads the
directory name as the session.

`manifest.py` has `load_session_manifests` probe from `start_session` and
stop at the first hole. A later directory on the same disc raises.
`_SESSION_PROBE_LIMIT` is 1000. `Manifest.session` and
`based_on_session` are global integers.

`session.py` later bins use `session_n + i` and `is_new_volume = i > 0`.
`based_on_session` is `session_n - 1`.
Staging paths are `oddarchiver_staging_{session:03d}`.
Same-disc dedup uses `volume_first_session`.
First-bin budget comes from the loaded medium only.

`cli.py` next session is `catalog.next_archive_session` in `_run_sync`
and `_dry_run_disc_state`.
That value is `max(known sessions) + 1`.
Old code used `first_session + len(manifests)` on the loaded disc.
`_burn_volume_chunks` inits every later chunk on a blank disc.

`_burn_verify_commit` records
`(session_n - marker_first_session) + 1` in the ledger.
`expected_session_count` is that next global number.
`_crypto_for_disc` reads `session_{first:03d}/enc_mode.json`.

`disc.py` `archive_session_count` returns `first_session + len(listed)`.
`_guard` compares that value to `expected_session_count`.
`list_session_numbers` already returns the real directory set.

`ledger.py` `confirmed_sessions` is a count.
A shortfall uses `range(readable, expected)` as if sessions started at 0.
The file key is label plus device path.
Every volume of one archive shares both.

`catalog.py` `_index_catalog` and `volume_for_session` use one interval
per disc.
`_usable_disc_manifests` keeps
`first_session <= session < first_session + len(manifests)`.

Take a disc with `first_session` 0 and 43 listed names.
Session 51 is outside `[0, 43)`.
The loader drops it.
`remember_inserted_disc` requires `m.session == slot` while it enumerates
from `first_session`.

`restore.py` loads from `marker.first_session`. Single-disc restore
enumerates manifests from that value. `--session N` is a global cutoff.

`restore_plan.py` `_disc_excludes_session` uses one interval.
`_unknown_candidate` walks `volume_index` downward from the source disc.
`_disc_authenticated` enumerates from `first_session`.

`restore_state.py` per-disc `VolumeRecord` stores one first/last pair.
`FileReplay.last_session` is a global session.

`verify.py` `_read_all_manifests` keys by `enumerate(loaded)`. That key
matches `manifest.session` only when `first_session` is 0. Incomplete
detection probes `last_clean + 1`. Coverage uses `ledger.py`.

`cache.py` blob names hash `f"{session}:{path}"`.

`crypto.py` `manifest_aad(session)` and blob AAD use the session
integer. `baseline_aad` uses `archive_id` and `volume_index`.

`diskread.py` `open_session(n)` is the global session.

`stats.py` walks the manifests on this disc and `parse_blob_session`.

`software_bundle.py` treats `session_n > 0` as append, not init.

`compress.py`, `config.py`, `delta.py`, `drive.py`, `filehash.py`,
`log.py`, `usb_reauth.py`, and `__main__.py` do not own the numbering
invariant.

## Target volume choice

Staging today reads `backend.mediainfo()` on the loaded medium. The first
bin uses `remaining_bytes * SPACE_SAFETY_MARGIN` minus merge overhead.
Later bins use a fresh medium. Fill needs a remaining-byte figure for
every candidate volume.

Host memory cannot be the only source. Another machine, or a burn that
this host did not record, makes a stored remaining count stale. The
program has to measure the disc in the drive.

### Prompt

Fill runs only with `--fill`.
An unattended run never prompts for older discs.
The user starts with a disc that can supply the live tree and the
next global session.
That disc holds a complete catalog snapshot or layout stub, or it is the newest catalog disc.

After the source diff, the program prints each known volume whose
recorded free space can hold at least one planned blob.
It skips the rest without a prompt.
For a drive it opens the tray and waits.

```
Fill: insert volume 0 (ARCHIVE, last measured 1200 MB free) and press Enter.
Type s to skip this disc for this sync.
Type q to abort. Data already burned stays on disc.
A stub session uses about 15.9 MiB (8144 blocks).
```

After Enter, the program reads `volume.json` again.
A mismatched `archive_id` or `volume_index` is a wrong disc.
It logs a warning, prints the found identity, and repeats the prompt.
It does not burn.

`--test-iso` does not prompt.
It opens each sibling `archive.volN.iso` that exists, measures it, and skips a missing sibling.


### Recorded per-volume data

Store this in the local archive record
(`$XDG_STATE_HOME/oddarchiver/archive-<id>.json`) after a successful
measure or burn:

- `volume_index`
- `label`
- `session_ranges` (list of `[first, last]` inclusive pairs)
- `used_bytes`

- `remaining_bytes`
- `appendable` (false when `-M` is not allowed)
- `measured_at` (UTC)
- `file_count` (for merge-overhead estimates)

Also write `session_ranges` into the merged `volume.json` on that disc
when the fill session burns. A later host can then learn membership from
the disc itself.

Do not add extra CLI flags for this record. The fill command writes it.

### Unavailable disc

`s` at the prompt skips that volume for this run. Packing does not use
it. The run continues with measured volumes and, if needed, a new blank
disc.

A missing `--test-iso` sibling is the same skip. The program logs a
warning and continues.

If no measured volume can hold the changeset and the user does not
insert a blank disc, the command exits 1. It does not invent remaining
bytes.

A skipped volume is not marked missing in restore-state. Skip applies to
this `sync` only.

### Which volume gets the blobs

Use oldest appendable volume first. Among volumes that can still take
`-M`, pack largest blobs first into the oldest disc that has room.

Tradeoffs versus "most free space first":

- Oldest-first uses leftover space on early discs, which is the point of
  fill.
- Most-free-first can keep dumping onto a large newest BD and never touch
  a DVD with 800 MB free.

Recommendation: oldest appendable first. Blobs that fit none of the
measured volumes go to a new blank disc. That matches current overflow.

Same-disc rules stay. A dedup reference or a delta must not move to a
disc that lacks the origin blob. `_repack_moved_blobs` already converts
those to full blobs when a bin is a new volume. Fill bins that land on a
different existing volume use the same conversion.

The planner must not use only the manifests of the inserted disc as
`disc_state`.
The baseline on an older disc does not list files added on later volumes.
Staging would pack those files again.
`--fill` therefore reads the newest catalog (or complete local history) before it measures
fill targets.

## growisofs -M past about 4 GiB used

`DiscBackend.append` runs `growisofs -M`.
`genisoimage` then merges the previous session as a file.
ISO9660 file size is a 32-bit byte count.
A merge source of about 4 GiB (`2**32` bytes) is the documented failure
point.
Typical writer text is `Value too large for defined data type`.

This is not the archive blob cap.
`BLOB_SIZE_CAP` is 3 GiB so a stored file stays under the ISO9660 file-size wrap.
A disc can already hold many blobs whose sum exceeds 4 GiB.

`ISOBackend.append` copies staging into `sessions_root` and rebuilds.
It does not hit this merge limit.
Tests that use `--test-iso` cannot prove the drive path.

`docs/media.md` records a BD-R append on the USB drive of this project.
Those runs do not record used bytes at the moment `-M` failed, if it
failed at all.
DVD burns in this repo are ISO simulation only.
The 4 GiB used cap is a detection rule to confirm on DVD-DL hardware.
It is not a measured constant from this tree.

### Detection

1. Before packing a drive volume, read `mediainfo().used_bytes`.

2. If `used_bytes >= 4 * 2**30`, set `appendable=False` for that volume.
   Do not call `-M`. Log a warning with the byte count.

3. If `-M` still fails with a too-large or merge-source error, set
   `appendable=False`, log the writer lines, and do not retry `-M` on
   that disc.

4. Do not treat USB stale-cache `pread64` failures as this limit.
   `DiscBackend._retry_stale_append` already handles that path.

If a later hardware run shows BD-R `-M` succeeding past 4 GiB used, keep
the byte cap for DVD profiles only. Drive profile comes from
`dvd+rw-mediainfo`. Until that run exists, apply the cap to every
`DiscBackend` fill target.

### Fallback

The program treats a non-appendable volume as closed for fill and for
ordinary `sync` append. Leftover MMC space stays unused.

Fallback is the existing new-volume path: prompt for a blank disc, write
a new `VolumeMarker`, and `growisofs -Z`.

Never fall back to `growisofs -Z` on a non-blank disc. That would
overwrite the volume. Never substitute `xorriso` for `-M` on
growisofs-formatted BD-R SRM+POW. `STATE_OF_THE_PROGRAM.md` records that
`xorriso` refuses that media.

## Catalog and restore

Today the newest catalog lives on the newest volume.
Closing disc k+1 writes a cumulative `volume_baseline` that lists discs 0..k.
Fill can write session 51 onto volume 0 after volume 2 exists.
The baseline on volume 2 then omits session 51.

New rule: the disc that holds the highest global session holds the newest
catalog snapshot the program wrote.
That disc may not have the highest `volume_index`.

### How restore finds sessions on older volumes

`--all-disks` still starts with the inserted disc.
Build the catalog from its baseline and manifests.
Merge the local archive record.

If local memory lists a session higher than `catalog.last_session`, print
that fact and add those discs to the needed set.

```
Catalog from ARCHIVE (disc 2), sessions through 50.
Local records show disc 0 holds session 51. Insert disc 0 to include it.
```

`learn_disc` must load every listed `session_NNN` on the inserted disc,
not `first_session + len(manifests)`.
It records `session_ranges` instead of one last-session integer.
`volume_for_session` searches those ranges.

Without local memory (another machine), start-from-newest-volume still
runs.
The baseline on that volume is complete for the original
monotonic layout.
For a filled archive the user must insert the filled disc once.
`learn_disc` then sees session 51 and updates the catalog.

To keep a cold restore from needing guesswork, each fill burn also writes
an updated catalog snapshot at the staging root (`volume.json` ranges
plus a baseline that lists every known range from host memory).
`growisofs -M` merges those root files.
Any disc that received a fill then names the extra sessions.

A pointer session on the highest `volume_index` would keep "newest disc
means newest catalog".
That extra burn needs the newest disc to still accept `-M`.
After 4 GiB used, that disc is often closed.
Do not require that pointer.
Use local memory plus the snapshot on the filled disc.

### Prompt order

Keep the current `--all-disks` prompt: list needed discs, Enter after
insert, `m N` to mark missing, `q` to save and exit 3.

Sort the needed list by `volume_index` so a user who still starts from
the newest disc sees older fill targets next. When an unresolved session
has no range yet, keep the current prompt text:
`Insert the disc that holds session N`.

ISO siblings stay `archive.vol{k}.iso`. Fill does not add a new name.

### Start-from-newest-disc

An archive that never used `--fill` keeps the current behavior. Highest
`volume_index` still has the highest session and the full baseline.

A filled archive can start on the highest `volume_index` as well.
Restore prints the local-memory notice when later sessions live elsewhere.
It does not refuse the starting disc.
It does not assume the starting baseline is complete until memory and learned discs agree.

Single-disc `restore` without `--all-disks` still uses only the inserted
disc.
A fill session on that disc restores with the rest of that disc.
It does not ask for other volumes.

## Failure modes

### Wrong disc

After every fill insert, `check_volume_identity` runs with the expected
`archive_id` and `volume_index`. A blank disc at a fill prompt is not a
fill target. Offer it only when packing still needs a new volume.

Log the mismatch. Do not burn.

### Missing volume

Skip that volume for this `sync`. Pack onto measured volumes or a new
disc. Do not write a restore missing mark.

If the newest catalog disc is absent and local memory is not complete,
refuse `--fill`. Print that the program needs the newest catalog disc or
a complete local record. Ordinary `sync` on the inserted disc still
works.

### Interrupted fill

A mid-`-M` interrupt matches the current append interrupt.
`BurnInterrupted` runs verify. Cache and ledger do not update unless
verify passes. Resume by inserting the same volume and running
`sync --fill` again.

Do not advance the host next-session value until post-burn verify
passes.
The next run can reuse that session number.
Staging lockfiles stay per session number (`oddarchiver_staging_NNN`).
An in-use lock still raises.

If the interrupt left a directory without a readable manifest,
`detect_incomplete_session` must look at `list_session_numbers()`, not
`last_clean + 1`.
On a gapped disc, `last_clean + 1` can be a session
that lives on another volume.

### Racing syncs

Two processes can load the same next global session from memory.
Staging locks only serialize one session number.
They do not serialize allocation.

Take an exclusive lock file
`ledger_dir() / f"archive-{id}.sync.lock"` for the whole `--fill` run
(open, `LOCK_EX`, write PID).
A second `sync` on the same `archive_id`
raises `RuntimeError` and names the lock path.
Ordinary `sync` without `--fill` on a different archive still runs.

Do not lock on device path alone. Two volumes of one archive can sit in
two drives. The race is the global session counter, not the drive.

## Migration for existing archives

Do not rewrite old discs before the first fill.

A disc with one contiguous range stays valid. Readers treat a missing
`session_ranges` field as `[[first_session, last_session]]` when both
bounds exist. `last_session` on an old baseline is the last session at
disc close.

The first `--fill` burn onto a volume merges a `volume.json` that adds
`session_ranges`.
Format stays 2.
Do not bump the on-disc format for this field.

A reader that does not know the field still uses `first_session`
and will miss gapped sessions.
That is the upgrade line: install the fill build before you fill.

Local archive records gain `session_ranges`, `used_bytes`,
`remaining_bytes`, and `appendable`.
A record without them is complete for restore of unfilled archives.
`--fill` measures discs instead of trusting missing remaining bytes.

Ledger files stay as they are until the ledger phase. Fill must not rely
on `confirmed_sessions` across volumes. The ledger phase keys records by
`archive_id` and `volume_index`. It never lowers a stored count.

Legacy unmarked discs (no `volume.json`) are not fill targets.

## Phased plan

Smallest safe change first. Each phase keeps default `sync` on the
inserted disc.

### Phase 1: session sets on one disc (no fill yet)

Allow a disc listing to be a set. Keep writing one contiguous range.

Files: `oddarchiver/manifest.py`, `oddarchiver/volume.py`,
`oddarchiver/catalog.py`, `oddarchiver/disc.py` (`archive_session_count`
and `_guard`), `oddarchiver/verify.py`, `tests/test_manifest.py`,
`tests/test_volume_identity.py`, `docs/volumes.md`.

Acceptance: a test ISO with directories `session_000` and `session_002`
raises unless catalog membership says 1 lives elsewhere. A normal
contiguous disc still loads. `volume_for_session` still returns one disc
per session.

### Phase 2: local volume space record

Write remaining bytes and appendable after each successful burn.

Files: `oddarchiver/catalog.py`, `oddarchiver/cli.py`
(`_burn_verify_commit`), tests under `tests/test_volume_staging.py`.

Acceptance: after an ISO sync, `archive-<id>.json` lists `used_bytes`
and `remaining_bytes` for that volume.

### Phase 3: archive lock and next-session from catalog

Next session is `max(known_sessions) + 1`. Lock `archive-{id}.sync.lock`
around `sync`.

Files: `oddarchiver/cli.py`, `oddarchiver/catalog.py`,
`oddarchiver/session.py` (lock helper next to staging lock),
`tests/test_session.py`.

Acceptance: inserting volume 0 of a two-volume ISO does not propose
session 42 as session 0 plus length.
`--fill` is still absent.

Default `sync` on volume 0 still appends there only when the user inserted it.
It still uses the archive-wide next number.
Document that default `sync` on an older disc is wrong without `--fill`.
In this phase, refuse default `sync` when the inserted disc is not the highest known volume.

### Phase 4: growisofs 4 GiB detection

Files: `oddarchiver/disc.py`, `oddarchiver/session.py` (budget treats
non-appendable remaining as 0), `tests/test_disc.py`.

Acceptance: a fake `used_bytes` of `4 * 2**30` refuses `-M`. A captured
too-large writer error marks the volume non-appendable. Stale `pread64`
still uses the USB retry path.

### Phase 5: `sync --fill` prompts and one-target packing

Measure volumes. Pack onto the oldest appendable disc that has room.
Burn with `append`, not `init`. Restage `volume.json` ranges at the
staging root.

Files: `oddarchiver/cli.py`, `oddarchiver/session.py`
(`is_new_volume` means "needs init on blank media", false for fill),
`oddarchiver/volume.py`, `tests/test_volume_staging.py`,
`tests/test_volume_swap.py`, `docs/session.md`, this file.

Acceptance: two ISO volumes, volume 0 with free space, volume 1 newest.
`sync --fill` writes the new session directory onto volume 0. Volume 1
stays as it was. Default `sync` still appends to the inserted newest ISO.

### Phase 6: restore and catalog for gapped volumes

Files: `oddarchiver/catalog.py`, `oddarchiver/restore.py`,
`oddarchiver/restore_plan.py`, `oddarchiver/restore_state.py`,
`oddarchiver/cli.py` (`_print_catalog_notices`),
`tests/test_volume_restore.py`, `docs/volumes.md`.

Acceptance: start `--all-disks` on volume 1.
Local memory names session 51 on volume 0.
Restore asks for volume 0 and restores that version.
Start on volume 1 with no local memory and no fill snapshot: files from
session 51 are absent until the user inserts volume 0.
After learn, a second run finds them.

### Phase 7: ledger per volume

Files: `oddarchiver/ledger.py`, `oddarchiver/cli.py`,
`oddarchiver/verify.py`, `docs/volumes.md`.

Acceptance: verify volume 1 in the same drive after a fill of volume 0
does not raise `cannot treat as absent`. The key includes `archive_id`
and `volume_index`.

### Phase 8: split one changeset across several existing volumes

Only after phase 5. Later bins may target another measured old volume,
not only a blank disc.

Files: `oddarchiver/session.py`, `oddarchiver/cli.py`
(`_burn_volume_chunks`), `tests/test_volume_split.py`.

## Real-DVD test plan

`docs/media.md` records no hardware DVD burn in this tree. Confirm fill
on DVD before relying on the 4 GiB rule. Use discardable media.

### DVD-SL (nominal 4.7 GB)

1. `init` a small tree. Note `status --stats` used bytes.

2. `sync` until used bytes pass 3 GiB and remaining is under 1 GiB.

3. `sync --fill` with this as the only volume. Expect either an append
   or a 4 GiB close plus a prompt for a new disc.

4. If `-M` fails, save the full writer log. Check for
   `Value too large for defined data type` versus disc-full versus stale
   `pread64`.

5. `verify --slow`. `restore --all-disks` to an empty directory. Diff
   against the source.

### DVD-DL (nominal 8.5 GB)

This is the disc that can have several GiB free after 4 GiB used.

1. `init` and `sync` until used bytes are just under `4 * 2**30`.

2. `sync` one small file. Expect `-M` to succeed.

3. `sync` until used bytes are just over `4 * 2**30` while remaining
   bytes are still several GiB.

4. `sync` one small file without `--fill`. Expect detection to refuse
   `-M` and to ask for a new volume.

5. Insert a second DL disc. Finish that session as volume 1.

6. Add a 500 MB file to the source. Run `sync --fill`. Insert volume 0
   when asked. Expect skip (non-appendable), then a burn to volume 1 or
   a third disc.

7. If step 4 still appended, the 4 GiB cap is wrong for this drive.
   Record used bytes and the genisoimage version. Narrow the cap to the
   profile that actually failed.

### Two-volume fill (DVD-SL plus leftover)

1. `init` on disc A with a tree that leaves at least 400 MB free.

2. Force a second volume with a file larger than remaining space on A.
   That is disc B.

3. Add a 200 MB file. Run `sync --fill`. Start with disc B so the
   catalog is newest. Insert disc A when asked.

4. Confirm `xorriso -ls /` on A shows a new `session_NNN` whose number
   is higher than every session on B.

5. `restore --all-disks` starting with B. Confirm the prompt names A.
   Restore the 200 MB file. SHA-256 must match the source.

6. Repeat the restore on a host with the local archive record removed.
   Insert B first, then A when asked or after a miss. The 200 MB file
   must still restore after the program learns A.

### Interrupt and wrong disc

1. Start `--fill` onto A. Send SIGINT during `growisofs -M`.

2. Confirm a non-zero exit. Confirm the cache did not update.
   Confirm `verify` names an incomplete trailing slot only when
   that directory exists on A.

3. Insert B when the prompt asked for A. Confirm refuse and re-prompt.

4. Insert A and rerun. Confirm the session completes or reuses the same
   number.

### What not to treat as a DVD result

`--test-iso --disc-size dvd-dl` covers packing math and prompts. It does
not cover `-M`. A BD-R success past 4 GiB used does not clear DVD-DL.
Record the `growisofs` and `genisoimage` versions next to each hardware
log.
