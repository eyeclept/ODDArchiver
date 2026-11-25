"""
Author: Richard Baldwin
Date:   2026

Description:
    Optical drive tray control for disc-swap prompts.
"""
# Imports
from __future__ import annotations

import logging
import shutil
import subprocess

# Globals
logger = logging.getLogger(__name__)
_log = logger

# Functions


def open_tray(device: str | None) -> bool:
    """
    Input:  device - drive node such as /dev/sr0, or None for an ISO
    Output: True when the tray opened or an ISO was requested
    Details:
        Runs eject(1) so the user does not press the button. A failure
        only logs; the prompt still works.
    """
    if not device:
        return True
    eject = shutil.which("eject")
    if eject is None:
        _log.warning("eject not found; open the tray manually")
        return False
    try:
        result = subprocess.run(
            [eject, device], capture_output=True, text=True,
        )
    except OSError:
        _log.exception("Could not run eject for %s", device)
        return False
    if result.returncode != 0:
        _log.warning(
            "eject %s failed (exit %d): %s",
            device, result.returncode, result.stderr.strip(),
        )
        return False
    return True
