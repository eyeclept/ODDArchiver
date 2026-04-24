"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for oddarchiver/config.py — load_config and resolve_config.
"""
# Imports
import argparse
from pathlib import Path

import pytest

from oddarchiver.config import Config, load_config, resolve_config

# Functions


def test_missing_config_no_error(tmp_path):
    """Missing config file returns Config with defaults; no exception raised."""
    cfg = load_config(tmp_path / "nonexistent.toml")
    assert isinstance(cfg, Config)


def test_default_values(tmp_path):
    """Each default field has the expected hard-coded value."""
    cfg = load_config(tmp_path / "nonexistent.toml")
    assert cfg.device == "/dev/sr0"
    assert cfg.delta_threshold == pytest.approx(0.90)
    assert cfg.space_safety_margin == pytest.approx(0.95)
    assert cfg.post_burn_verify is True
    assert cfg.encryption_mode == "none"
    assert cfg.disc_size == "25gb"
    assert cfg.staging_dir is None
    # cache_dir and log_file are Path objects under home
    assert isinstance(cfg.cache_dir, Path)
    assert isinstance(cfg.log_file, Path)


def test_config_file_loaded(tmp_path):
    """Values from config.toml are applied over defaults."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'device = "/dev/sr1"\n'
        "delta_threshold = 0.75\n"
        'disc_size = "50gb"\n'
        "post_burn_verify = false\n"
        "\n[encryption]\nmode = \"passphrase\"\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.device == "/dev/sr1"
    assert cfg.delta_threshold == pytest.approx(0.75)
    assert cfg.disc_size == "50gb"
    assert cfg.post_burn_verify is False
    assert cfg.encryption_mode == "passphrase"


def test_cli_flag_overrides_config(tmp_path):
    """CLI flag value overrides the corresponding config file value."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('device = "/dev/config-device"\ndisc_size = "50gb"\n')

    args = argparse.Namespace(device="/dev/cli-device", disc_size=None)
    cfg = resolve_config(args, path=cfg_file)

    # --device was explicitly set; should override config file
    assert cfg.device == "/dev/cli-device"
    # --disc-size was not set (None); should come from config file
    assert cfg.disc_size == "50gb"


def test_resolve_none_args_use_defaults(tmp_path):
    """resolve_config with all-None args falls back to file/defaults cleanly."""
    args = argparse.Namespace()
    cfg = resolve_config(args, path=tmp_path / "nonexistent.toml")
    assert cfg.device == "/dev/sr0"
    assert cfg.disc_size == "25gb"


def test_config_cache_dir_expanduser(tmp_path):
    """cache_dir with ~ is expanded to an absolute path."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('cache_dir = "~/.cache/odd_test"\n')
    cfg = load_config(cfg_file)
    assert not str(cfg.cache_dir).startswith("~")
    assert "odd_test" in str(cfg.cache_dir)
