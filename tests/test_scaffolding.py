"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for Task 0: project scaffolding — module imports, entry point, pyproject.toml.
"""
# Imports
import importlib
import subprocess
import sys
from pathlib import Path

# Globals
PROJECT_ROOT = Path(__file__).parent.parent
MODULES = [
    "oddarchiver",
    "oddarchiver.__main__",
    "oddarchiver.cli",
    "oddarchiver.disc",
    "oddarchiver.manifest",
    "oddarchiver.delta",
    "oddarchiver.session",
    "oddarchiver.cache",
    "oddarchiver.crypto",
    "oddarchiver.restore",
    "oddarchiver.verify",
]

# Functions


def test_all_modules_importable():
    """
    Input:  None
    Output: None
    Details:
        Each module in MODULES must import without raising any exception.
    """
    for name in MODULES:
        importlib.import_module(name)


def test_help_exits_zero():
    """
    Input:  None
    Output: None
    Details:
        `oddarchiver --help` subprocess must exit 0.
    """
    result = subprocess.run(
        [sys.executable, "-m", "oddarchiver", "--help"],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_pyproject_declares_console_script():
    """
    Input:  None
    Output: None
    Details:
        pyproject.toml must contain an oddarchiver console_scripts entry.
    """
    toml_text = (PROJECT_ROOT / "pyproject.toml").read_text()
    assert 'oddarchiver = "oddarchiver.cli:main"' in toml_text
