# Testing

All tests live in `tests/` and run with `pytest`.  No physical disc is required
— every test uses `ISOBackend` or mock objects.

---

## Running Tests

```bash
# All tests
pytest tests/ -v

# One module
pytest tests/test_delta.py -v

# Integration only
pytest tests/test_integration.py -v

# With coverage
pip install pytest-cov
pytest tests/ --cov=oddarchiver --cov-report=term-missing
```

`pytest tests/ -v` must exit 0 before any feature is marked done.

---

## Test File Overview

| File | Type | Covers |
|---|---|---|
| `test_scaffolding.py` | unit | module imports, --help, pyproject entry point |
| `test_cli.py` | unit | argparse flags, mutual exclusions, unknown flags |
| `test_manifest.py` | unit | checksum round-trip, SUSPECT detection, disc-state replay |
| `test_delta.py` | unit | xdelta3 round-trip, threshold logic, process_files ordering |
| `test_disc.py` | unit | ISOBackend init/append/mediainfo, prefill, double-burn guard |
| `test_session.py` | unit | build_staging layout, SIGINT cleanup, space check |
| `test_cache.py` | unit | put/get round-trip, miss fallback, partial-write handling |
| `test_crypto.py` | unit | NullCrypto, PassphraseCrypto, KeyfileCrypto round-trips |
| `test_restore.py` | unit | full restore, --session stop, non-destructive, force, checksum mismatch |
| `test_verify.py` | unit | fast/checksum/full levels, per-session failure isolation |
| `test_config.py` | unit | defaults, file loading, CLI override precedence |
| `test_logging.py` | unit | log format, levels, capacity thresholds, parent-dir creation |
| `test_dryrun.py` | unit | dry-run report, ISO invariance, cache invariance, OVERAGE no exit 1 |
| `test_cli_e2e.py` | integration | init, sync no-change, sync changed, history, status |
| `test_idempotency.py` | integration | init re-run, sync twice, burn failure, stale staging cleanup |
| `test_integration.py` | integration | full init+sync+verify_full+restore cycle; dry-run ISO invariance; SIGINT cleanup; burn failure cache isolation |

---

## ISOBackend vs Real Disc

`ISOBackend` is a drop-in replacement for `DiscBackend` that stores sessions
in a directory tree alongside a UDF ISO file built with `genisoimage`.  It
supports the full `BurnBackend` interface (`init`, `append`, `mediainfo`,
`read_path`) and requires no physical disc drive.

All integration tests use `ISOBackend` via the `--test-iso PATH` argument
(or by constructing it directly).  To test against a real disc, substitute
`--device /dev/sr0` (or whatever your drive is).

---

## Key Fixtures and Helpers

- **`_make_args(**kwargs)`** — builds an `argparse.Namespace` with test-safe
  defaults; call `dispatch(_make_args(...))` to exercise the full CLI dispatch
  path without invoking a subprocess.
- **`_make_backend(remaining_bytes=…)`** — returns a `MagicMock` with a
  `mediainfo()` that returns a `DiscInfo` with the given space budget.  Used
  in unit tests that call `build_staging` directly.
- **`_staging_root=tmp_path/…`** — `build_staging` accepts this optional
  parameter to place the staging dir inside `tmp_path` for full test isolation.

---

## System Dependencies

Some tests invoke `xdelta3` and `genisoimage` via subprocess.  `init.sh`
checks for these at startup.  Install with:

```bash
# Fedora / RHEL
sudo dnf install xdelta genisoimage

# Debian / Ubuntu
sudo apt install xdelta3 genisoimage
```

Tests that do not call these tools (unit tests for manifest, config, crypto,
etc.) run without them.

---

## Done Criteria (Task 15)

- `pytest tests/ -v` exits 0 (118 tests, all pass).
- Integration tests use `ISOBackend` only; no physical disc required.
- `docs/testing.md` exists.
