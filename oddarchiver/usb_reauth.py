#!/usr/bin/env python3
"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    This is a standalone privileged helper script that a user runs through
    sudo, then destroys and recreates the kernel device object of a USB
    optical drive (it deauthorizes the device, then reauthorizes it).
    This forces a full USB re-enumeration, including a real capacity probe.

    The probe fixes a specific case.
    growisofs -M reads the volume descriptor of the previous session
    through the kernel block layer, and that read fails because the
    kernel never got an accurate capacity for the disc in the drive.
    This behavior is confirmed live.
    See the "Environment-specific hardware limitation" section of STATE_OF_THE_PROGRAM.md.

    Security model: this script is the only part of the program that needs
    root access, and it does exactly one narrow task. It takes one
    argument, a bare block device name (for example "sr0"). The script
    validates the name against a strict "sr<N>" pattern.

    The kernel itself enforces this naming convention only for SCSI CD-ROM
    devices, so the script can never target another class of hardware. The
    script never accepts a raw sysfs path from the caller.

    Instead, the script re-derives the sysfs path of the USB device itself.
    It walks from a real, existing block device.
    disc.resolve_usb_device_sysfs_path() does the same for the
    unprivileged caller. The script then writes only "0" and then "1" to
    that one resolved 'authorized' file.

    This script is deliberately self-contained: it uses only the standard
    library, with no `from oddarchiver import ...`.
    The script runs as root through sudo.
    Also, pip installs `oddarchiver` itself into the writable site-packages
    of the invoking user, through a normal `pip install --user -e .`.

    If this script imported that package, root would dynamically run code
    from a location the unprivileged user can modify. That is a real
    privilege-escalation risk, of the kind a reviewed setuid binary must
    never allow.

    For this reason, the roughly 15 lines of resolution logic below
    duplicate resolve_usb_device_sysfs_path() in oddarchiver/disc.py,
    instead of importing it.
    tests/test_usb_reauth.py checks that both copies resolve to the same
    result, so they cannot silently drift apart.

    Do not install this script through `pip install -e .` or `[project.scripts]`.
    Anything pip puts on the PATH of the unprivileged user is, by definition,
    writable by that same user, so pip would bring back the same problem,
    one level up, that the self-contained-imports design above solves.
    Sudoers trusts an exact path string, not whoever currently controls the
    file at that path.

    Instead, an administrator copies this source file once, directly to a
    root-owned system path, with a manual `sudo install` step (see SETUP.md):
    `sudo install -o root -g root -m 0755 oddarchiver/usb_reauth.py
    /usr/local/sbin/oddarchiver-usb-reauth`.
    The sudoers NOPASSWD grant and `DiscBackend._attempt_usb_reauth()` both
    reference that same fixed, root-owned path directly.
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
    Output: Path to the USB sysfs directory of the device. The
            "authorized" attribute in this directory controls whether
            the kernel treats the device as plugged in. The function
            returns None if it cannot resolve the path.
    Details:
        This function duplicates resolve_usb_device_sysfs_path() in
        disc.py. See the module docstring for
        the reason this file does not import the oddarchiver package.
        The function walks up from /sys/class/block/<name>/device, a
        symlink into the real device tree, through its ancestors. It
        stops at the first directory that has both an "idVendor" file
        and an "authorized" file.
        Keep this function in sync with the copy in disc.py.
        tests/test_usb_reauth.py checks that both copies agree.
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
    Input:  argv - the command line arguments. The default is sys.argv[1:].
    Output: int - the exit code. It is 0 on success and 1 on any failure.
    Details:
        The function checks the device name. It then resolves the name to
        a USB sysfs path.
        It writes "0" and then "1" to the "authorized" attribute at that
        path.
        Each failure path logs a warning that names the exact reason.
        The function then returns a non-zero code. This lets the log of
        a cron job show the real reason instead of a silent failure with
        no output.
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
