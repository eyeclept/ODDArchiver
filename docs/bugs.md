# Bug Report and Fix Log

Bugs confirmed during manual testing (sourced from `Assist/bugs.txt`).

---

## B1 — `--test-iso` and `--dry-run` could not coexist

**Symptom:** `oddarchiver sync odd-test --test-iso odd-test.iso --dry-run` exited 1 with "mutually exclusive" error, making it impossible to dry-run against an ISO file.

**Root cause:** `dispatch()` enforced a mutual exclusion between `--dry-run` and `--test-iso`. The restriction was incorrect: `--test-iso` selects the backend; `--dry-run` prevents any write. They are orthogonal.

**Fix:** Removed the mutual exclusion check from `dispatch()`. Both flags may now coexist.

**Regression:** `tests/test_cli.py::test_dry_run_and_test_iso_are_not_mutually_exclusive`

---

## B2 — Passphrase displayed in terminal while typing

**Symptom:** `oddarchiver init ... --encrypt passphrase` showed the passphrase in plain text on the terminal as the user typed.

**Root cause:** `_make_init_crypto` and `_crypto_for_disc` in `cli.py` used `input("Passphrase: ")` instead of `getpass.getpass()`.

**Fix:** Replaced both calls with `getpass.getpass("Passphrase: ")`.

**Regression:** `tests/test_cli.py::test_passphrase_prompt_uses_getpass`

---

## B3 — `AttributeError: 'bytes' object has no attribute 'encode'` on init with passphrase

**Symptom:** `oddarchiver init ... --encrypt passphrase` crashed with `AttributeError: 'bytes' object has no attribute 'encode'` after entering the passphrase.

**Root cause:** `cli.py` called `passphrase.encode()` before passing to `make_crypto`, so `PassphraseCrypto.__init__` received `bytes`. `_derive_key` then called `.encode()` again on the already-`bytes` value.

**Fix:** `PassphraseCrypto.__init__` now normalises the passphrase argument to `bytes` regardless of whether a `str` or `bytes` is passed. `.encode()` was removed from `_derive_key`.

**Regression:** `tests/test_crypto.py::TestPassphraseBytesRegression`

---

## B4 — `restore` produced ghost files after a deletion-only `sync`

**Symptom:** After deleting a file from the source and running `sync`, `restore` still produced the deleted file. `diff -r source restore_dest` showed extra files in the restore.

**Root cause:** `_run_sync` in `cli.py` checked for `changed` and `new_files` to decide whether any work was needed, but did not check for `deleted_files`. When the only change was a deletion, both `changed` and `new_files` were empty, so `_run_sync` returned 0 silently without writing a session. No manifest with a `deleted` list was ever burned, so `restore` had no record of the deletion.

**Fix:** Added `deleted_files` to the no-change check:
```python
deleted_files = [p for p in disc_state if p not in current_state]
if not changed and not new_files and not deleted_files:
    return 0
```

**Regression:** `tests/test_cli_e2e.py::test_sync_deletion_only_writes_session_and_restore_has_no_ghost`

---

## B5 — `verify --level fast` raised `JSONDecodeError` on corrupted manifest

**Symptom:** After appending junk bytes to a manifest file (`echo "junk" >> manifest.json`), running `oddarchiver verify --level fast` printed an unhandled `JSONDecodeError` traceback instead of a clean FAIL report.

**Root cause:** `read_manifest` in `manifest.py` called `json.loads` without catching `json.JSONDecodeError`. The exception propagated through `_read_all_manifests` in `verify.py` and up to the CLI handler.

**Fix:** Wrapped the `json.loads` call in `read_manifest` with a `try/except (json.JSONDecodeError, UnicodeDecodeError)`. On parse failure the function returns a `Manifest` with `suspect=True` and logs a WARNING, matching the behaviour for checksum mismatches.

**Regression:** `tests/test_manifest.py::test_read_manifest_invalid_json_returns_suspect`, `tests/test_manifest.py::test_read_manifest_appended_garbage_returns_suspect`, `tests/test_verify.py::test_fast_fail_on_invalid_json_manifest_no_traceback`

---

## B6 — Missing manifest file caused unhandled error in verify

**Symptom:** After deleting `session_001/manifest.json`, verify raised an unhandled error rather than reporting the session as failed.

**Root cause:** Same as B5 — `read_manifest` was not wrapped. `_read_all_manifests` in `verify.py` caught `OSError` (missing file) but not `JSONDecodeError`. The B5 fix covers this path too: a missing file is caught by the existing `OSError` handler; a corrupt/truncated file is now caught by the new `JSONDecodeError` handler.

**Fix:** Covered by the B5 fix to `read_manifest`.

**Regression:** Covered by B5 regressions.

---

## Test coverage summary

| Bug | File fixed | Regression test |
|-----|-----------|----------------|
| B1 | `cli.py` | `tests/test_cli.py::test_dry_run_and_test_iso_are_not_mutually_exclusive` |
| B2 | `cli.py` | `tests/test_cli.py::test_passphrase_prompt_uses_getpass` |
| B3 | `crypto.py` | `tests/test_crypto.py::TestPassphraseBytesRegression` |
| B4 | `cli.py` | `tests/test_cli_e2e.py::test_sync_deletion_only_writes_session_and_restore_has_no_ghost` |
| B5 | `manifest.py` | `tests/test_manifest.py::test_read_manifest_invalid_json_returns_suspect`, `test_read_manifest_appended_garbage_returns_suspect`, `tests/test_verify.py::test_fast_fail_on_invalid_json_manifest_no_traceback` |
| B6 | `manifest.py` (via B5) | Covered by B5 regressions |
