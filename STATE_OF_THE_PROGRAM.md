# STATE OF THE PROGRAM

## Current stage

ODDArchiver is a complete, tested command-line tool. The program implements every feature in the original build plan. The full test suite passes: 383 tests passed, 2 deselected (GB-scale `rocky` and `synth` fixtures). Run `python -m pytest tests/ -q -k "not rocky and not synth"` to verify this.

All feature list entries with ids 0-20 in `feature_list.json` show `passes: true`. The project tracks later feature ids 21-33 in `Assist/DONE.md` instead.

The repository is a local, private working copy with git history. It has no configured remote (`git remote -v` returns nothing). The project uses a local-only-working-repo convention. The user pushes to any remote themselves, if and when that happens.

## What is done

- **Core pipeline**: The CLI connects `init`, `sync`, `restore`, `history`, `verify`, and `status` end to end.
- **Delta compression**: xdelta3 computes and applies deltas entirely in memory, with no plaintext temp files. A configurable threshold selects full files or deltas.
- **Two burn backends**: `DiscBackend` handles a physical BD-R drive. `ISOBackend` handles local-file testing. Both use one interface. Real-drive reads go straight to the medium over SG_IO via `xorriso`, never through an OS mount. The `growisofs` command reports burn progress, including its `xorriso`/`mkisofs` build phase. This includes the files-added counter and the percent-done/ETA line of `xorriso`. The program shows that progress on a single line that updates in place. It does not print a separate line for each update.
- **Three encryption modes**: `none`, `passphrase` (Argon2id plus ChaCha20-Poly1305), and `keyfile` (a per-file wrapped data key). The user selects the mode at `init`. Later commands read the mode automatically. The program encrypts the manifest and uses opaque, encrypted blob names. Reading an encrypted disc reveals no file names or content.
- **Local cache**: The encrypted blob cache only improves performance. On any miss or corruption, the program automatically reads the disc instead.
- **Multi-drive mirroring**: `--mirror` writes each session to a second drive or ISO. The program detects failures and reports status.
- **Dry run and test ISO mode**: `--dry-run` previews a sync without writes. In `--test-iso`, the program runs the full pipeline against a local ISO file without a physical drive.
- **Idempotency**: Users can safely run `init` and `sync` again. An interrupted burn leaves the cache and disc consistent and recoverable. A SIGINT during staging runs the full staging cleanup.
- **Verification**: The program supports three depths (`fast`, `checksum`, `full`). Per-session pass/fail lets it continue after a bad session.
- **Configuration**: The optional `~/.config/oddarchiver/config.toml` sets defaults. A CLI flag can override every setting.
- **Disc usage stats**: `status --stats` estimates usage from manifests. `status --stats-exact` lists real on-disc file sizes. `--json` gives machine-readable output. See [docs/stats.md](docs/stats.md).
- **23 confirmed bugs fixed** during manual and automated testing, each with a regression test. Fixes covered argument parsing, passphrase handling, and ghost files left after a deletion-only sync. They also covered a corrupt-manifest crash, false "already initialized" reports on blank media, and SIGINT handling. Fixes also covered non-BMP Unicode filenames.

  A cache-fallback path guessed the wrong on-disc blob name on a cold cache. A wrong-session cache-fallback lookup crashed sync() whenever the last write of a changed file was more than one session back. A tmpfs staging area could run out of space mid-job on a large real archive. Real-drive reads were unreliable after a multi-GB burn because the kernel block layer stayed stale.

  Now `DiscBackend.read_path()` extracts files over SG_IO with `xorriso` instead of depending on an OS mount. It auto-unmounts and retries when a desktop automount holds the device busy. `DiscBackend.append()` recognizes the growisofs "unable to pread64(2) primary volume descriptor" failure. It attempts one USB re-enumeration via a root-owned `oddarchiver-usb-reauth` helper, then retries. Without that helper, it raises a clear error. The error replaces the misleading "-Z" suggestion that growisofs itself gives.

  A live `init` was also corrupted. The test suite used `build_staging()` to delete the same deterministic staging path out from under growisofs. Staging dirs now carry a PID lockfile. Tests isolate `XDG_RUNTIME_DIR` via an autouse fixture. The source scan excludes the staging tree itself. A nested staging root cannot become archived user data.

  See `Assist/HANDOVER_disc_sync_bug.md`, `Assist/HANDOVER_staging_space_bug.md`, and `Assist/HANDOVER_staging_collision_incident.md`. The "Environment-specific hardware limitation" section of this file documents the disc-read mechanism and the growisofs append kernel limit.
