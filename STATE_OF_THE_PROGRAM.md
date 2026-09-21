# STATE OF THE PROGRAM

## Current stage

ODDArchiver is a complete, tested command-line tool. The program implements every feature in the original build plan. The full test suite passes: 383 tests passed, 2 deselected (GB-scale `rocky` and `synth` fixtures). The command `python -m pytest tests/ -q -k "not rocky and not synth"` verifies this. Feature list entries with ids 0-20 in `feature_list.json` all show `passes: true`. The project tracks entries for later feature ids 21-33 in `Assist/DONE.md` instead.

The repository is a local, private working copy. It has git history but no configured remote (`git remote -v` returns nothing). The project uses a local-only-working-repo convention. The user pushes to any remote themselves, if and when that happens.

## What is done

- **Core pipeline**: `init`, `sync`, `restore`, `history`, `verify`, `status`, wired end to end through the CLI.
- **Delta compression**: xdelta3-based deltas with a configurable full-vs-delta threshold, computed and applied entirely in memory, with no plaintext temp files.
- **Two burn backends**: `DiscBackend` for a physical BD-R drive and `ISOBackend` for local-file testing, sharing one interface. Real-drive reads go straight to the medium over SG_IO via `xorriso`, never through an OS mount. The `growisofs` command (and the `xorriso`/`mkisofs` build phase it shells out to) reports burn progress. This includes the files-added counter and the percent-done/ETA line of `xorriso`. The program collapses that progress into a single in-place updating line. It does not flood the terminal with one line per update.
- **Three encryption modes**: `none`, `passphrase` (Argon2id plus ChaCha20-Poly1305), and `keyfile` (a per-file wrapped data key). The mode is selected at `init` and read automatically afterward. Blob names and the manifest itself are opaque and encrypted. Reading an encrypted disc reveals no file names or content.
- **Local cache**: a performance-only encrypted blob cache with automatic disc-read fallback on any miss or corruption.
- **Multi-drive mirroring**: `--mirror` writes each session to a second drive or ISO, with failure detection and status reporting.
- **Dry run and test ISO mode**: `--dry-run` previews a sync with no writes. In `--test-iso`, the program runs the full pipeline against a local ISO file with no physical drive.
- **Idempotency**: `init` and `sync` are safe to re-run. An interrupted burn leaves the cache and disc in a consistent, recoverable state. A SIGINT during staging cleans up fully.
- **Verification**: three depths (`fast`, `checksum`, `full`), with per-session pass/fail so one bad session does not block the rest.
- **Configuration**: an optional `~/.config/oddarchiver/config.toml`, with every setting overridable by a CLI flag.
- **23 confirmed bugs fixed** during manual and automated testing, each with a regression test. Fixes covered argument parsing, passphrase handling, and ghost files left after a deletion-only sync. They also covered a corrupt-manifest crash, false "already initialized" reports on blank media, and SIGINT handling. They also covered non-BMP Unicode filenames. They also covered a cache-fallback path that guessed the wrong on-disc blob name on a cold cache. A wrong-session cache-fallback lookup crashed sync() whenever the last write of a changed file was more than one session back. A tmpfs staging area could run out of space mid-job on a large real archive. Real-drive reads were unreliable after a multi-GB burn because the kernel block layer stayed stale. Now `DiscBackend.read_path()` extracts files over SG_IO with `xorriso` instead of depending on an OS mount. It auto-unmounts and retries when a desktop automount holds the device busy. The `DiscBackend.append()` method recognizes the growisofs "unable to pread64(2) primary volume descriptor" failure. It attempts one USB re-enumeration via a root-owned `oddarchiver-usb-reauth` helper, and it retries. Without that helper, it raises a clear error. The error replaces the misleading "-Z" suggestion that growisofs itself gives. A live `init` was also corrupted. The `build_staging()` of the test suite had deleted the same deterministic staging path out from under growisofs. Staging dirs now carry a PID lockfile. Tests isolate `XDG_RUNTIME_DIR` via an autouse fixture. The source scan excludes the staging tree itself, so a nested staging root cannot be archived as user data. See `Assist/HANDOVER_disc_sync_bug.md`, `Assist/HANDOVER_staging_space_bug.md`, and `Assist/HANDOVER_staging_collision_incident.md`. The "Environment-specific hardware limitation" section of this file documents the disc-read mechanism and the growisofs append kernel limit.
- **A security review** covering crypto correctness, key handling, subprocess safety, temp-file exposure, manifest integrity, cache poisoning, and path traversal. All Critical and High findings are fixed, each with a regression test in `tests/test_security.py`.

## Environment-specific hardware limitation (confirmed, not a code defect)

On the USB-bridged BD-R drive of this workstation, `growisofs -M` (session append) can fail with `unable to pread64(2) primary volume descriptor`. The drive and disc are completely healthy when that failure happens. The `growisofs.c` source of `dvd+rw-tools` confirms the root cause. That source shows `-M` always reads the previous session back through a plain kernel `open()`+`pread64()`. The read path is the raw device node, never SG_IO. Nothing signals a kernel media-change event after an in-place burn on this drive. So the cached block-device capacity of the kernel can stay stuck at a stale placeholder indefinitely.

`xorriso` cannot substitute for `-M` here. Its man page states it refuses to write or read multi-session info on `growisofs`-formatted Pseudo-Overwrite BD-R media.

