# session.py — Staging Directory Construction

## Overview

`session.py` implements `build_staging`, which orchestrates steps 3–7 of the sync algorithm: source scan, diff, delta/full staging, space check, and manifest write.

---

## Staging Directory Layout

`build_staging` creates a temporary directory via `tempfile.mkdtemp` and populates it as follows:

```
<tmpdir>/
└── session_NNN/
    ├── manifest.json
    ├── full/
    │   └── <relative/path/to/new_or_full_file>   # encrypted
    └── deltas/
        └── <relative/path/to/changed_file>.xdelta # encrypted delta
```

The caller receives the `<tmpdir>` path and is responsible for passing it to the burn backend. On any error (including Ctrl+C), the temp directory is removed automatically.

---

## Sync Algorithm Steps 3–7

### Step 3 — Scan Source

`pathlib.Path.rglob("*")` walks the source directory. `sha256` is computed for each regular file. Result: `{relative_path: sha256_hex}`.

### Step 4 — Diff

Three classifications are produced by comparing `current_state` against `disc_state` (from `manifest.build_disc_state()`):

| Class     | Condition                                               |
|-----------|---------------------------------------------------------|
| `changed` | Path exists in both; checksums differ                  |
| `new`     | Path exists in source only                             |
| `deleted` | Path exists in disc_state only; logged, not acted on   |

If `changed + new == 0`, the caller should exit 0 silently (no-change cron run). `build_staging` does not enforce this — it returns an empty staging dir.

### Step 5 — Stage Changed Files

For each changed file:

1. Retrieve old encrypted blob via `cache.get_with_fallback(path, base_session, backend)` (falls back to disc read on cache miss).
2. Decrypt to memory: `crypto.decrypt(encrypted_old)`.
3. Compute `delta_or_full(old_bytes, new_path)` — returns `("delta", bytes)` or `("full", bytes)`.
4. Encrypt result: `crypto.encrypt(blob)`.
5. Write to `session_NNN/deltas/<path>.xdelta` (delta) or `session_NNN/full/<path>` (full).

Plaintext never touches the filesystem — all encrypt/decrypt happens in memory.

### Step 6 — Stage New Files

For each new file:

1. Read file into memory.
2. Encrypt: `crypto.encrypt(file_bytes)`.
3. Write to `session_NNN/full/<relative_path>`.

### Space Check

```
staging_bytes = du -sb <tmpdir>/
limit         = backend.mediainfo().remaining_bytes * SPACE_SAFETY_MARGIN (0.95)
```

If `staging_bytes >= limit`, logs ERROR and raises `SystemExit(1)`. The except clause removes the staging dir before exit.

### Step 7 — Write Manifest

A `Manifest` is constructed with all `ManifestEntry` objects and the `deleted` list, then written atomically to `session_NNN/manifest.json` via `manifest.write_manifest`.

`label` and `encryption` fields are written as empty defaults; the caller (CLI layer) should fill these in before burning.

---

## SIGINT Cleanup Behavior

`build_staging` installs `_handle_sigint` as the `SIGINT` handler at entry. The handler sets `_sigint_received = True`; it does not raise immediately.

After file staging is complete, `build_staging` checks `_sigint_received`. If set, it raises `KeyboardInterrupt`. The `except BaseException` block removes the staging directory via `shutil.rmtree` before re-raising.

Result: Ctrl+C at any point during staging (scan, diff, file writes) leaves no orphaned temp directory.

---

## Space Safety Margin

`SPACE_SAFETY_MARGIN = 0.95` — staging must use less than 95% of disc remaining space. This leaves headroom for filesystem overhead and ensures the burn does not fail mid-write.

Tunable via `config.toml`:

```toml
[defaults]
space_safety_margin = 0.95
```

---

## Key Parameters

| Parameter    | Type               | Description                                          |
|--------------|--------------------|------------------------------------------------------|
| `session_n`  | `int`              | Session number (0 = init, N = sync)                  |
| `source`     | `Path`             | Source directory to archive                          |
| `disc_state` | `dict[str, str]`   | `{rel_path: sha256}` from `build_disc_state()`       |
| `backend`    | `BurnBackend`      | Used for disc reads on cache miss and space check    |
| `cache`      | `CacheManager`     | Encrypted blob cache                                 |
| `crypto`     | `CryptoBackend`    | Encrypt/decrypt; use `NullCrypto` for mode 0         |

Returns the staging `Path`. Caller is responsible for cleanup after burn.
