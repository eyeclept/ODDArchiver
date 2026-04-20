# ODDArchiver — Manifest Format

Each session written to disc produces a `manifest.json` file stored at `session_NNN/manifest.json`. Manifests are the authoritative record of what was archived and how to reconstruct it.

---

## JSON Schema

```json
{
  "version": 1,
  "session": 0,
  "timestamp": "2026-04-19T14:00:00Z",
  "source": "/home/user/documents",
  "label": "ARCHIVE",
  "based_on_session": null,
  "encryption": {
    "mode": "none"
  },
  "entries": [
    {
      "path": "notes/todo.txt",
      "type": "full",
      "file": "session_000/full/notes_todo.txt",
      "result_checksum": "sha256:<hex>",
      "full_size_bytes": 1024,
      "source_checksum": "sha256:<hex>",
      "delta_file": "",
      "delta_size_bytes": 0,
      "encrypted_dek": ""
    }
  ],
  "deleted": [],
  "manifest_checksum": "<hex>"
}
```

### Top-level fields

| Field | Type | Description |
|---|---|---|
| `version` | `int` | Schema version — currently `1` |
| `session` | `int` | Session number (0-based) |
| `timestamp` | `str` | ISO 8601 UTC timestamp |
| `source` | `str` | Absolute path of the archived directory |
| `label` | `str` | UDF volume label |
| `based_on_session` | `int \| null` | Session this one diffs against; `null` for session 0 |
| `encryption` | `object` | Encryption mode and parameters (see [encryption.md](encryption.md)) |
| `entries` | `list` | Per-file records (see below) |
| `deleted` | `list[str]` | Relative paths removed since the previous session |
| `manifest_checksum` | `str` | SHA-256 of the manifest with this field set to `""` |

### Entry fields

| Field | Type | Description |
|---|---|---|
| `path` | `str` | Relative path within the source directory |
| `type` | `str` | `"full"` or `"delta"` |
| `file` | `str` | Disc-relative path to the stored full blob |
| `delta_file` | `str` | Disc-relative path to the stored delta blob (delta entries only) |
| `result_checksum` | `str` | SHA-256 of the reconstructed file after applying all deltas |
| `source_checksum` | `str` | SHA-256 of the source file at scan time |
| `full_size_bytes` | `int` | Size of the reconstructed file in bytes |
| `delta_size_bytes` | `int` | Size of the delta blob in bytes (delta entries only) |
| `encrypted_dek` | `str` | Base64-encoded encrypted DEK (keyfile mode only; empty otherwise) |

---

## Checksum Algorithm

`manifest_checksum` is the SHA-256 of the canonical JSON representation of the manifest, with `manifest_checksum` itself set to `""` before hashing. Canonical form uses sorted keys and no extra whitespace (`json.dumps(..., sort_keys=True, separators=(",", ":"))` in Python).

---

## SUSPECT Manifests

A manifest is marked `SUSPECT` when its stored `manifest_checksum` does not match the computed value on read. This indicates tampering or corruption.

- `read_manifest` sets `manifest.suspect = True` and logs a `WARNING`; it does not raise.
- `build_disc_state` skips SUSPECT manifests and logs a `WARNING` for each one skipped.
- `oddarchiver status` surfaces all SUSPECT sessions.

SUSPECT sessions are excluded from disc-state reconstruction but do not prevent other sessions from being used.

---

## Disc-State Reconstruction

`build_disc_state(manifests)` replays a list of manifests in ascending session order to produce the last known state of every file:

```
{relative_path: result_checksum, ...}
```

Rules:
- Each entry overwrites the same path from a prior session.
- Each path in `deleted` is removed from the state map.
- SUSPECT manifests are skipped entirely.

The result reflects the disc contents as of the last non-SUSPECT session.

---

## Atomic Writes

`write_manifest` writes to `manifest.json.tmp` then calls `os.replace()` to rename atomically. A process crash during the write leaves a `.tmp` file but never a partial `manifest.json`.
