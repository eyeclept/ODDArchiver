# verify.py — Integrity Checking

## Overview

`verify()` reads sessions off a disc or ISO and checks their integrity at one of three levels. A failed session does not invalidate others; per-session status is always reported.

---

## Verification Levels

### `--level fast` (default, post-burn)

Run after every burn automatically. Does not read file blobs.

- Re-reads each session's `manifest.json` off disc/ISO.
- Validates `manifest_checksum` (sha256 of manifest with the field set to `""`).
- Checks `manifest.session` field matches the session directory index.
- Checks timestamps are non-decreasing across sessions.

Use when: after every burn; as a quick sanity check.

### `--level checksum` (recommended monthly)

Reads every stored blob; does not reconstruct the full source tree.

- All fast-level checks.
- For each **full** entry: decrypts the blob, sha256s the plaintext, and compares against `result_checksum`.
- For each **delta** entry: decrypts the blob to verify it is readable (auth tag check for encrypted modes).

Use when: periodic health check; after disc transport or storage.

### `--level full`

Exercises the complete pipeline. Runtime: minutes to tens of minutes.

- All fast-level checks.
- All checksum-level checks.
- Restores all sessions to a temporary directory.
- Verifies sha256 of every reconstructed file against `result_checksum`.

Use when: before relying on a disc for recovery; after suspecting bit-rot.

---

## Output Format

```
Session 000: 38 files  -- OK
Session 001:  2 files  -- OK
Session 002:  1 file   -- OK
Session 003:  3 files  -- FAIL
  session_003/full/notes.txt: checksum mismatch
    expected: d9e2f4a1...
    got:      f1c3e8b2...
Session 004:  2 files  -- OK

Result: 1 error across 5 sessions.
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0`  | All sessions passed |
| `1`  | One or more errors detected |

---

## Post-Burn Verify

`--level fast` is called automatically after every burn. The config key `post_burn_verify = "checksum"` upgrades the automatic post-burn verify to checksum level.

---

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `backend` | `BurnBackend` | Source disc or ISO |
| `crypto`  | `CryptoBackend` | Decryption backend (required for checksum/full) |
| `level`   | `"fast"` \| `"checksum"` \| `"full"` | Verification depth |
