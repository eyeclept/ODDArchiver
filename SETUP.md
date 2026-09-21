# SETUP

## Requirements

- Python 3.10 or later
- `xdelta3`: binary delta computation
- `genisoimage`: builds ISO images for `--test-iso` mode and the test suite
- `dvd+rw-tools` (`growisofs`, `dvd+rw-mediainfo`): writes and reads a physical BD-R drive
- `xorriso`: reads files back off a physical BD-R drive directly over SG_IO.
  It never depends on the OS mounting the disc. OS mounting was unreliable
  for several minutes after a real, multi-GB burn. See the
  "Environment-specific hardware limitation" section of
  `STATE_OF_THE_PROGRAM.md`
- `udftools`: optional. Only needed to mount an individual session by hand

Install the system tools:

```sh
# Fedora / RHEL
sudo dnf install xdelta genisoimage dvd+rw-tools xorriso

# Debian / Ubuntu
sudo apt install xdelta3 genisoimage dvd+rw-tools xorriso
```

## Install the Python package

```sh
git clone <this repository>
cd ODDArchiver
pip install -e .                 # core install, no encryption support
pip install -e ".[encrypt]"      # adds argon2-cffi and cryptography, needed for
                                  # passphrase and keyfile encryption modes
```

Verify the install:

```sh
oddarchiver --help
```

## Enable unattended recovery for a stuck USB optical drive (optional, recommended for cron)

On some USB-bridged BD-R drives, the `sync` append step can fail right
after a burn with `unable to pread64(2) primary volume descriptor`. The
cause is a stale kernel block-device capacity cache, not disc corruption
or a code defect. Live testing confirmed this failure. The
"Environment-specific hardware limitation" section of
`STATE_OF_THE_PROGRAM.md` documents it.

`append()` recovers from this automatically. It deauthorizes and then
reauthorizes the USB device of the drive. This forces the kernel to fully
re-enumerate the device.

That toggle needs no tray action and no interactive session. But that
toggle needs root, so it needs a one-time, narrowly scoped `sudo` grant.

Without this step, `sync` still works interactively. The error message
tells you to physically eject and reinsert the disc. But `sync` cannot
self-heal inside an unattended cron job.

Install the helper script itself as a **root-owned** file at a fixed
system path. This is a separate, manual, `sudo`-run step.
`pip install -e .` deliberately does not do it for you.

`pip install -e .` runs as your own unprivileged user, so anything it
puts on your `PATH` is writable by that same user. A `sudo` grant trusts
an exact path string, not who currently controls the file living there.
So the actual privileged script must live somewhere your own account
cannot modify:

```sh
cd ODDArchiver   # this repo's root
sudo install -o root -g root -m 0755 \
    oddarchiver/usb_reauth.py /usr/local/sbin/oddarchiver-usb-reauth
```

Then add exactly this line via `sudo visudo` (or a new file under
`/etc/sudoers.d/`), replacing `<you>` with your own username:

```
<you> ALL=(root) NOPASSWD: /usr/local/sbin/oddarchiver-usb-reauth
```

The exact path above has no wildcards and no argument pattern. The
helper only ever accepts one bare device name (e.g. `sr0`). It validates
it against `^sr[0-9]+$` and re-derives the real USB sysfs path of the
drive itself. It trusts nothing passed to it.

The job of the sudoers rule is only to let this one specific,
already-validating, root-owned script run without a password. It is not
the security boundary for what gets touched. The script is deliberately
self-contained (no `oddarchiver` package import), so root never
dynamically loads code from your own writable site-packages. See the
module docstring of `oddarchiver/usb_reauth.py` for the full security
model.

Re-run the `sudo install` command above any time
`oddarchiver/usb_reauth.py` changes upstream.

## Set up encryption

Encryption is optional. Pick one mode before your first `init`. See the README for a comparison of the three modes.

### Passphrase mode

Set the passphrase in the environment so it is not prompted on every run:

```sh
export ODDARCHIVER_PASSPHRASE="correct horse battery staple"
oddarchiver init ~/ToArchive --test-iso archive.iso --encrypt passphrase
```

