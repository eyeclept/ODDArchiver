# Disc usage stats

`oddarchiver status --stats` prints a usage table after the normal status lines.
The command estimates blob sizes from manifests and from control files already in memory.
It does not read blob files.

`oddarchiver status --stats-exact` reads each on-disc file size once through `list_file_sizes()`.

`--stats` and `--stats-exact` cannot be used together.

`--json` prints only a JSON object. The flag requires `--stats` or `--stats-exact`.
Without a stats flag, `--json` exits with status 2.

The JSON object uses `"mode": "estimate"` or `"mode": "exact"`.
Estimate text uses an estimate header and a `~` prefix on bounded blob sizes.

## Categories

The command walks manifests in session order with a live-path set.
It adds each entry path. It then removes each deleted path.

- original: a full blob owned by this session for a path that was not live
- changed_full: a full blob owned by this session for a path that was already live
- changed_delta: a delta blob owned by this session
- dedup: a blob in another session, or a second entry that reuses a blob this session already owns
- deleted: paths listed as deleted in that session
- control: manifests, `enc_mode.json`, `volume.json`, and the volume baseline
- other: files that are not blobs and not control files. Exact mode only
- filesystem_overhead: used bytes minus data, control, and other. A negative value becomes 0. The command measures it for the whole disc only, so session blocks do not show it

Dedup reports file count and plaintext bytes saved (`full_size_bytes`). Disc bytes are 0.

A path that is deleted and later added again counts as original.

SUSPECT manifests do not contribute entries. The output names those sessions.

## Sizes

Exact mode uses `list_file_sizes()` for each blob path and each control file.

Estimate mode uses the length of each control file already read.
It bounds each blob with `session._encoded_size_upper_bound`.
A full blob uses `full_size_bytes`. A delta blob uses `delta_size_bytes`.

A volume-split entry stores the whole-file size in `full_size_bytes`.
Estimate mode sizes each chunk as `full_size_bytes / volume_chunk_count`.
The output marks those sizes as approximate.

Exact mode lists other files such as `TRANS.TBL`, `.lock`, and `oddarchiver-software/`.
Text output shows the largest of those files.
