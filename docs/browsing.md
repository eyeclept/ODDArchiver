# Listing, selective restore, and browse

`restore` without a path filter reconstructs the whole live tree. Use
`ls` to see what the archive holds, `restore --path` to pull a subset,
and `browse` when you want to pick that subset interactively.

All three commands share the same connection flags as `history`:
`--device`, `--test-iso`, and `--key`.

## `oddarchiver ls`

Prints one row per archive path from the session manifests. It does not
read file blobs.

```
oddarchiver ls --test-iso archive.iso
oddarchiver ls --device /dev/sr0 --session 3 --path photos/
oddarchiver ls --test-iso archive.iso --show-deleted
```

Columns: Path, Size, Origin Session, Last Modified.

Origin Session is the session that last wrote the path. Size is
`full_size_bytes` of that entry. Last Modified is the timestamp of that
session, or the timestamp of the deleting session when the row is a
tombstone.

Default view: live paths as of the latest session. The listing omits
deleted paths.

## `--path`, `--session`, `--show-deleted`

These flags mean the same matching rules on `ls` and `restore`. They
are not the same flag object.

`--path` on `ls` is a single listing filter. `--path` on `restore` is
repeatable (`action=append`) so one invocation can name many items.

A requested path `P` matches archive entry `R` when `R == P` or `R`
starts with `P` with trailing slashes stripped plus `/`. So `--path a/b.txt`
selects that file. `--path d/` (or `--path d`) selects every path under
`d/`.

`--session N` freezes the tree at session N. Files added later do not
appear. A file updated later shows its content as of N.

`--show-deleted` applies to `ls` and `browse` only. Tombstones print
with a trailing ` [DELETED at session NNN]` marker. BD-R is write-once,
so the blob from the last write is still on the disc. Restore does not
need this flag: pass `--session` from before the deletion.

## `oddarchiver restore --path`

```
oddarchiver restore /tmp/out --test-iso archive.iso --path a/b.txt
oddarchiver restore /tmp/out --test-iso archive.iso \
    --path a/b.txt --path d/ --path notes/todo.md
oddarchiver restore /tmp/out --test-iso archive.iso \
    --path gone.txt --session 0
```

`--force` is the same as elsewhere: overwrite dest files even when checksums match.

If a requested path matches nothing at the target session, stdout gets:

```
warning: no archive entry matches 'foo/bar.txt' as of session N
```

Exit 0 when at least one requested path matched. Exit 1 when none did.

## `oddarchiver browse`

```
oddarchiver browse --test-iso archive.iso
oddarchiver browse --device /dev/sr0 --show-deleted
```

Plain `input()` loop. No extra TUI library. `build_disc_tree()` builds
the live tree once. The listing shows direct children of the
current prefix. Directories end with `/`. A `*` marks a selected path.

Start at archive root (`/`). Selection holds a set of relative paths and
survives directory changes in the same process.

Commands:

- `N` - open directory N, or toggle file N in the selection
- `t N` - toggle entry N, file or directory prefix (alias: `m N`)
- `..` - go up one directory
- `s` - print the current selection
- `r` - prompt for a destination directory, then call
  `restore(paths=sorted(selected), dest=..., backend=..., crypto=...)`
  and exit
- `q` - quit with no restore
- `h` - print the command list
- EOF on `input()` - same as `q`

A selected directory prefix restores every file under it, same as
`restore --path that/dir`.

`--show-deleted` lists tombstones so you can mark them. Restoring a
deleted path at the live session skips it (the delete is in range).
Recover it with `ls --show-deleted` then `restore --path` and
`--session` as below.

## Recovering a deleted-but-still-present file

1. `oddarchiver ls --test-iso archive.iso --show-deleted`
   Find the path and the `DELETED at session NNN` marker.
2. Restore it as of the session before that deletion:

```
oddarchiver restore /tmp/out --test-iso archive.iso \
    --path gone.txt --session 0
```

Use `--session N-1` when the marker is session N. The blob is the last
write before the tombstone. `--force` overwrites a dest file that
already exists.
