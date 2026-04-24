# cache.py — Local Cache Management

## Purpose

The cache stores encrypted blobs locally to avoid re-reading them from the disc or ISO on every sync. It is a **performance optimization only** — correctness never depends on it. A miss, a corrupt entry, or a missing cache directory is always handled by falling back to the disc/ISO.

---

## Cache Directory Layout

```
~/.cache/oddarchiver/
├── cache_manifest.json        # index: {session:path -> {size}}
└── blobs/
    └── <session>/
        └── <relative/path>.blob   # encrypted blob as written to disc
```

The default root is `~/.cache/oddarchiver`. Override by passing `cache_dir` to `CacheManager`.

---

## Key Schema

Blobs are keyed by `(session, path)`, stored in `cache_manifest.json` as the string `"<session>:<path>"`.

Example entry:

```json
{
  "1:docs/file.txt": {"size": 2048}
}
```

---

## Miss Fallback Behavior

When `get_with_fallback(path, session, backend)` is called and the key is absent or the blob is corrupt:

1. Calls `backend.read_path("session_NNN/full/<path>")` to retrieve the encrypted blob from disc/ISO.
2. Stores the result via `put()` so the next call hits the cache.
3. Returns the blob.

Delta blobs are **not** reconstructed via the delta chain at cache level — the caller is responsible for applying deltas after retrieving the relevant full blob.

---

## Cache Miss Conditions

A `get()` call returns `None` (cache miss) under any of these conditions:

| Condition | Log message |
|---|---|
| Key not in `cache_manifest.json` | `cache miss: <path> session <N> (not in manifest)` |
| Blob file absent | `cache miss: <path> session <N> (blob file missing)` |
| Blob file truncated (size mismatch) | `cache miss: <path> session <N> (partial write: got X expected Y)` |

All misses log at **WARN** level via the `oddarchiver.cache` logger.

---

## When to Clear the Cache Manually

Clear the cache if:

- The disc/ISO has been rebuilt or the label reused for a different archive.
- A blob is suspected corrupt and you want to force a disc re-read.
- Free space is tight on the host machine.

To clear: `rm -rf ~/.cache/oddarchiver`

The next sync will rebuild the cache from disc reads automatically.