- **A security review** covered crypto correctness, key handling, subprocess safety, temp-file exposure, manifest integrity, cache poisoning, and path traversal. The project fixed all Critical and High findings. Each has a regression test in `tests/test_security.py`.

## Environment-specific hardware limitation (confirmed, not a code defect)

On the USB-bridged BD-R drive of this workstation, `growisofs -M` (session append) can fail with `unable to pread64(2) primary volume descriptor`. The drive and disc remain completely healthy when that failure happens. The `growisofs.c` source of `dvd+rw-tools` confirms the root cause. That source shows `-M` always reads the previous session through a plain kernel `open()`+`pread64()`. The read path uses the raw device node, never SG_IO.

Nothing signals a kernel media-change event after an in-place burn on this drive. The kernel can therefore keep a stale placeholder as its cached block-device capacity indefinitely.

`xorriso` cannot substitute for `-M` here. Its man page states that it refuses to write or read multi-session info on `growisofs`-formatted Pseudo-Overwrite BD-R media.

`append()` detects this failure and attempts one automatic recovery. It runs a root-owned helper (`/usr/local/sbin/oddarchiver-usb-reauth`) via `sudo -n`. The helper deauthorizes and then reauthorizes the USB device of the drive. This forces a full re-enumeration and a real capacity probe. Live testing confirmed this.

The helper requires a one-time manual install and a no-wildcard sudoers NOPASSWD line. See SETUP.md.

Without that grant, `append()` still raises a clear error. The error tells the user to physically eject and reinsert the disc (or reboot). It also explicitly warns against the `-Z` suggestion from growisofs.

Live testing on a disposable test disc confirmed this. A physical eject/reinsert of the tray also clears the stale kernel cache. This lets `append()` succeed. The software tray close of this drive does not work, so the user must close it by hand.

Reinsertion also triggers the desktop automount monitor. That monitor then blocks the SG_IO-based read fix (`DiscBackend.read_path()`) until unmounted. That is also fixed. `read_path()` detects a busy device, auto-unmounts, and retries. See the "Environment-specific hardware limitation" section above for the full writeup and live test results.

## Known issues and limitations

The security review found High and Medium issues that are now fixed. Three Low-severity items remain open by choice, not urgency:

- The code hardcodes the Argon2id KDF parameters in `crypto.py` rather than storing them in the manifest. Changing them in a future release would silently stop older archives from decrypting. The code would need to read the parameters back from disc first.
- Encrypted blobs lack AEAD associated data that binds them to their `(session, path)` context. This is a low-severity gap in encrypted modes. It does not apply to unencrypted archives.
- `cli.py` still writes the plaintext session-0 manifest to a `NamedTemporaryFile(delete=False)` when reading it back for `--test-iso` mode. The file leaks on disk if `read_manifest` raises before the cleanup call runs.

Other limitations are design choices, not defects:

- BD-R is write-once. Users cannot remove or shrink a burned session. A mistake on that disc is permanent.
- The program never automatically backfills a missing mirror (see `--mirror` in the README). Recovery requires a full restore from the intact primary and a fresh `init` on the replacement drive.
- The delta-versus-full threshold (`0.90`) is a module constant in `delta.py`. Users cannot set it through `config.toml`.
- ODDArchiver targets small, slow-changing data such as secrets, configs, and infrastructure-as-code. It is the wrong tool for databases, VM images, or media libraries. See the "What to archive" section of the README.

