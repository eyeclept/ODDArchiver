"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for usb_reauth.py, the privileged helper invoked via sudo to
    force a USB optical drive to fully re-enumerate. Covers the security
    validation (device name allowlist, resolution failure, existing
    device check), the toggle sequence itself, and that its
    intentionally-duplicated resolution logic (see module docstring for
    why it cannot import oddarchiver.disc) agrees with disc.py's copy --
    all without touching real hardware or requiring root.
"""
# Imports
from pathlib import Path

import pytest

from oddarchiver.usb_reauth import main

# Functions


def test_rejects_device_name_with_shell_metacharacters(monkeypatch, caplog):
    """
    Input:  monkeypatch, caplog
    Output: None
    Details:
        A device name that does not match the strict "sr<N>" pattern
        must be rejected before any sysfs access is attempted --
        including anything containing shell metacharacters, path
        separators, or option-like strings.
    """
    for bad in ["sr0; rm -rf /", "../../etc/passwd", "sr", "sda", "sr0a"]:
        with caplog.at_level("WARNING"):
            assert main([bad]) == 1
        assert "must match sr<N>" in caplog.text
        caplog.clear()

    # A leading "-" is rejected by argparse itself (treated as an option,
    # not a positional value) before this script's own validation runs --
    # still fails closed, just via a different, standard-library code path.
    with pytest.raises(SystemExit):
        main(["-rf"])


def test_rejects_nonexistent_block_device(monkeypatch, caplog, tmp_path):
    """
    Input:  monkeypatch, caplog, tmp_path
    Output: None
    Details:
        A syntactically valid device name with no corresponding sysfs
        entry must be rejected, not silently proceed.
    """
    import oddarchiver.usb_reauth as mod

    real_path = mod.Path

    def fake_path(arg, *args):
        if arg == "/sys/class/block":
            return real_path(tmp_path / "sys" / "class" / "block")
        return real_path(arg, *args)

    monkeypatch.setattr(mod, "Path", fake_path)

    with caplog.at_level("WARNING"):
        assert main(["sr0"]) == 1
    assert "No such block device" in caplog.text


def test_rejects_device_with_no_usb_ancestor(monkeypatch, caplog, tmp_path):
    """
    Input:  monkeypatch, caplog, tmp_path
    Output: None
    Details:
        If the block device exists but resolution cannot find a USB
        ancestor (e.g. an internal SATA drive that happens to be
        misnamed), main() must reject it and never attempt to write
        anything.
    """
    import oddarchiver.usb_reauth as mod

    block_dir = tmp_path / "sys" / "class" / "block" / "sr0"
    block_dir.mkdir(parents=True)

    real_path = mod.Path

    def fake_path(arg, *args):
        if arg == "/sys/class/block":
            return real_path(tmp_path / "sys" / "class" / "block")
        return real_path(arg, *args)

    monkeypatch.setattr(mod, "Path", fake_path)
    monkeypatch.setattr(mod, "_resolve_usb_device_sysfs_path", lambda name: None)

    with caplog.at_level("WARNING"):
        assert main(["sr0"]) == 1
    assert "Could not resolve a USB ancestor" in caplog.text


def test_toggles_authorized_zero_then_one(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        On a fully valid, resolvable device, main() must write "0" then
        "1" to the resolved path's "authorized" file, in that order, and
        return 0.
    """
    import oddarchiver.usb_reauth as mod

    block_dir = tmp_path / "sys" / "class" / "block" / "sr0"
    block_dir.mkdir(parents=True)
    usb_dir = tmp_path / "usb_device"
    usb_dir.mkdir()
    authorized = usb_dir / "authorized"
    authorized.write_text("1")

    real_path = mod.Path

    def fake_path(arg, *args):
        if arg == "/sys/class/block":
            return real_path(tmp_path / "sys" / "class" / "block")
        return real_path(arg, *args)

    monkeypatch.setattr(mod, "Path", fake_path)
    monkeypatch.setattr(mod, "_resolve_usb_device_sysfs_path", lambda name: usb_dir)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)

    written = []
    real_write_text = Path.write_text

    def tracking_write_text(self, data, *args, **kwargs):
        if self == authorized:
            written.append(data)
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", tracking_write_text)

    assert main(["sr0"]) == 0
    assert written == ["0", "1"]


def test_logs_exception_on_permission_denied(monkeypatch, caplog, tmp_path):
    """
    Input:  monkeypatch, caplog, tmp_path
    Output: None
    Details:
        If the caller lacks permission to write "authorized" (i.e. this
        script was not actually run as root), main() must log the
        exception -- not swallow it -- and return 1.
    """
    import oddarchiver.usb_reauth as mod

    block_dir = tmp_path / "sys" / "class" / "block" / "sr0"
    block_dir.mkdir(parents=True)
    usb_dir = tmp_path / "usb_device"
    usb_dir.mkdir()

    real_path = mod.Path

    def fake_path(arg, *args):
        if arg == "/sys/class/block":
            return real_path(tmp_path / "sys" / "class" / "block")
        return real_path(arg, *args)

    monkeypatch.setattr(mod, "Path", fake_path)
    monkeypatch.setattr(mod, "_resolve_usb_device_sysfs_path", lambda name: usb_dir)

    def raising_write_text(self, *args, **kwargs):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(Path, "write_text", raising_write_text)

    with caplog.at_level("WARNING"):
        assert main(["sr0"]) == 1
    assert "Failed to toggle" in caplog.text


def test_self_contained_resolution_matches_disc_module(tmp_path, monkeypatch):
    """
    Input:  tmp_path
    Output: None
    Details:
        usb_reauth.py deliberately does not import oddarchiver.disc (a
        privileged script must not dynamically load code from the
        invoking user's writable site-packages -- see module
        docstring), so it carries its own duplicated copy of the sysfs
        walk. This test is the guardrail against the two copies
        silently drifting apart: given the exact same fake sysfs tree,
        both must resolve to the exact same path.
    """
    from oddarchiver.disc import resolve_usb_device_sysfs_path
    from oddarchiver.usb_reauth import _resolve_usb_device_sysfs_path

    real = tmp_path / "sys" / "devices" / "usb5" / "5-1" / "5-1.2"
    real.mkdir(parents=True)
    (real / "idVendor").write_text("0411")
    (real / "authorized").write_text("1")
    lun = real / "5-1.2:1.0" / "host8" / "target8:0:0" / "8:0:0:0"
    lun.mkdir(parents=True)

    block_root = tmp_path / "sys" / "class" / "block"
    sr0 = block_root / "sr0"
    sr0.mkdir(parents=True)
    (sr0 / "device").symlink_to(lun, target_is_directory=True)

    disc_result = resolve_usb_device_sysfs_path("/dev/sr0", sys_class_block=block_root)

    import oddarchiver.usb_reauth as mod
    real_path = mod.Path

    def fake_path(arg, *args):
        if arg == "/sys/class/block":
            return real_path(block_root)
        return real_path(arg, *args)

    monkeypatch.setattr(mod, "Path", fake_path)
    helper_result = _resolve_usb_device_sysfs_path("sr0")

    assert disc_result == helper_result == real.resolve()
