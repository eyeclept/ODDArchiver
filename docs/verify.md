# Verify levels

`oddarchiver verify` checks archive integrity at three depths. `init` and
`sync` also run a verify after every successful burn.

## Levels

| Level | Speed | Checks |
|---|---|---|
| `fast` | seconds | Manifest checksums and session ordering. Does not read blob bytes. |
| `checksum` | minutes | Every `fast` check, then decrypts each stored blob and compares sha256 to the recorded checksum. |
| `full` | minutes to hours | Every `checksum` check, then reconstructs files from those blobs and compares each file to `result_checksum`. |

```
oddarchiver verify --test-iso archive.iso --level fast
oddarchiver verify --device /dev/sr0 --level checksum
oddarchiver verify --test-iso archive.iso --level full
```

A failed session does not invalidate the others. `verify` prints pass or
fail per session and exits 1 if any session failed.

## Post-burn verify

`init` and `sync` always verify after a burn. The default level is `fast`.
That pass checks the in-memory manifest. It does not read blob bytes from
the medium. Media-level write corruption can still print
`Verify OK (manifest only).`

`--slow` selects `checksum` instead:

```
oddarchiver init ~/data --test-iso archive.iso --slow
oddarchiver sync ~/data --test-iso archive.iso --slow
```

Checksum verify reads every stored blob from the medium, decrypts it, and
compares sha256 to the recorded checksum. It is slower on a large archive.

`--slow` only changes the level. Post-burn verify still always runs.
