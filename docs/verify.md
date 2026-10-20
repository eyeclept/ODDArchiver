# Verify levels

`oddarchiver verify` checks archive integrity at three depths. `init` and
`sync` also verify after each successful burn.

## Levels

| Level | Speed | Checks |
|---|---|---|
| `fast` | seconds | Manifest checksums and session ordering. Does not read blob bytes. |
| `checksum` | minutes | Runs every `fast` check. Then decrypts each stored blob and compares its sha256 to the recorded checksum. |
| `full` | minutes to hours | Runs every `checksum` check. Then reconstructs files from those blobs and compares each file to `result_checksum`. |

```
oddarchiver verify --test-iso archive.iso --level fast
oddarchiver verify --device /dev/sr0 --level checksum
oddarchiver verify --test-iso archive.iso --level full
```

A failed session does not invalidate the others. `verify` prints pass or
fail for each session. It exits 1 if any session failed.

## Interrupted sessions

`verify` and `status` check for an interrupted burn after the last
session with a valid manifest. The check first uses the session listing of
the medium. If that listing is not available, it uses the control files
the command already read. Only then does it use the session count
from `mediainfo`. The check does not read more paths from the medium.

## Temporary files

`checksum` and `full` verify decrypt streamed blobs into a temporary directory. If
`verify` cannot remove that directory, it prints a warning with the path
to stderr. Remove that directory by hand. It can hold decrypted
plaintext.

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
compares its sha256 to the recorded checksum. It is slower on a large archive.

`--slow` only changes the level. Post-burn verify still always runs.