`append()` detects this failure and attempts one automatic recovery. It runs a root-owned helper (`/usr/local/sbin/oddarchiver-usb-reauth`) via `sudo -n`. The helper deauthorizes and then reauthorizes the USB device of the drive. That forces a full re-enumeration and a real capacity probe. Confirmed live. The helper is a one-time manual install plus a no-wildcard sudoers NOPASSWD line. See SETUP.md.

Without that grant, `append()` still raises a clear error. The error tells the user to physically eject and reinsert the disc (or reboot). It also says explicitly not to follow the `-Z` suggestion that growisofs itself gives.

Confirmed live on a disposable test disc. A physical eject/reinsert of the tray also clears the stale kernel cache. That lets `append()` succeed. The software tray close of this drive does not work, so it must be done by hand. The reinsert also triggers the automount monitor of the desktop. That monitor then blocks the SG_IO-based read fix (`DiscBackend.read_path()`) until unmounted. That is also fixed. `read_path()` detects a busy device and auto-unmounts and retries. See the "Environment-specific hardware limitation" section above for the full writeup and live test results.

## Known issues and limitations

From the security review: the High and Medium findings are fixed. Three Low-severity items remain open by choice, not urgency:

- The Argon2id KDF parameters are hardcoded in `crypto.py`, not stored in the manifest. Changing them in a future release would silently stop older archives from decrypting. The code would need to read the parameters back from disc first.
- Encrypted blobs are not bound to their `(session, path)` context through AEAD associated data. This is a low-severity gap in encrypted modes. It does not apply to unencrypted archives.
- `cli.py` still writes the plaintext session-0 manifest to a `NamedTemporaryFile(delete=False)` when reading it back for `--test-iso` mode. The file leaks on disk if `read_manifest` raises before the cleanup call runs.

Other limitations exist by design, not as defects:

- BD-R is write-once. There is no way to remove or shrink a burned session. A mistake on that disc is permanent.
- A missing mirror (see `--mirror` in the README) is never backfilled automatically. Recovering one requires a full restore from the intact primary, plus a fresh `init` on the replacement drive.
- The delta-versus-full threshold (`0.90`) is a module constant in `delta.py`, not exposed through `config.toml`.
- ODDArchiver targets small, slow-changing data such as secrets, configs, and infrastructure-as-code. It is the wrong tool for databases, VM images, or media libraries. See the "What to archive" section of the README.

## What is left

The following items are future work, and the program does not implement them:

- PAR2 recovery blocks per session, for bit-rot correction beyond what `verify` detects.
- `oddarchiver diff --session A --session B`, to show what changed between two historical sessions.
- Shell completions for bash, zsh, and fish.
- A Python API surface for a separate `ODDArchiver-server` project. That project could then call ODDArchiver directly instead of through the CLI, including Vault-backed key retrieval. The `ODDArchiver-server` project does not exist in this repository. ODDArchiver itself has no dependency on Vault or any external service today.

## Test status

`pytest tests/ -q -k "not rocky and not synth"` exits 0: 383 passed, 2 deselected. Coverage spans every module as a unit test. It also covers full init-sync-verify-restore cycles, dry-run invariance, SIGINT cleanup, and interrupted-burn recovery as integration tests. Every test runs against `ISOBackend` or mocked disc I/O. No physical drive is required.

## Hardening since initial completion

The program gained five layers of hardening after those sections were written. Burn failure modes 31a, 31b, and 31d landed in `9a8a087`. They add disc-full detection on both burn paths, prior-session re-verification before reporting, and wrong-disc refusal. Mode 31c diagnoses a mid-write USB disconnect (`900c6c5`, tests in `14f5890`) and was reproduced on real hardware. Mode 31e adds a `--slow` post-burn verify flag (`70c01ac`). The program attempted CD support, then formally refused it in code with a clear error (`c2c8ba7`). No tested toolchain keeps a CD-R appendable. The project extended the media matrix with `bd-dl` and `bdxl` presets, exercised through `ISOBackend` simulation. The project rewrote Python source in every `oddarchiver/` module into Simplified Technical English. Markdown docs have not had that pass yet. A later test audit found four defects after the CD refusal shipped, and all four are fixed and merged. The double-burn guard now compares archive session count instead of the MMC session count (`24e0f7d`). No-ledger session coverage reports "count unknown" instead of fabricated indexes (`5c46c2e`). Dry-run prints the true next-session number (`7eaa757`). A later cleanup deleted an orphaned xorriso helper (`c167115`).

## Development notes

`Assist/TODO.md` and `Assist/progress.md` hold the detailed build history for this project. Git history archives the 25 development sessions recorded in `progress.md` as individual commits.

Two checkboxes remain unchecked in `Assist/TODO.md` even though the code implements both. One covers reading the encryption mode from the disc manifest on every non-init command. The other covers running a post-burn fast verify automatically. Both are confirmed present in `oddarchiver/cli.py`: `_crypto_for_disc`, and the `verify_mod.verify(..., level="fast")` calls inside `_run_init` and `_run_sync`. The current test suite exercises both. The project finished the two items under a later section of the TODO list. The earlier checkboxes were never retroactively checked off. This note corrects the record. No code change is needed. As of `24e0f7d`, `Assist/TODO.md` tracks nothing further as unchecked. `Assist/HANDOVER_unfinished_2026-09-20.md` is the current record of open work.
