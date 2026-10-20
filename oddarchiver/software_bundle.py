"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Writes a plain, unencrypted copy of the oddarchiver software into
    a staging root as oddarchiver-software/. Used by init and sync when
    --embed-software is set. Never routes files through crypto.encrypt().
"""
# Imports
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend

logger = logging.getLogger(__name__)
_log = logger

BUNDLE_DIR_NAME = "oddarchiver-software"
VERSION_RELPATH = f"{BUNDLE_DIR_NAME}/VERSION"
README_NAME = "README.txt"
MINIMAL_EXCLUDE_FILES = frozenset({"usb_reauth.py"})
VENDOR_PACKAGES = ("argon2-cffi>=23.1.0", "cryptography>=42.0.0")
PYTHON_REQUIRES = "3.10"

_VERSION_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.M)

_IGNORE_DIR_NAMES = frozenset({
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    "dist",
    "build",
    "htmlcov",
    "graphify-out",
    "tmp",
    "node_modules",
})
_IGNORE_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd"})


# Functions


def package_dir() -> Path:
    """
    Input:  None
    Output: Path of the running oddarchiver package directory
    Details:
        Resolves from this file. Works for a git checkout and for an
        installed package.
    """
    return Path(__file__).resolve().parent


def source_root() -> Path:
    """
    Input:  None
    Output: Path of the source tree root, or the package directory
    Details:
        Walks one level up from the package. A directory that holds
        pyproject.toml naming oddarchiver is the repo root. An installed
        wheel has no sibling pyproject.toml, so the package directory
        is returned instead.
    """
    pkg = package_dir()
    parent = pkg.parent
    marker = parent / "pyproject.toml"
    if not marker.is_file():
        return pkg
    try:
        text = marker.read_text(encoding="utf-8")
    except OSError:
        _log.exception("Could not read %s", marker)
        return pkg
    if 'name = "oddarchiver"' in text or "name = 'oddarchiver'" in text:
        return parent
    return pkg


def current_version() -> str:
    """
    Input:  None
    Output: installed or pyproject oddarchiver version string
    Details:
        Prefers importlib.metadata. Falls back to pyproject.toml.
        Returns 0.0.0 if neither source works.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("oddarchiver")
        except PackageNotFoundError:
            logger.warning("Installed oddarchiver version not found; checking pyproject.toml")
    except Exception:
        _log.exception("Could not read installed oddarchiver version")
    pyproject = source_root() / "pyproject.toml"
    if not pyproject.is_file():
        pyproject = package_dir().parent / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        _log.exception("Could not read %s for version", pyproject)
        return "0.0.0"
    match = _VERSION_RE.search(text)
    if match:
        return match.group(1)
    return "0.0.0"


