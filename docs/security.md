# ODDArchiver — Security Review

Audit performed by Claude Opus 4.7. Code fixes implemented in a Sonnet 4.6 session.

---

## Findings

| ID  | Sev      | Component                                   | Description                                                                                                                                                                                                        | Status    |
|-----|----------|---------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------|
| H1  | High     | `delta.py`                                  | `compute_delta` and `apply_delta` wrote decrypted bytes to a `NamedTemporaryFile` in `/tmp`, violating the "plaintext never on filesystem" guarantee.                                                             | **DONE**  |
| H2  | High     | `restore.py`, `verify.py`, `disc.py`        | Manifest-controlled paths (`entry.path`, `entry.file`, `entry.delta_file`) fed `dest / rel_path` and `backend.read_path()` without validation. A tampered manifest with `../../etc/passwd` could read/write outside the intended directory. | **DONE**  |
| M1  | Medium   | `session.py`                                | Default staging root was `/tmp` with a deterministic name, enabling a local-user DoS and a TOCTOU window.                                                                                                        | **DONE**  |
| M2  | Medium   | `cli.py`                                    | `make_crypto("keyfile", key=key_bytes)` raised `TypeError` because `KeyfileCrypto` has no `key` parameter; `_crypto_for_disc` raised `NotImplementedError` for keyfile mode. The documented keyfile capability was unreachable from the CLI. | **DONE**  |
| L1  | Low      | `crypto.py`                                 | KDF parameters (`m`, `t`, `p`) hardcoded and not embedded in the wire format; a future tuning silently breaks old archives.                                                                                       | TODO      |
| L2  | Low      | `crypto.py`                                 | AEAD calls pass `aad=None`. Blobs are not bound to their `(session_n, rel_path)` context; in null-encryption mode the binding is unauthenticated.                                                                 | TODO      |
| L3  | Low      | `manifest.py`                               | `manifest_checksum` is a bare SHA-256, not an HMAC. On write-once media the attacker cannot rewrite; accepted by design.                                                                                          | WONT-FIX  |
| L4  | Low      | `cli.py`                                    | `_crypto_for_disc` wrote a plaintext manifest to a `NamedTemporaryFile(delete=False)` and only `unlink`s after `read_manifest` returns. If `read_manifest` raises, the temp file leaks (data is already plaintext on disc). | TODO      |
| I1  | Info     | All subprocess sites                        | Every `subprocess.run`/`Popen` uses list form. No `shell=True`. No fix required.                                                                                                                                  | OK        |
| I2  | Info     | Logging                                     | `dispatch()` logs `exc_info=True`. Python traceback formatter does not serialise local-variable values. `_log.warning(...)` uses the exception message, not key material. No fix required.                         | OK        |
| I3  | Info     | `crypto.py`                                 | Argon2id parameters `m=65536, t=3, p=4` exceed OWASP 2024 minima. Acceptable.                                                                                                                                    | OK        |

---

## Fix Details

### H1 — `delta.py`: Eliminate plaintext temp files

**Root cause:** `compute_delta` and `apply_delta` used `tempfile.NamedTemporaryFile` to pass `old_bytes` / `base_bytes` to xdelta3 as a seekable source file.

**Fix:** Replaced with `os.memfd_create` + `/proc/self/fd/{fd}` + `subprocess.Popen(pass_fds=(fd,))`. The anonymous in-memory file is never visible on any filesystem. `tempfile` import removed from `delta.py`.

**Regression tests:** `tests/test_security.py::test_compute_delta_writes_no_plaintext_tmpfile`, `tests/test_security.py::test_apply_delta_writes_no_plaintext_tmpfile`

### H2 — `restore.py`, `verify.py`, `disc.py`: Block path traversal

**Root cause:** No validation on manifest-supplied path fields before use in `dest / rel_path` or `backend.read_path(entry.file)`.

**Fix:** Added three validators to `manifest.py`:
- `validate_blob_path(path)` — accepts only `session_NNN/(full|deltas)/<sha256-hex>`
- `validate_disc_read_path(path)` — accepts blob paths and known manifest/control paths
- `safe_join_under(root, rel_path)` — checks resolved path stays under root

Called in `_read_full`, `_apply_delta_entry` (restore.py), `_check_blobs` (verify.py), and both `read_path` implementations (disc.py). Test fixtures updated to use sha256 blob names matching `session.py`.

**Regression tests:** `tests/test_security.py::test_restore_rejects_traversal_in_entry_path`, `test_restore_rejects_traversal_in_entry_file`, `test_iso_backend_read_path_rejects_dotdot`, `test_validate_blob_path_accepts_legitimate_values`

### M1 — `session.py`: Per-user private staging root

**Root cause:** Default staging root was `tempfile.gettempdir()` (`/tmp`) — shared and world-writable.

**Fix:** Added `_default_staging_root()` which prefers `$XDG_RUNTIME_DIR/oddarchiver` (tmpfs, cleaned on logout) and falls back to `~/.local/state/oddarchiver/staging`. Directory is created with mode `0o700`.

**Regression test:** `tests/test_security.py::test_default_staging_root_is_user_private`

### M2 — `cli.py`: Repair keyfile mode wiring

**Root cause:** `_make_init_crypto` called `make_crypto("keyfile", key=key_bytes)` but `KeyfileCrypto.__init__` takes `keyfile_path: str`. `_crypto_for_disc` raised `NotImplementedError` for keyfile mode. Non-init commands had no `--key` flag.

**Fix:** 
- `_make_init_crypto`: changed to `make_crypto("keyfile", keyfile_path=key_path)`.
- `_crypto_for_disc`: added `key_path: str | None = None` parameter; handles keyfile mode by calling `make_crypto("keyfile", keyfile_path=key_path)`; exits 1 with a clear message if `key_path` is absent.
- Added `--key PATH` to `sync`, `restore`, `history`, `verify`, `status` subparsers.
- All six `_crypto_for_disc(backend)` call sites updated to pass `key_path=getattr(args, "key", None)`.

**Regression tests:** `tests/test_security.py::test_init_keyfile_round_trip`, `test_keyfile_missing_key_arg_exits_1`

---

## Acceptance Criteria Status

- [x] All High and Medium findings have Status DONE
- [x] `docs/security.md` exists with findings table and fix descriptions
- [x] `feature_list.json` id 20 set to `passes: true`
- [x] `pytest tests/ -v` exits 0 (154 tests pass)
- [x] `grep -n "NamedTemporaryFile" oddarchiver/delta.py` → no match
- [x] `grep -nE "Path.+entry\.(path|file|delta_file)" oddarchiver/restore.py oddarchiver/verify.py` → no match
- [x] `grep -n "tempfile\.gettempdir" oddarchiver/session.py` → no match
