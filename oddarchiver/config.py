"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    This module loads configuration for oddarchiver.
    It reads ~/.config/oddarchiver/config.toml with tomllib (3.11+) or tomli.
    CLI flags from argparse.Namespace override file values when the flag is not None.
"""
# Imports
import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Globals
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "oddarchiver" / "config.toml"


# Functions

@dataclass
class Config:
    """
    Input:  N/A - constructed by load_config or directly
    Output: N/A
    Details:
        Holds all configuration fields with defaults.
        Path fields use expanduser() when loaded from a file.
    """
    device: str = "/dev/sr0"
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "oddarchiver")
    staging_dir: Path | None = None
    delta_threshold: float = 0.90
    space_safety_margin: float = 0.95
    log_file: Path = field(default_factory=lambda: Path.home() / "logs" / "oddarchiver.log")
    post_burn_verify: bool = True
    encryption_mode: str = "none"
    disc_size: str = "25gb"


def _try_import_tomllib():
    """
    Input:  None
    Output: The tomllib module, or None if not available.
    Details:
        The function uses the standard tomllib module first (Python 3.11 and later).
        If that module is not available, the function uses the tomli package instead.
    """
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib
    try:
        import tomllib  # type: ignore[no-redef]
        return tomllib
    except ImportError:
        pass
    try:
        import tomli as tomllib  # type: ignore[no-redef]
        return tomllib
    except ImportError:
        return None


def load_config(path: Path | None = None) -> Config:
    """
    Input:  path - the path to config.toml. If path is None, the function uses DEFAULT_CONFIG_PATH.
    Output: A Config object. File values override the defaults. If the file is absent, the function returns the defaults.
    Details:
        The config file is optional. If the file is absent, or the tomllib module is not available, the function returns the defaults.
        The function reads these top-level keys: device, cache_dir, staging_dir, delta_threshold,
        space_safety_margin, log_file, post_burn_verify, and disc_size.
        The function also reads an encryption sub-table, [encryption], with the key mode.
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH

    cfg = Config()

    if not path.exists():
        return cfg

    tomllib = _try_import_tomllib()
    if tomllib is None:
        return cfg

    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    if "device" in data:
        cfg.device = str(data["device"])
    if "cache_dir" in data:
        cfg.cache_dir = Path(str(data["cache_dir"])).expanduser()
    if "staging_dir" in data:
        cfg.staging_dir = Path(str(data["staging_dir"])).expanduser()
    if "delta_threshold" in data:
        cfg.delta_threshold = float(data["delta_threshold"])
    if "space_safety_margin" in data:
        cfg.space_safety_margin = float(data["space_safety_margin"])
    if "log_file" in data:
        cfg.log_file = Path(str(data["log_file"])).expanduser()
    if "post_burn_verify" in data:
        cfg.post_burn_verify = bool(data["post_burn_verify"])
    if "disc_size" in data:
        cfg.disc_size = str(data["disc_size"])
    enc = data.get("encryption")
    if isinstance(enc, dict) and "mode" in enc:
        cfg.encryption_mode = str(enc["mode"])

    return cfg


def resolve_config(args: argparse.Namespace, path: Path | None = None) -> Config:
    """
    Input:  args - the parsed argparse.Namespace object. Attributes that are not None override the config.
            path - the path to config.toml. If None, the function uses DEFAULT_CONFIG_PATH.
    Output: A Config object with the CLI flag values applied on top of the file values or the defaults.
    Details:
        Only the attributes present and not None on args override the config.
        The argparse defaults are None. This lets the function tell a user-supplied
        value apart from an absent flag.
    """
    cfg = load_config(path)

    if getattr(args, "device", None) is not None:
        cfg.device = args.device
    if getattr(args, "cache_dir", None) is not None:
        cfg.cache_dir = Path(args.cache_dir).expanduser()
    if getattr(args, "staging_dir", None) is not None:
        cfg.staging_dir = Path(args.staging_dir).expanduser()
    if getattr(args, "disc_size", None) is not None:
        cfg.disc_size = args.disc_size
    if getattr(args, "delta_threshold", None) is not None:
        cfg.delta_threshold = float(args.delta_threshold)
    if getattr(args, "space_safety_margin", None) is not None:
        cfg.space_safety_margin = float(args.space_safety_margin)

    return cfg


def main() -> None:
    """
    Input:  None
    Output: None
    Details:
        Shows each value from load_config(). Use this output to debug the program.
    """
    cfg = load_config()
    for attr, val in vars(cfg).items():
        print(f"{attr}: {val}")


if __name__ == "__main__":
    main()
