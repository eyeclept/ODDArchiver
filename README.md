# ODDArchiver

Incremental delta-compressed backups to write-once optical media (BD-R) or ISO files.

Each `sync` appends a new session containing only changed files as xdelta3 deltas. The full source tree can be reconstructed from any point in history.

## Install

```sh
# Clone the repository, then:
pip install -e .

# With encryption support (PassphraseCrypto / KeyfileCrypto)
pip install -e ".[encrypt]"
```

Requires Python 3.10+. System tools `xdelta3` and `genisoimage` must be on `PATH`.

```sh
# Fedora/RHEL
sudo dnf install xdelta genisoimage dvd+rw-tools

# Debian/Ubuntu
sudo apt install xdelta3 genisoimage dvd+rw-tools
```

## Quick start

```sh
# Create session 0 (full snapshot) on a test ISO
oddarchiver init ~/Documents/ToArchive --test-iso archive.iso --label MYARCHIVE

# Sync changes (incremental)
oddarchiver sync ~/Documents/ToArchive --test-iso archive.iso

# Verify integrity
oddarchiver verify --test-iso archive.iso --level checksum

# Restore to a directory
oddarchiver restore /tmp/restored --test-iso archive.iso

# Physical BD-R drive
oddarchiver init ~/Documents/ToArchive --device /dev/sr0 --label MYARCHIVE
oddarchiver sync ~/Documents/ToArchive --device /dev/sr0
```

## Documentation

Full how-to guide: [docs/GUIDE.md](docs/GUIDE.md)

Reference documentation:

- [docs/overview.md](docs/overview.md) — architecture and module map
- [docs/cli.md](docs/cli.md) — all commands and flags
- [docs/quickstart.md](docs/quickstart.md) — command examples
- [docs/encryption.md](docs/encryption.md) — encryption modes
- [docs/configuration.md](docs/configuration.md) — config file reference
- [docs/mirror.md](docs/mirror.md) — writing sessions to two drives simultaneously