## What is left

The program does not implement these future items:

- PAR2 recovery blocks per session, for bit-rot correction beyond what `verify` detects.
- `oddarchiver diff --session A --session B`, to show what changed between two historical sessions.
- Shell completions for bash, zsh, and fish.
- A Python API for a separate `ODDArchiver-server` project. That project could then call ODDArchiver directly instead of through the CLI, including Vault-backed key retrieval. The `ODDArchiver-server` project does not exist in this repository. ODDArchiver itself has no dependency on Vault or any external service today.
- A disc visualizer that maps which disc regions hold which data type, per session.

## Test status

`pytest tests/ -q -k "not rocky and not synth and not test_no_plaintext_in_tmp"` exits 0: 911 passed, 4 deselected (as of `816ceca`). Unit tests cover every module. Integration tests cover full init-sync-verify-restore cycles, dry-run invariance, SIGINT cleanup, interrupted-burn recovery, and multi-volume fill and restore. Every test runs against `ISOBackend` or mocked disc I/O. No physical drive is required. Real DVD-R results are in `Assist/REAL_DISC_TESTS.md`.

## Hardening since initial completion

The program gained five layers of hardening after the project wrote those sections. Burn failure modes 31a, 31b, and 31d landed in `9a8a087`. They detect a full disc on both burn paths, re-verify prior sessions before reporting, and refuse the wrong disc. Mode 31c diagnoses a mid-write USB disconnect (`900c6c5`, tests in `14f5890`). Testing on real hardware reproduced it.

Mode 31e adds a `--slow` post-burn verify flag (`70c01ac`). The program attempted CD support, then formally refused it in code with a clear error (`c2c8ba7`). No tested toolchain keeps a CD-R appendable. The project added `bd-dl` and `bdxl` presets to the media matrix and exercised them through `ISOBackend` simulation.

The project rewrote Python source in every `oddarchiver/` module into Simplified Technical English. Markdown docs have not had that pass yet. A later test audit found four defects after the CD refusal shipped. The project fixed and merged all four.

The double-burn guard now compares archive session count instead of the MMC session count (`24e0f7d`). No-ledger session coverage reports "count unknown" instead of fabricated indexes (`5c46c2e`). Dry-run prints the true next-session number (`7eaa757`). A later cleanup deleted an orphaned xorriso helper (`c167115`).

Multi-volume archives and opt-in fill merged in `816ceca`. A sync can span several discs. `--fill` appends new sessions to an older disc with room, using two-phase intent and outcome stubs on the newest disc. `restore --all-disks` reads every available disc, so a lost newest disc does not block restore. Real DVD-R tests T16-T19 passed (packing, `--split-large`, fill, and a cache-free restore). A disc with a confirmed media write error is closed for appends in local memory. A post-merge audit strengthened 21 tests. It also found that a plain sync did not record volume free space; that is fixed. Design and limits: `docs/multi-volume-fill.md` and `docs/volumes.md`.

## Development notes

`Assist/TODO.md` and `Assist/progress.md` hold the detailed build history for this project. Git history stores the 25 development sessions recorded in `progress.md` as individual commits.

Two checkboxes remain unchecked in `Assist/TODO.md` even though the code implements both. One covers reading the encryption mode from the disc manifest on every non-init command. The other covers automatically running a post-burn fast verify. The code in `oddarchiver/cli.py` confirms both: `_crypto_for_disc`, and the `verify_mod.verify(..., level="fast")` calls inside `_run_init` and `_run_sync`.

The current test suite exercises both. The project finished the two items under a later section of the TODO list. The project never retroactively checked off the earlier checkboxes. This note corrects the record. No code change is needed.

As of `24e0f7d`, `Assist/TODO.md` tracks nothing further as unchecked. `Assist/HANDOVER_unfinished_2026-09-20.md` is the current record of open work.
