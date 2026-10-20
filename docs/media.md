# Supported media

ODDArchiver writes Digital Versatile Disc (DVD) and Blu-ray Disc (BD).
Millennial Disc (M-DISC) DVD and BD use the presets for the matching normal media.
ODDArchiver does not support Compact Disc (CD). See "CD is not supported" below.
`--disc-size` accepts a named preset, a unit-suffixed size, or a raw byte count.
Preset names match case-insensitively.

## Size presets

| Preset | Nominal | Usable bytes | Write engine |
|---|---|---|---|
| `cd` | 700 MB | 737,280,000 | none (simulation only) |
| `dvd-sl` | 4.7 GB | 4,700,372,992 | growisofs plus genisoimage |
| `dvd-dl` | 8.5 GB | 8,547,991,552 | growisofs plus genisoimage |
| `bd-r` | 25 GB | 24,696,061,952 | growisofs plus genisoimage |
| `bd-dl` | 50 GB | 49,392,123,904 | growisofs plus genisoimage |
| `bdxl` | 100 GB | 99,857,989,632 | growisofs plus genisoimage |

All DVD and BD writes use growisofs plus genisoimage.
`dvd+rw-mediainfo` reports capacity and session information. `xorriso -indev` reads data. It does not report capacity or session counts here.
Commit `f403a6d` reverted capacity reporting from xorriso because `xorriso -toc` misreads POW-formatted BD-R as blank and unsuitable.

The `cd` preset remains available for `ISOBackend` simulation. Tests use it to cover a small-medium packing path. It cannot drive a real CD burn.

BD usable bytes are the largest whole gibibyte (GiB) count that fits the spec user-data size.
`bd-r` is 23 GiB. `bd-dl` is 46 GiB. `bdxl` is 93 GiB.
Dual-layer BD-R spec user data is 50,050,629,632 bytes.

## CD is not supported

The program refuses a CD burn with a clear error. Appendability is the reason, not writing.

ODDArchiver adds one session per `sync` to write-once media. This toolchain
cannot keep a CD appendable. Two blank CD-Rs burned on real hardware
both came back `is written , is closed` with 0 writable blocks:

1. First attempt wrote session 000 with `xorriso -outdev`. The xorriso manual
   explains that result: an `-outdev` whose input drive differs selects the
   modifying write method, and "modified sessions cannot be written to
   appendable media but to blank media only".
2. Second attempt used `-dev` plus `-close off`, which is the documented growing
   method and is supposed to "keep the medium writable for an appended session".
   The disc still closed.

A single session writes correctly both times, so this is not a write failure.
A CD archive could never receive a second session. That defeats the tool's purpose.

growisofs cannot substitute. dvd+rw-tools targets DVD and BD only, and refuses
CD outright. Writing CD needs xorriso or a cdrecord-family tool.

Two further notes come from the same hardware runs:

- `xorriso -dummy on` does not simulate on the Buffalo USB drive tested. It
  reported a successful simulated write and actually consumed the disc. Do not
  trust `-dummy` for diagnosis. Re-read medium state after any probe.
- On blank CD media, `-write_type auto` selects Session At Once, and that drive
  rejected the setup command with "SCSI error condition on command 5Dh SEND CUE
  SHEET: [5 2C 00] Illegal request. Command sequence error." Track At Once
  avoids it. This note is for anyone who revisits CD support.

If someone revisits CD support, the open question is narrow: can any tool leave a
CD-R appendable on this drive? Test `cdrskin -multi` or `cdrecord -multi` on one
blank CD-R and re-read the medium state straight after.

## M-DISC

M-DISC DVD+R reports the DVD+R MultiMedia Command (MMC) profile. M-DISC BD-R reports the BD-R profile. Capacity matches the equivalent normal medium. ODDArchiver has no M-DISC flag or mode.

A drive must support M-DISC writes. Without that support, the disc stays readable, but the drive cannot write it.

## Hardware tests versus ISO simulation

`--test-iso` with `--disc-size PRESET` runs `ISOBackend` at the usable size for that preset.
An empty ISO reports `remaining_bytes` equal to that size.
Each written session reduces `remaining_bytes`.
The value never drops below zero.

Real-hardware status on a USB Buffalo drive:

| Medium | Status |
|---|---|
| CD-R | Burned successfully, twice. Append impossible, so the program refuses CD |
| DVD, all types | No hardware burn. `ISOBackend` simulation only |
| BD-R SL, M-DISC BD-R SL | Burned, appended, checksum-verified, and disconnect-tested on real hardware, all on the same disc |
| BD-R DL and BDXL | No hardware burn. `ISOBackend` simulation only. Not a code gap: those capacity blanks are not on hand |
| M-DISC DVD | No hardware burn. Single-layer M-DISC on hand is BD-R |

Hardware tests confirm batched reads. A CD-R written by ODDArchiver
accepted 3000 `-extract` pairs in one `xorriso` argv. Three 8 MiB blobs
pulled in a single batched call all matched their recorded checksums.