def parse_version_marker(data: bytes) -> tuple[str, str] | None:
    """
    Input:  data - raw VERSION file bytes
    Output: (version, mode) or None when the file is unusable
    Details:
        The marker is two lines: version, then mode (minimal or all).
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _log.warning("Ignoring unreadable software VERSION marker")
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        _log.warning("Ignoring malformed software VERSION marker")
        return None
    return lines[0], lines[1]


def read_disc_marker(backend: "BurnBackend") -> tuple[str, str] | None:
    """
    Input:  backend - BurnBackend to read oddarchiver-software/VERSION from
    Output: (version, mode) or None when the disc has no usable marker
    Details:
        Uses backend.read_paths. Missing paths are omitted, not raised.
        A decode or format problem is treated as absent.
    """
    try:
        found = backend.read_paths([VERSION_RELPATH])
    except Exception:
        _log.exception("Failed to read %s from backend", VERSION_RELPATH)
        raise
    data = found.get(VERSION_RELPATH)
    if not data:
        return None
    return parse_version_marker(data)


def _software_bundle_unchanged(
    backend: "BurnBackend", session_n: int, version: str, mode: str,
) -> bool:
    """
    Input:  backend - BurnBackend holding a prior VERSION marker
            session_n - session number being built
            version - current software version
            mode - requested bundle mode
    Output: bool - whether the disc already has this version and mode
    """
    if session_n > 0:
        existing = read_disc_marker(backend)
        if existing is not None:
            disc_version, disc_mode = existing
            if disc_version == version and disc_mode == mode:
                _log.info(
                    "Software bundle unchanged (version %s mode %s); not re-staging",
                    version, mode,
                )
                print(
                    f"  Software bundle unchanged (version {version}); not re-staging.",
                    flush=True,
                )
                return True
    return False


def stage_software_bundle(
    staging: Path,
    backend: "BurnBackend",
    mode: str,
    session_n: int,
) -> int:
    """
    Input:  staging   - staging root that will also hold session_NNN/
            backend   - BurnBackend used to read a prior VERSION marker
            mode      - "minimal" or "all"
            session_n - session number being built (0 on init)
    Output: int - bytes written into staging, or 0 when the write is skipped
    Details:
        Writes oddarchiver-software/ as plain files. On session_n > 0,
        skips the write when the disc already holds the same version and
        mode. Does not call crypto.encrypt.
    """
    if mode not in ("minimal", "all"):
        raise ValueError(f"Unknown embed-software mode: {mode!r}")

    version = current_version()
    if _software_bundle_unchanged(backend, session_n, version, mode):
        return 0

    dest = staging / BUNDLE_DIR_NAME
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    print(f"  Embedding oddarchiver software ({mode})...", flush=True)
    if mode == "minimal":
        _write_minimal(dest)
    else:
        _write_all(dest)

    (dest / "VERSION").write_text(f"{version}\n{mode}\n", encoding="utf-8")
    (dest / README_NAME).write_text(
        _readme_text(mode, version), encoding="utf-8",
    )

    size = _dir_bytes(dest)
    _log.info(
        "Staged software bundle: mode=%s version=%s bytes=%d",
        mode, version, size,
    )
    print(f"  Software bundle staged: {size} bytes.", flush=True)
    return size


def _dir_bytes(path: Path) -> int:
    """
    Input:  path - directory to measure
    Output: total bytes used on disk as reported by du -sb
    Details:
        Matches session._staging_bytes so the packing budget sees the
        same number the later space-check print uses.
    """
    try:
        proc = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        _log.exception("du -sb failed for %s", path)
        raise
    return int(proc.stdout.split()[0])


def _copy_minimal_package_file(src: Path, pkg: Path, out_pkg: Path) -> None:
    """
    Input:  src - package entry to copy
            pkg - source package directory
            out_pkg - destination package directory
    Output: None
    """
    if not src.is_file():
        return
    if src.name in MINIMAL_EXCLUDE_FILES:
        return
    if src.suffix in _IGNORE_SUFFIXES:
        return
    if any(part in _IGNORE_DIR_NAMES for part in src.relative_to(pkg).parts):
        return
    rel = src.relative_to(pkg)
    target = out_pkg / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)


def _write_minimal(dest: Path) -> None:
    """
    Input:  dest - oddarchiver-software/ directory to fill
    Output: None
    Details:
        Copies the running oddarchiver package minus usb_reauth.py and
        bytecode. Does not copy tests, docs, Assist, or vendor/.
    """
    pkg = package_dir()
    out_pkg = dest / "oddarchiver"
    out_pkg.mkdir()
    for src in pkg.rglob("*"):
        _copy_minimal_package_file(src, pkg, out_pkg)


def _copy_repository_file(root: Path, rel: Path, dest: Path) -> None:
    """
    Input:  root - repository source directory
            rel - path relative to the source directory
            dest - bundle destination directory
    Output: None
    """
    src = root / rel
    if not src.is_file():
        return
    if any(part in _IGNORE_DIR_NAMES for part in rel.parts):
        return
    if src.suffix in _IGNORE_SUFFIXES:
        return
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)


def _write_all(dest: Path) -> None:
    """
    Input:  dest - oddarchiver-software/ directory to fill
    Output: None
    Details:
        Copies the git-tracked source tree (and Assist/ when present),
        then vendors argon2-cffi and cryptography into vendor/.
        Raises RuntimeError when the source tree is not available.
    """
    root = source_root()
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError(
            "--embed-software all requires the oddarchiver source tree "
            "(pyproject.toml plus tests/ and docs/), not a bare installed package."
        )
    for rel in _repo_relative_files(root):
        _copy_repository_file(root, rel, dest)
    assist = root / "Assist"
    if assist.is_dir() and not (dest / "Assist").exists():
        _copy_tree_filtered(assist, dest / "Assist")
    _vendor_dependencies(dest / "vendor")


def _repo_relative_files(root: Path) -> list[Path]:
    """
    Input:  root - source tree root
    Output: relative Path list of files to copy
    Details:
        Prefers git ls-files so .venv and other untracked trees stay out.
        Walks the tree with the ignore set when git is missing.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        _log.exception("git ls-files failed; walking the source tree")
        return _walk_relative_files(root)
    rels: list[Path] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rels.append(Path(os.fsdecode(raw)))
    return rels


