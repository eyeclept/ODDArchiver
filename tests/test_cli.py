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

import pytest

from oddarchiver.cli import build_parser, dispatch

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


def test_dry_run_and_test_iso_mutual_exclusion():
    """
    Input:  None
    Output: None
    Details:
        --dry-run + --test-iso must cause dispatch() to return 1.
    """
    parser = build_parser()
    args = parser.parse_args(["init", "/src", "--dry-run", "--test-iso", "test.iso"])
    assert dispatch(args) == 1


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
