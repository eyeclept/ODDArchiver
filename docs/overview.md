# ODDArchiver — Project Overview

ODDArchiver creates incremental, delta-compressed backups on write-once optical media (BD-R). Each `sync` appends a new session containing only changed files as xdelta3 deltas. The full source tree can be reconstructed from any point in history.

---

## Install

```sh
# Clone and install (editable)
pip install -e .

# With encryption support
pip install -e ".[encrypt]"

# Verify
oddarchiver --help
```

Requires Python 3.10+. The `[encrypt]` extra adds `argon2-cffi` and `pyage`.

---

## Module Map

| Module | Purpose |
|---|---|
| `cli.py` | Argument parsing and subcommand dispatch |
| `disc.py` | Burn backends: `DiscBackend` (physical BD-R) and `ISOBackend` (test ISO) |
| `manifest.py` | Session manifest read/write/merge and disc-state reconstruction |
| `delta.py` | xdelta3 wrapper: compute, apply, threshold selection, parallel processing |
| `session.py` | Staging directory construction (scan, diff, encrypt, space check) |
| `cache.py` | Local encrypted-blob cache with disc-read fallback |
| `crypto.py` | Encryption layer: `NullCrypto`, `PassphraseCrypto`, `KeyfileCrypto` |
| `restore.py` | Reconstruct source tree from disc/ISO at any historical session |
| `verify.py` | Integrity checking at fast / checksum / full depth |

---

## Source Structure

```
oddarchiver/
    __init__.py
    __main__.py       # python -m oddarchiver entry point
    cli.py
    disc.py
    manifest.py
    delta.py
    session.py
    cache.py
    crypto.py
    restore.py
    verify.py
tests/
    test_scaffolding.py
    test_cli.py
    test_disc.py
    test_manifest.py
    test_delta.py
Assist/
    init.sh           # baseline smoke test
    progress.md       # append-only session log
    base.py           # code formatting reference
docs/                 # user documentation (this directory)
pyproject.toml
```

---

## Running `init.sh`

`Assist/init.sh` is the baseline smoke test. Run it at the start of every session before touching any code:

```sh
bash Assist/init.sh
```

It installs the package in editable mode, installs test dependencies, imports all modules, and confirms `oddarchiver --help` exits 0. All steps must pass before any work begins.

---

## Running Tests

```sh
pytest tests/ -v
```

Individual module tests:

```sh
pytest tests/test_scaffolding.py -v
pytest tests/test_cli.py -v
pytest tests/test_disc.py -v
pytest tests/test_manifest.py -v
pytest tests/test_delta.py -v
```

Tests use `ISOBackend` exclusively — no physical disc required.
