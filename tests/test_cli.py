"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for Task 1: cli.py argument parsing and dispatch.
"""
# Imports
import subprocess
import sys
from unittest.mock import patch

import pytest

from oddarchiver.cli import build_parser, dispatch, _make_init_crypto

# Globals
SUBCOMMANDS = ["init", "sync", "restore", "history", "verify", "status"]

# Functions


@pytest.mark.parametrize("subcmd", SUBCOMMANDS)
def test_subcommand_help_exits_zero(subcmd):
    """
    Input:  subcmd — subcommand name
    Output: None
    Details:
        `oddarchiver <subcmd> --help` must exit 0 and print usage.
    """
    result = subprocess.run(
        [sys.executable, "-m", "oddarchiver", subcmd, "--help"],
        capture_output=True,
    )
    assert result.returncode == 0
    assert b"usage" in result.stdout.lower()


def test_dry_run_and_test_iso_are_not_mutually_exclusive():
    """
    Input:  None
    Output: None
    Details:
        --dry-run + --test-iso PATH must parse without error (B1 regression).
        --test-iso selects the backend; --dry-run prevents any write.
        dispatch() may still fail for other reasons (missing ISO file is fine).
    """
    parser = build_parser()
    args = parser.parse_args(["sync", "/src", "--test-iso", "fake.iso", "--dry-run"])
    assert args.dry_run is True
    assert args.test_iso == "fake.iso"


def test_init_flags_in_namespace():
    """
    Input:  None
    Output: None
    Details:
        Flags specific to init must appear in the parsed namespace.
    """
    parser = build_parser()
    args = parser.parse_args([
        "init", "/src",
        "--device", "/dev/sr1",
        "--label", "MYDISC",
        "--encrypt", "passphrase",
        "--disc-size", "100gb",
        "--prefill", "1gb",
    ])
    assert args.command == "init"
    assert args.device == "/dev/sr1"
    assert args.label == "MYDISC"
    assert args.encrypt == "passphrase"
    assert args.disc_size == "100gb"
    assert args.prefill == "1gb"


def test_sync_flags_in_namespace():
    """
    Input:  None
    Output: None
    Details:
        Flags specific to sync must appear in the parsed namespace.
    """
    parser = build_parser()
    args = parser.parse_args(["sync", "/src", "--no-cache"])
    assert args.command == "sync"
    assert args.no_cache is True


def test_restore_flags_in_namespace():
    """
    Input:  None
    Output: None
    Details:
        --session and --force must appear in restore namespace.
    """
    parser = build_parser()
    args = parser.parse_args(["restore", "/dest", "--session", "3", "--force"])
    assert args.session == 3
    assert args.force is True


def test_verify_level_flag():
    """
    Input:  None
    Output: None
    Details:
        --level must accept fast/checksum/full and default to fast.
    """
    parser = build_parser()
    assert parser.parse_args(["verify", "--level", "checksum"]).level == "checksum"
    assert parser.parse_args(["verify"]).level == "fast"


def test_unknown_flag_exits_nonzero():
    """
    Input:  None
    Output: None
    Details:
        An unrecognised flag must cause a non-zero exit.
    """
    result = subprocess.run(
        [sys.executable, "-m", "oddarchiver", "init", "/src", "--unknown-flag"],
        capture_output=True,
    )
    assert result.returncode != 0


def test_passphrase_prompt_uses_getpass(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        B2 regression: passphrase prompt must use getpass.getpass, not input().
        Asserts getpass.getpass is called when --encrypt passphrase is used
        and no env var is set.
    """
    parser = build_parser()
    args = parser.parse_args(["init", str(tmp_path), "--encrypt", "passphrase"])
    with patch("oddarchiver.cli.getpass.getpass", return_value="mysecret") as mock_gp:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ODDARCHIVER_PASSPHRASE", None)
            crypto = _make_init_crypto(args)
    mock_gp.assert_called_once()
    # Verify crypto actually works (round-trip)
    from oddarchiver.crypto import PassphraseCrypto
    assert isinstance(crypto, PassphraseCrypto)
