#!/usr/bin/env python3
"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Standalone privileged helper, run via sudo, that forces a USB optical
    drive's kernel device object to be destroyed and recreated
    (deauthorize then reauthorize). That forces a full USB
    re-enumeration, including a real capacity probe -- which fixes the
    specific case where growisofs -M's own kernel-block-layer read of
    the previous session's volume descriptor fails because the kernel
    never got an accurate capacity for the disc currently in the drive.
    Confirmed live; see Assist/HANDOVER_growisofs_append_kernel_limit.md.

    Security model: this script is the only thing that needs root, and
    it does exactly one narrow thing. It takes exactly one argument, a
    bare block device name (e.g. "sr0"), validated against a strict
    "sr<N>" pattern -- a naming convention the kernel itself enforces
    only for SCSI CD-ROM devices, so this can never be pointed at some
    other class of hardware. It never accepts a raw sysfs path from the
    caller: it re-derives the USB device's sysfs path itself (walking
    from a real, existing block device, exactly like
    disc.resolve_usb_device_sysfs_path() does for the unprivileged
    caller) and writes only "0" then "1" to that one resolved
    'authorized' file.

    Deliberately self-contained -- stdlib only, no `from oddarchiver
    import ...`. This script runs as root via sudo while `oddarchiver`
    itself is installed into the invoking *user's* writable site-packages
    (a normal `pip install --user -e .`); importing that package here
    would mean root dynamically executing code from a location the
    unprivileged user can modify, which is a real privilege-escalation
    risk in exactly the way a reviewed setuid binary must never allow.
    The ~15 lines of resolution logic below are intentionally duplicated
    from oddarchiver/disc.py's resolve_usb_device_sysfs_path() rather
    than imported; tests/test_usb_reauth.py asserts both copies resolve
    identically so they cannot silently drift apart.

    NOT installed via `pip install -e .` / `[project.scripts]` --
    anything pip puts on the unprivileged user's own PATH is, by
    definition, writable by that same user, which would make the same
    problem the self-contained-imports fix above solves reappear one
    level up (sudoers trusts an exact path string, not who currently
    controls the file living there). Instead this source file is copied
    directly, once, to a root-owned system path with a manual
    `sudo install` step (see SETUP.md):
    `sudo install -o root -g root -m 0755 oddarchiver/usb_reauth.py
    /usr/local/sbin/oddarchiver-usb-reauth`. The sudoers NOPASSWD grant
    and `DiscBackend._attempt_usb_reauth()` both reference that fixed,
    root-owned path directly.
"""
# Imports
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path

# Globals
_log = logging.getLogger(__name__)
_DEVICE_NAME_RE = re.compile(r"^sr[0-9]+$")

# Functions


def _resolve_usb_device_sysfs_path(device_name: str) -> Path | None:
    """
    Input:  device_name - bare block device name, e.g. "sr0"
    Output: Path to the device's USB sysfs directory (the one whose
            "authorized" attribute controls whether the kernel treats it
            as plugged in), or None if it cannot be resolved.
    Details:
        Intentionally duplicated from disc.py's
        resolve_usb_device_sysfs_path() -- see the module docstring for
        why this file does not import the oddarchiver package. Walks
        /sys/class/block/<name>/device (a symlink into the real device
        tree) up through its ancestors until it finds a directory that
        has both "idVendor" and "authorized". Keep in sync with
        disc.py's copy; tests/test_usb_reauth.py checks they agree.
    """
    class_path = Path("/sys/class/block") / device_name
    try:
        node = (class_path / "device").resolve(strict=True)
    except OSError:
        return None
    while node != node.parent:
        if (node / "idVendor").exists() and (node / "authorized").exists():
            return node
        node = node.parent
    return None


def main(argv: list[str] | None = None) -> int:
    """
    Input:  argv - CLI args, defaults to sys.argv[1:]
    Output: int - exit code (0 on success, 1 on any failure)
    Details:
        Validates the device name, resolves it to a USB sysfs path,
        then writes "0" then "1" to that path's "authorized" attribute.
        Every failure path logs a warning naming exactly why before
        returning non-zero, so a cron job's logs show the real reason
        rather than a silent no-op.
    """
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="oddarchiver-usb-reauth",
        description=(
            "Force a USB optical drive to fully re-enumerate "
            "(deauthorize then reauthorize), clearing a stale kernel "
            "block-device capacity cache without touching the tray."
        ),
    )
    parser.add_argument("device_name", help='Bare block device name, e.g. "sr0"')
    args = parser.parse_args(argv)

    if not _DEVICE_NAME_RE.match(args.device_name):
        _log.warning(
            "Rejected device name %r: must match sr<N> (e.g. sr0)",
            args.device_name,
        )
        return 1

    class_path = Path("/sys/class/block") / args.device_name
    if not class_path.is_dir():
        _log.warning("No such block device: %s", class_path)
        return 1

    usb_path = _resolve_usb_device_sysfs_path(args.device_name)
    if usb_path is None:
        _log.warning(
            "Could not resolve a USB ancestor for %s; it may not be a "
            "USB-attached drive",
            args.device_name,
        )
        return 1

    authorized = usb_path / "authorized"
    try:
        authorized.write_text("0")
        time.sleep(1.0)
        authorized.write_text("1")
    except OSError:
        _log.exception("Failed to toggle %s", authorized)
        return 1

    print(f"Re-enumerated {args.device_name} via {usb_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
