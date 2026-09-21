"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for cli.py argument parsing and dispatch.
"""
# Imports
import subprocess
import sys
from unittest.mock import patch

import pytest

from oddarchiver.cli import build_parser, dispatch, _make_backend, _make_init_crypto
from oddarchiver.disc import (
    BDXL_DISC_BYTES,
    BD_DL_DISC_BYTES,
    CD_DISC_BYTES,
    DVD_DL_DISC_BYTES,
    DVD_SL_DISC_BYTES,
    ISOBackend,
)

# Globals
SUBCOMMANDS = ["init", "sync", "restore", "history", "verify", "status", "ls", "browse"]

# Functions


@pytest.mark.parametrize("subcmd", SUBCOMMANDS)
def test_subcommand_help_exits_zero(subcmd):
    """
    Input:  subcmd - subcommand name
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
        --dry-run plus --test-iso PATH must parse without error (B1 regression).
        The test only calls parse_args. It does not call dispatch().
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


@pytest.mark.parametrize("preset,expected", [
    ("cd", CD_DISC_BYTES),
    ("dvd-sl", DVD_SL_DISC_BYTES),
    ("dvd-dl", DVD_DL_DISC_BYTES),
    ("bd-r", ISOBackend.DEFAULT_DISC_BYTES),
    ("bd-dl", BD_DL_DISC_BYTES),
    ("bdxl", BDXL_DISC_BYTES),
])
def test_make_backend_honors_disc_size_presets(preset, expected, tmp_path):
    """
    Input:  preset, expected, tmp_path
    Output: None
    Details:
        --disc-size with each media preset name must parse and construct
        an ISOBackend whose disc_size and empty remaining_bytes match
        the preset.
    """
    parser = build_parser()
    iso = tmp_path / "archive.iso"
    args = parser.parse_args([
        "init", "/src", "--test-iso", str(iso), "--disc-size", preset,
    ])
    backend = _make_backend(args)
    assert isinstance(backend, ISOBackend)
    assert backend.disc_size == expected
    assert backend.mediainfo().remaining_bytes == expected


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



def test_read_mode_flag_defaults_and_choices():
    """
    Input:  None
    Output: None
    Details:
        restore and verify expose --read-mode with default sequential.
    """
    parser = build_parser()
    assert parser.parse_args(["restore", "/dest"]).read_mode == "sequential"
    assert parser.parse_args(["verify"]).read_mode == "sequential"
    assert parser.parse_args(
        ["restore", "/dest", "--read-mode", "full-copy"]
    ).read_mode == "full-copy"


def test_unknown_flag_exits_nonzero():
    """
    Input:  None
    Output: None
    Details:
        An unrecognized flag must cause a non-zero exit.
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
        Prompts twice (passphrase plus confirm). Both return the same value so the loop exits.
    """
    parser = build_parser()
    args = parser.parse_args(["init", str(tmp_path), "--encrypt", "passphrase"])
    with patch("oddarchiver.cli.getpass.getpass", return_value="mysecret") as mock_gp:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ODDARCHIVER_PASSPHRASE", None)
            crypto = _make_init_crypto(args)
    assert mock_gp.call_count == 2  # passphrase prompt + confirm prompt
    from oddarchiver.crypto import PassphraseCrypto
    assert isinstance(crypto, PassphraseCrypto)


def test_passphrase_confirm_mismatch_retries(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        When passphrase and confirm do not match, the loop retries.
        Sequence: wrong/wrong, wrong/wrong, correct/correct. getpass called 6 times.
    """
    parser = build_parser()
    args = parser.parse_args(["init", str(tmp_path), "--encrypt", "passphrase"])
    # Alternate: (pass1, confirm_bad), (pass2, confirm_bad2), (good, good)
    side_effects = ["aaa", "bbb", "ccc", "ddd", "final", "final"]
    with patch("oddarchiver.cli.getpass.getpass", side_effect=side_effects) as mock_gp:
        with patch("oddarchiver.cli.sys.stderr"):
            import os
            os.environ.pop("ODDARCHIVER_PASSPHRASE", None)
            crypto = _make_init_crypto(args)
    assert mock_gp.call_count == 6
    from oddarchiver.crypto import PassphraseCrypto
    assert isinstance(crypto, PassphraseCrypto)


def test_slow_flag_on_init_and_sync_only():
    """
    Input:  None
    Output: None
    Details:
        --slow is accepted on init and sync and defaults to False.
        Commands that do not burn reject the flag.
    """
    parser = build_parser()
    assert parser.parse_args(["init", "/src"]).slow is False
    assert parser.parse_args(["init", "/src", "--slow"]).slow is True
    assert parser.parse_args(["sync", "/src"]).slow is False
    assert parser.parse_args(["sync", "/src", "--slow"]).slow is True
    with pytest.raises(SystemExit):
        parser.parse_args(["verify", "--slow"])
    with pytest.raises(SystemExit):
        parser.parse_args(["restore", "/dest", "--slow"])

