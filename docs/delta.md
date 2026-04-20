# ODDArchiver — Delta Compression

ODDArchiver uses xdelta3 to compute binary deltas between successive versions of each file. Storing a delta instead of the full file reduces the per-session disc footprint for files that change incrementally.

---

## Dependency

`xdelta3` must be installed and on `PATH`.

```sh
# Fedora/RHEL
sudo dnf install xdelta

# Debian/Ubuntu
sudo apt install xdelta3
```

---

## How It Works

For each changed file during a `sync`:

1. The previous version (plaintext) is retrieved from cache or disc.
2. `compute_delta(old_bytes, new_path)` runs `xdelta3 -e -c -s <tmp_source> <new_path>` and returns the delta as bytes.
3. `delta_or_full` compares `len(delta)` against `DELTA_THRESHOLD * len(full)`. If the delta is too large relative to the full file, the full content is stored instead.
4. The chosen bytes are then passed to the encryption layer before being written to staging.

Reconstruction (restore) reverses the process: `apply_delta(base_bytes, delta_bytes)` runs `xdelta3 -d -c -s <tmp_base> -`, feeding the delta on stdin and reading the result from stdout.

---

## Threshold Tuning

`DELTA_THRESHOLD = 0.90` (default). A delta larger than 90% of the full file size is discarded in favour of storing the full content.

To understand the trade-off: a delta near 90% saves only ~10% of disc space but adds reconstruction complexity (an extra xdelta3 call per restore). Lower values store deltas more aggressively; higher values store full files more often.

The threshold is a module-level constant in `delta.py`. It is not currently exposed as a config key.

---

## Per-File Log Format

Every file decision is logged at `INFO` level:

```
notes/todo.txt: delta 4KB vs full 42KB -- storing delta
archive.zip: delta 38KB vs full 40KB -- storing full
```

Format: `<filename>: delta <N>KB vs full <N>KB -- storing delta|full`

---

## Parallel Processing

`process_files(jobs, max_workers=4)` runs `delta_or_full` concurrently across a list of `(old_bytes, new_path)` pairs using `ThreadPoolExecutor`. Results are returned in the same order as the input list regardless of completion order.

Delta computation is I/O-bound (subprocess + temp file writes), so thread-level parallelism is effective. The default pool size is 4; it can be adjusted by passing `max_workers` directly.

---

## Error Handling

Both `compute_delta` and `apply_delta` raise `RuntimeError` if xdelta3 exits non-zero. The error message includes the exit code and xdelta3's stderr output.
