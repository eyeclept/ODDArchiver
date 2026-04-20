# ODDArchiver — Burn Backends

Two backends implement the `BurnBackend` interface: `DiscBackend` for physical BD-R drives and `ISOBackend` for ISO files used in testing and `--test-iso` mode.

---

## System Tool Requirements

| Backend | Required tools |
|---|---|
| `DiscBackend` | `growisofs`, `dvd+rw-mediainfo` |
| `ISOBackend` | `genisoimage` |

Install on Fedora/RHEL: `sudo dnf install dvd+rw-tools genisoimage`

---

## `DiscBackend`

Wraps a physical optical drive at a block device path (default `/dev/sr0`).

- **`init`** burns session 0 with `growisofs -Z` (new disc).
- **`append`** adds a session with `growisofs -M` (existing disc).
- **`mediainfo`** runs `dvd+rw-mediainfo` and parses session count, remaining blocks, and total capacity from stdout. Remaining and capacity values are reported in 2 KB blocks; the backend multiplies by 2048 to produce bytes.
- **`read_path`** locates the disc mount point via `/proc/mounts` and reads the file directly. Raises `RuntimeError` if the device is not mounted.

---

## `ISOBackend`

Simulates a multi-session disc using a UDF ISO file. Used for `--test-iso` mode and all automated tests — no physical drive required.

### How sessions are stored

All session content accumulates in a sibling directory `<iso>.d/` next to the ISO file. On every `init` or `append`, the ISO is rebuilt from that directory using `genisoimage`. The ISO is written atomically (`.tmp` then rename) to prevent partial files.

`read_path` reads from `<iso>.d/` directly without mounting the ISO.

### Capacity simulation

`ISOBackend` is constructed with a `disc_size` parameter (default 23 GiB, approximating a 25 GB BD-R). `mediainfo` reports `used_bytes` as the current ISO file size plus any prefill, and `remaining_bytes` as `disc_size - used_bytes`.

### `--prefill`

`prefill(prefill_bytes)` writes a small metadata file recording the simulated used space. No real data is written. This allows testing capacity warning thresholds without creating gigabytes of content.

```sh
# Simulate a disc that is already 20 GiB used on a 23 GiB disc
oddarchiver init ./source --test-iso test.iso --prefill 20gib
```

---

## `DiscInfo` Dataclass

Returned by `mediainfo()` on both backends.

| Field | Type | Description |
|---|---|---|
| `session_count` | `int` | Number of sessions written so far |
| `used_bytes` | `int` | Bytes consumed (ISO size + prefill) |
| `remaining_bytes` | `int` | Bytes still available |
| `label` | `str` | UDF volume label |

---

## Double-Burn Guard

Both backends re-read the session count immediately before every write. If the count differs from the value recorded at the start of the run, the write is aborted with `RuntimeError`. This prevents writing a duplicate session if the disc was written concurrently (e.g. two processes, a crashed previous run that actually succeeded).

---

## Size String Format

`parse_disc_size(size_str)` accepts human-readable sizes for `--disc-size` and `--prefill`:

| Format | Example |
|---|---|
| Decimal (SI) | `25gb`, `100mb`, `1tb` |
| Binary (IEC) | `23gib`, `512mib` |
| Raw bytes | `24696061952` |

Case-insensitive.
