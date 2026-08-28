# STATE OF THE PROGRAM

## Current stage

ODDArchiver is a complete, tested command-line tool. Every feature in the original build plan is implemented. The full test suite passes: 186 tests, 0 failures, verified by running `python -m pytest tests/ -q`. `feature_list.json` shows zero features with `passes: false`.

The repository is a local, private working copy. It has git history but no configured remote (`git remote -v` returns nothing). This follows the project's local-only-working-repo convention. The user pushes to any remote themselves, if and when that happens.

## What is done

- **Core pipeline**: `init`, `sync`, `restore`, `history`, `verify`, `status`, wired end to end through the CLI.
- **Delta compression**: xdelta3-based deltas with a configurable full-vs-delta threshold, computed and applied entirely in memory, with no plaintext temp files.
- **Two burn backends**: `DiscBackend` for a physical BD-R drive and `ISOBackend` for local-file testing, sharing one interface. Real-drive reads go straight to the medium over SG_IO via `xorriso`, never through an OS mount. Burn progress from `growisofs` (and the `xorriso`/`mkisofs` build phase it shells out to, including both the files-added counter and xorriso's percent-done/ETA line) is collapsed into a single in-place updating line instead of flooding the terminal with one line per update.
- **Three encryption modes**: `none`, `passphrase` (Argon2id plus ChaCha20-Poly1305), and `keyfile` (a per-file wrapped data key). The mode is selected at `init` and read automatically afterward. Blob names and the manifest itself are opaque and encrypted. Reading an encrypted disc reveals no file names or content.
- **Local cache**: a performance-only encrypted blob cache with automatic disc-read fallback on any miss or corruption.
- **Multi-drive mirroring**: `--mirror` writes each session to a second drive or ISO, with failure detection and status reporting.
- **Dry run and test ISO mode**: `--dry-run` previews a sync with no writes. `--test-iso` runs the full pipeline against a local ISO file with no physical drive.
- **Idempotency**: `init` and `sync` are safe to re-run. An interrupted burn leaves the cache and disc in a consistent, recoverable state. A SIGINT during staging cleans up fully.
- **Verification**: three depths (`fast`, `checksum`, `full`), with per-session pass/fail so one bad session does not block the rest.
- **Configuration**: an optional `~/.config/oddarchiver/config.toml`, with every setting overridable by a CLI flag.
- **22 confirmed bugs fixed** during manual and automated testing, each with a regression test. Fixes covered argument parsing, passphrase handling, and ghost files left after a deletion-only sync. They also covered a corrupt-manifest crash, false "already initialized" reports on blank media, SIGINT handling, non-BMP Unicode filenames, a cache-fallback path that guessed the wrong on-disc blob name on a cold cache, and a tmpfs staging area that could run out of space mid-job on a large real archive. Real-drive reads were unreliable after a multi-GB burn because the kernel block layer stayed stale; `DiscBackend.read_path()` now extracts files over SG_IO with `xorriso` instead of depending on an OS mount, and it auto-unmounts and retries when a desktop automount holds the device busy. `DiscBackend.append()` recognizes growisofs's "unable to pread64(2) primary volume descriptor" failure, attempts one USB re-enumeration via a root-owned `oddarchiver-usb-reauth` helper, and retries; if that helper is not installed it raises a clear error rather than growisofs's own misleading "-Z" suggestion. A live `init` was also corrupted when the test suite's `build_staging()` deleted the same deterministic staging path out from under growisofs; staging dirs now carry a PID lockfile, tests isolate `XDG_RUNTIME_DIR` via an autouse fixture, and the source scan excludes the staging tree itself so a nested staging root cannot be archived as user data. See `Assist/HANDOVER_disc_sync_bug.md`, `Assist/HANDOVER_staging_space_bug.md`, `Assist/HANDOVER_disc_read_mechanism.md`, `Assist/HANDOVER_growisofs_append_kernel_limit.md`, and `Assist/HANDOVER_staging_collision_incident.md`.
- **A security review** covering crypto correctness, key handling, subprocess safety, temp-file exposure, manifest integrity, cache poisoning, and path traversal. All Critical and High findings are fixed, each with a regression test in `tests/test_security.py`.

## Environment-specific hardware limitation (confirmed, not a code defect)

On this workstation's USB-bridged BD-R drive, `growisofs -M` (session
append) can fail with `unable to pread64(2) primary volume descriptor`
even though the drive and disc are completely healthy. Root cause,
confirmed from `dvd+rw-tools`' own `growisofs.c` source: `-M` always
reads the previous session back through a plain kernel `open()`+`pread64()`
on the raw device node (never SG_IO), and nothing signals a kernel
media-change event after an in-place burn on this drive, so the kernel's
cached block-device capacity can stay stuck at a stale placeholder
indefinitely. `xorriso` cannot substitute for `-M` here: its man page
states it refuses to write or read multi-session info on `growisofs`-
formatted Pseudo-Overwrite BD-R media.