If the variable is unset, ODDArchiver prompts for the passphrase with hidden input.

### Keyfile mode

Generate a 32-byte keyfile:

```sh
python3 -c "from oddarchiver.crypto import generate_keyfile; generate_keyfile('/path/to/keyfile.bin')"
```

Store the keyfile somewhere secure, such as an encrypted volume or a hardware token, not next to the archive itself. Pass `--key PATH` on `init` and on every command afterward:

```sh
oddarchiver init ~/ToArchive --test-iso archive.iso --encrypt keyfile --key /path/to/keyfile.bin
oddarchiver sync ~/ToArchive --test-iso archive.iso --key /path/to/keyfile.bin
```

Losing the keyfile makes the archive permanently unreadable. Keep backup copies of it.

## Configuration file

ODDArchiver reads an optional file at `~/.config/oddarchiver/config.toml`. Every setting has a built-in default, so the file is never required.

Precedence, lowest to highest: built-in default, then `config.toml`, then a CLI flag. A CLI flag always wins.

```toml
# device = "/dev/sr0"
# cache_dir = "~/.cache/oddarchiver"
# staging_dir = ""                  # empty = use the per-user default; see below
# delta_threshold = 0.90
# space_safety_margin = 0.95
# log_file = "~/logs/oddarchiver.log"
# post_burn_verify = true
# disc_size = "25gb"

[encryption]
# mode = "none"
```

| Field | Default | Meaning |
|---|---|---|
| `device` | `/dev/sr0` | Physical drive path used when `--test-iso` is not given |
| `cache_dir` | `~/.cache/oddarchiver` | Local encrypted-blob cache |
| `staging_dir` | unset | Staging directory root. Unset uses the per-user default below |
| `delta_threshold` | `0.90` | Store a full file instead of a delta once the delta exceeds this share of the full size |
| `space_safety_margin` | `0.95` | Refuse to burn a session that would use more than this share of remaining disc space |
| `log_file` | `~/logs/oddarchiver.log` | Structured log path. The tool creates parent directories automatically |
| `post_burn_verify` | `true` | Run `verify --level fast` automatically after every burn |
| `disc_size` | `25gb` | Simulated capacity for `--test-iso` runs |
| `encryption.mode` | `none` | `none`, `passphrase`, or `keyfile` |

## Directories ODDArchiver uses

| Directory | Purpose | Override |
|---|---|---|
| `~/.cache/oddarchiver/` | Local blob cache. Performance only, safe to delete | `cache_dir` in `config.toml` |
| `$XDG_RUNTIME_DIR/oddarchiver/`, falling back to `~/.local/state/oddarchiver/staging/` | Staging directory during a burn, created with mode `0o700` | `staging_dir` in `config.toml`* |
| `~/logs/` | Log file directory | `log_file` in `config.toml` |
| `~/.config/oddarchiver/` | Configuration file | not overridable |

\* Not currently wired through the CLI. Setting it in `config.toml` has no
effect yet.

`$XDG_RUNTIME_DIR` is usually a RAM-backed tmpfs sized well below real
disk space (6.2 GiB on a 61 GiB workstation is typical). A session 0
`init` stages every source file as a full blob before burning. So the
whole source tree has to fit there, regardless of the capacity of the
target disc or `/home`.

`init`/`sync` check for this before staging any file and fail fast with
a clear message instead of crashing partway through. See "Staging space
check failed" in the Troubleshooting section of the README.

Clear the cache at any time:

```sh
rm -rf ~/.cache/oddarchiver
```

It rebuilds automatically from disc reads on the next `sync`.

## Cron deployment

```cron
0 2 * * 0 oddarchiver sync ~/Documents/ToArchive --device /dev/sr0
```

A no-change run prints nothing to stdout, so cron does not send mail unless the job fails. Errors go to stderr and to the log file. The process exits 1 on any error, so cron can alert on a non-zero exit code.

## Development setup

```sh
pip install -e .
pip install -r requirements.txt   # pytest, argon2-cffi, cryptography
pytest tests/ -v
```

Consider a change complete only when `pytest tests/ -v` exits 0. All tests use `ISOBackend`. The suite needs no physical drive.
