# ODDArchiver Logging

All log output is written to a structured plaintext file. No messages are
written to stdout; errors also appear on stderr for immediate CLI feedback.

---

## Log Format

```
TIMESTAMP LEVEL [module] message
```

Example lines:

```
2026-04-24T12:34:56Z INFO [oddarchiver.session] Starting sync for session 3
2026-04-24T12:34:57Z WARNING [oddarchiver.cli] Disc capacity 82% used — 4.1 GiB remaining
2026-04-24T12:35:01Z ERROR [oddarchiver.cli] Disc capacity 96% used — 900.0 MiB remaining; disc nearly full
2026-04-24T12:35:02Z SUSPECT [oddarchiver.cli] session_002: manifest checksum mismatch
```

Timestamps are UTC; the trailing `Z` denotes Zulu (UTC) time.

---

## Log Levels

| Level | Value | Meaning |
|---|---|---|
| `INFO` | 20 | Normal operational events |
| `WARNING` | 30 | Disc capacity ≥80%; non-fatal anomalies |
| `SUSPECT` | 35 | Manifest checksum mismatch detected |
| `ERROR` | 40 | Disc capacity ≥95%; operation failure |

`SUSPECT` is a custom level between `WARNING` and `ERROR`. It is always
surfaced in `oddarchiver status` output as well as the log.

---

## Default Log Path

```
~/logs/oddarchiver.log
```

The path is configurable in `~/.config/oddarchiver/config.toml`:

```toml
log_file = "~/logs/oddarchiver.log"
```

Parent directories are created automatically if they do not exist.

---

## Capacity Threshold Table

| Used % | Level | Output |
|---|---|---|
| < 80% | INFO | `Disc capacity N% used — X remaining` |
| ≥ 80% | WARNING | `Disc capacity N% used — X remaining` |
| ≥ 95% | ERROR | `Disc capacity N% used — X remaining; disc nearly full` |

---

## Cron-Safe Stdout Rules

- All log messages go to the log file only.
- ERROR-level messages also go to stderr.
- Nothing is written to stdout unless a command produces user-facing output
  (e.g., `history`, `status`).
- A no-change `sync` run produces zero stdout output — safe for cron with
  `MAILTO=""` or output redirect.

---

## SUSPECT Surfacing

When `oddarchiver status` finds any manifest with a bad checksum, it:

1. Prints the session list to stdout (for the operator).
2. Logs each entry at `SUSPECT` level so the event is recorded in the log file.

To check the log for SUSPECT entries:

```sh
grep SUSPECT ~/logs/oddarchiver.log
```