`append()` detects this failure and attempts one automatic recovery:
it runs a root-owned helper (`/usr/local/sbin/oddarchiver-usb-reauth`)
via `sudo -n` to deauthorize then reauthorize the drive's USB device,
which forces a full re-enumeration and a real capacity probe. Confirmed
live. The helper is a one-time manual install plus a no-wildcard sudoers
NOPASSWD line; see SETUP.md. Without that grant, `append()` still raises
a clear error telling the user to physically eject and reinsert the disc
(or reboot), and explicitly not to follow growisofs's own `-Z` suggestion.

Confirmed live on a disposable test disc: a physical eject/reinsert of
the tray also clears the stale kernel cache and lets `append()` succeed
(this drive's software tray close does not work, so it must be done by
hand). That reinsert was also found to trigger the desktop's automount
monitor, which then blocks the SG_IO-based read fix
(`DiscBackend.read_path()`) until unmounted -- also fixed, by having
`read_path()` detect a busy device and auto-unmount-and-retry. See
`Assist/HANDOVER_growisofs_append_kernel_limit.md` for the full
writeup and live test results.

## Known issues and limitations

From the security review: the High and Medium findings are fixed. Three Low-severity items remain open by choice, not urgency:

- The Argon2id KDF parameters are hardcoded in `crypto.py`, not stored in the manifest. Changing them in a future release would silently break decryption of older archives. The code would need to read the parameters back from disc first.
- Encrypted blobs are not bound to their `(session, path)` context through AEAD associated data. This is a low-severity gap in encrypted modes. It does not apply to unencrypted archives.
- `cli.py` still writes the plaintext session-0 manifest to a `NamedTemporaryFile(delete=False)` when reading it back for `--test-iso` mode. The file leaks on disk if `read_manifest` raises before the cleanup call runs.

Other limitations exist by design, not as defects:

- BD-R is write-once. There is no way to remove or shrink a burned session. A mistake on that disc is permanent.
- A missing mirror (see `--mirror` in the README) is never backfilled automatically. Recovering one requires a full restore from the intact primary, plus a fresh `init` on the replacement drive.
- The delta-versus-full threshold (`0.90`) is a module constant in `delta.py`, not exposed through `config.toml`.
- ODDArchiver targets small, slow-changing data such as secrets, configs, and infrastructure-as-code. It is the wrong tool for databases, VM images, or media libraries. See the README's "What to archive" section.

## What is left

The following are documented as future work and are not implemented:

- PAR2 recovery blocks per session, for bit-rot correction beyond what `verify` detects.
- `oddarchiver diff --session A --session B`, to show what changed between two historical sessions.
- Shell completions for bash, zsh, and fish.
- A Python API surface for a separate `ODDArchiver-server` project. That project could then call ODDArchiver directly instead of through the CLI, including Vault-backed key retrieval. `ODDArchiver-server` does not exist in this repository. ODDArchiver itself has no dependency on Vault or any external service today.

## Test status

`pytest tests/ -v` exits 0: 186 tests across 20 files. Coverage spans every module as a unit test. It also covers full init-sync-verify-restore cycles, dry-run invariance, SIGINT cleanup, and interrupted-burn recovery as integration tests. Every test runs against `ISOBackend` or mocked disc I/O. No physical drive is required.

## Development notes

`Assist/TODO.md` and `Assist/progress.md` hold the detailed build history for this project. The 25 development sessions recorded in `progress.md` are archived into git history as individual commits.

Two checkboxes remain unchecked in `Assist/TODO.md` even though the code implements both. One covers reading the encryption mode from the disc manifest on every non-init command. The other covers running a post-burn fast verify automatically. Both are confirmed present in `oddarchiver/cli.py`: `_crypto_for_disc`, and the `verify_mod.verify(..., level="fast")` calls inside `_run_init` and `_run_sync`. The current test suite exercises both. The two items were finished under a later section of the TODO list. The earlier checkboxes were never retroactively checked off. This note corrects the record. No code change is needed.
