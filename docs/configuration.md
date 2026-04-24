# ODDArchiver Configuration

`oddarchiver` reads an optional configuration file at
`~/.config/oddarchiver/config.toml`.  All settings have built-in defaults, so
the file is never required.

---

## Precedence

```
built-in defaults  <  config.toml  <  CLI flags
```

A CLI flag supplied at invocation time always wins.  A value in `config.toml`
wins over the built-in default but yields to a CLI flag.

---

## Annotated Template

Copy this to `~/.config/oddarchiver/config.toml` and uncomment the lines you
want to override.

```toml
# Burn device for physical disc operations (DiscBackend).
# device = "/dev/sr0"

# Directory where encrypted blob cache is stored.
# cache_dir = "~/.cache/oddarchiver"

# Temporary staging directory root.  Uses system tempdir when unset.
# staging_dir = ""

# Store a delta instead of a full copy only when the delta is smaller
# than this fraction of the full file size.  Range: 0.0–1.0.
# delta_threshold = 0.90

# Refuse to burn if staging size exceeds this fraction of remaining disc
# space.  Range: 0.0–1.0.
# space_safety_margin = 0.95

# Path to the structured plaintext log file.
# log_file = "~/logs/oddarchiver.log"

# Run a fast integrity verify automatically after every burn.
# post_burn_verify = true

# Default disc size for --test-iso runs.  Accepts: "25gb", "50gb", "93gb",
# or an integer byte count.
# disc_size = "25gb"

[encryption]
# Encryption mode applied at init time.  Options: "none", "passphrase", "keyfile".
# mode = "none"
```

---

## Field Reference

| Field | Type | Default | Notes |
|---|---|---|---|
| `device` | string | `/dev/sr0` | Physical drive path for `DiscBackend` |
| `cache_dir` | path | `~/.cache/oddarchiver` | Local encrypted-blob cache root |
| `staging_dir` | path | *(tempdir)* | Parent for staging directories; unset = system temp |
| `delta_threshold` | float | `0.90` | Delta : full ratio above which full copy is stored |
| `space_safety_margin` | float | `0.95` | Fraction of remaining bytes available to staging |
| `log_file` | path | `~/logs/oddarchiver.log` | Append-only structured log |
| `post_burn_verify` | bool | `true` | Run `verify --level fast` after every burn |
| `disc_size` | string | `25gb` | ISO capacity for `--test-iso` runs |
| `encryption.mode` | string | `none` | `none` / `passphrase` / `keyfile` |

---

## Override Precedence Example

```toml
# config.toml
device = "/dev/sr1"
disc_size = "50gb"
```

```sh
# CLI flag overrides device; disc_size comes from config.toml
oddarchiver sync /data --device /dev/sr2 --test-iso test.iso
# effective: device=/dev/sr2, disc_size=50gb
```
