# Listing, selective restore, and browse

Without a path filter, `restore` reconstructs the whole live tree. Use
`ls` to see the archive contents. Use `restore --path` to restore a
subset, or use `browse` to select a subset interactively.

All three commands share the same connection flags as `history`:
`--device`, `--test-iso`, and `--key`.

## `oddarchiver ls`

`ls` reads manifests but not file blobs. By default, it lists only files on the inserted disc.
The default listing does not read the archive baseline. It uses the disc marker, label, and manifests to update the local disc record.
For a marked disc, it prints a notice on stderr because another disc can hold a newer version.
Legacy discs without `volume.json` do not print the notice.

```
oddarchiver ls --test-iso archive.iso
oddarchiver ls --device /dev/sr0 --session 3 --path photos/
oddarchiver ls --test-iso archive.vol2.iso --all-disks --show-deleted
```

Use `--all-disks` to list the archive from the inserted disc's baseline and its own manifests.
Insert the newest disc that you have. This listing does not read other discs.
It prints the source disc and last known session on stderr. It warns you if local records show a newer disc.

The columns are Path, Size, Origin Session, and Last Modified.
`--all-disks` adds a Disc column after these columns. It shows the disc with the newest live version or tombstone.
Origin Session identifies the last write. Size comes from that write.
Last Modified shows the last write time or the deletion time for a tombstone.
The default listing omits deleted paths.

An older v1 baseline has no full history or disc session ranges.
For a live path from that baseline, the catalog uses its known origin session and shows size 0.
If the catalog cannot name an older disc, `ls --all-disks` prints a notice on stderr.
Insert each older disc once. Run `ls` or `browse` to save its name and session range.
The local file is `$XDG_STATE_HOME/oddarchiver/archive-<archive_id>.json`.
Without `XDG_STATE_HOME`, it is `~/.local/state/oddarchiver/archive-<archive_id>.json`.
It also stores the highest disc index that this computer has seen.
If the local disc record exists but is invalid or unreadable, the command stops. Move or delete the named file, then retry.
The command leaves an unreadable file unchanged. It does not write an unchanged disc record again.
Long archive IDs use a SHA-256 digest in the file name.
Disc labels and paths print with control characters escaped. Printable Unicode remains unchanged.

Legacy discs have no archive ID. They list only their own manifests and do not save a local disc record.

## `--path`, `--session`, `--show-deleted`

These flags use the same matching rules on `ls` and `restore`. They are
not the same flag object.

`--path` on `ls` is a single listing filter. `--path` on `restore` is
repeatable (`action=append`), so one invocation can name many items.

A requested path `P` matches archive entry `R` when `R == P` or `R`
starts with `P` with trailing slashes stripped plus `/`. Thus, `--path a/b.txt`
selects that file. `--path d/` (or `--path d`) selects every path under
`d/`.

`--session N` uses the real session number, not the position of a manifest on the inserted disc.
Files added after N do not appear. A file updated after N shows its older state.
With `--all-disks`, N can refer to a session on an earlier disc.

`--show-deleted` applies only to `ls` and `browse`. Tombstones print
with a trailing ` [DELETED at session NNN]` marker. BD-R is write-once,
so the blob from the last write remains on the disc. Restore does not
need this flag. Pass `--session` from before the deletion.

## `oddarchiver restore`

Use `--all-disks` to restore the newest version of each selected live file.
For an ISO, give one image path. Restore finds the needed sibling images.
With a drive, insert any disc from the prompt list.

```
oddarchiver restore /tmp/out --all-disks --test-iso archive.vol2.iso
oddarchiver restore /tmp/out --all-disks --device /dev/sr0
```

Without `--all-disks`, restore reads only the inserted disc and does not ask for another.
It prints a notice if that disc belongs to a marked archive. A legacy disc keeps its original output.
Bare `restore` and `--all-files` select every live file. Use repeatable `--path P` to select specific files or directories.
Do not combine `--all-files` and `--path`.

```
oddarchiver restore /tmp/out --all-disks --test-iso archive.vol2.iso --path a/b.txt
oddarchiver restore /tmp/out --test-iso archive.iso --path gone.txt --session 0
```

The restore-state file records progress outside the destination.
Type `q` or send EOF at an insert prompt to save progress. Run the same command to resume.
Use `--skip-volume N` or type `m N` at a prompt to mark a disc missing.
Use `--unskip-volume N` to clear a saved mark.
Restore reports the newest available older version as stale. It reports a file with no available version as missing.
`--force` writes selected files again even if their checksums match.

If no archive entry matches a requested path at the target session, restore prints a warning.

## `oddarchiver browse`

Use `browse --all-disks` to list the catalog from the inserted disc.
Rows show the disc name for each file. The listing does not read blobs from other discs.
Without `--all-disks`, browse lists only the inserted disc and prints a notice for marked archives.

```
oddarchiver browse --all-disks --test-iso archive.vol2.iso
oddarchiver browse --device /dev/sr0 --show-deleted
```

Browse shows the direct children of the current directory. Directories end with `/`.
A `*` marks a selected path. Selections stay active when you change directories.

Commands:

- `N` opens directory N or selects file N.
- `t N` selects an entry. `m N` does the same.
- `..` goes to the parent directory.
- `s` prints the selected paths.
- `r` asks for a destination, then restores the selected paths.
  If that restore pauses, browse exits with code 3.
- `q` exits without a restore.
- `h` prints the commands.
- EOF exits without a restore.

With `--all-disks`, `r` requests the discs that hold the selected file versions.
Without the flag, `r` uses only the inserted disc.
A selected directory includes its files and subdirectories.
Use `--show-deleted` to show tombstones. To restore a deleted file, choose a session before deletion.

## Recovering a deleted-but-still-present file

1. Run `oddarchiver ls --test-iso archive.iso --show-deleted`.
   Find the path and its `DELETED at session NNN` marker.
2. Restore the path as of the session before deletion:

```
oddarchiver restore /tmp/out --test-iso archive.iso \
    --path gone.txt --session 0
```

Use `--session N-1` when the marker shows session N. The blob is the
last write before the tombstone. `--force` overwrites an existing file.