def _walk_relative_files(root: Path) -> list[Path]:
    """
    Input:  root - source tree root
    Output: relative Path list from a filtered os.walk
    Details:
        Fallback when git ls-files cannot run.
    """
    rels: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIR_NAMES]
        base = Path(dirpath)
        for name in filenames:
            src = base / name
            if src.suffix in _IGNORE_SUFFIXES:
                continue
            rels.append(src.relative_to(root))
    return rels


def _copy_filtered_tree_entry(item: Path, dest: Path) -> None:
    """
    Input:  item - source directory entry
            dest - destination directory
    Output: None
    """
    if item.name in _IGNORE_DIR_NAMES:
        return
    if item.is_dir():
        _copy_tree_filtered(item, dest / item.name)
    elif item.is_file():
        if item.suffix in _IGNORE_SUFFIXES:
            return
        shutil.copy2(item, dest / item.name)


def _copy_tree_filtered(src: Path, dest: Path) -> None:
    """
    Input:  src  - directory to copy
            dest - destination directory
    Output: None
    Details:
        Recursive copy that skips ignore directory names and bytecode.
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        entries = list(src.iterdir())
    except OSError:
        _log.exception("Could not list %s", src)
        raise
    for item in entries:
        _copy_filtered_tree_entry(item, dest)


def _vendor_dependencies(vendor_dir: Path) -> None:
    """
    Input:  vendor_dir - directory to fill with pip download output
    Output: None
    Details:
        Runs pip download for argon2-cffi and cryptography, including
        their dependencies. Needs network access to PyPI. Raises
        RuntimeError when pip download fails.
    """
    vendor_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "pip", "download",
        "--disable-pip-version-check",
        "-d", str(vendor_dir),
        *VENDOR_PACKAGES,
    ]
    print(
        "  Downloading argon2-cffi and cryptography into vendor/ ...",
        flush=True,
    )
    try:
        subprocess.run(cmd, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        _log.exception("pip download of embed-software vendor packages failed")
        raise RuntimeError(
            "Failed to download argon2-cffi and cryptography for "
            "--embed-software all. Network access to PyPI is required."
        ) from exc


def _readme_text(mode: str, version: str) -> str:
    """
    Input:  mode    - "minimal" or "all"
            version - oddarchiver version written into VERSION
    Output: plaintext README body
    Details:
        Names the Python version, the mode, and the install command
        for argon2-cffi and cryptography.
    """
    py = f"Python {PYTHON_REQUIRES} or later"
    header = (
        f"ODDArchiver restore software ({mode})\n"
        f"\n"
        f"This directory is a plain copy of the oddarchiver tool.\n"
        f"Use it to restore or verify this disc. The files are not encrypted.\n"
        f"\n"
        f"Need {py}. This copy is oddarchiver version {version}.\n"
        f"\n"
        f"The oddarchiver package imports argon2-cffi and cryptography when it\n"
        f"starts. Install those libraries before you run restore or verify.\n"
        f"\n"
    )
    if mode == "all":
        deps = (
            "Install the crypto libraries from vendor/ with no network:\n"
            "\n"
            "  pip install --no-index --find-links=vendor/ argon2-cffi cryptography\n"
            "\n"
        )
    else:
        deps = (
            "If this archive is encrypted, or if import fails, install:\n"
            "\n"
            "  pip install oddarchiver[encrypt]\n"
            "\n"
            "You can also install the argon2-cffi and cryptography system packages.\n"
            "\n"
        )
    how = (
        "How to restore this disc:\n"
        "\n"
        "1. Copy the oddarchiver-software directory to a working machine.\n"
        "2. Set PYTHONPATH to that directory (the parent of the oddarchiver\n"
        "   package).\n"
        "3. Run:\n"
        "\n"
        "  python3 -m oddarchiver restore DEST --device /dev/sr0\n"
        "\n"
        "   Replace DEST with the restore destination.\n"
        "   For an ISO file, pass --test-iso PATH instead of --device.\n"
        "\n"
        "How to verify this disc:\n"
        "\n"
        "  python3 -m oddarchiver verify --device /dev/sr0\n"
    )
    return header + deps + how


if __name__ == "__main__":
    pass
