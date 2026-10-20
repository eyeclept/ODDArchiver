"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for --embed-software. Covers minimal and all modes, the
    version-marker skip on sync, the version-bump re-embed, the
    crypto.encrypt exclusion, and the default-off regression.
"""
# Imports
import argparse
import logging
import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.cli import build_parser, dispatch
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import DiscInfo, ISOBackend
from oddarchiver.manifest import (
    build_disc_origin,
    build_disc_state,
    load_session_manifests,
    validate_disc_read_path,
)
from oddarchiver.session import build_staging
import oddarchiver.software_bundle as software_bundle

# Globals
DISC = 200 * 2**20  # 200 MiB. Room for the bundle plus a small archive.
logger = logging.getLogger(__name__)


# Functions

@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Points CLI CacheManager at a per-test directory so these tests
        do not share ~/.cache/oddarchiver with other processes.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        "oddarchiver.cli.CacheManager",
        lambda *a, **k: CacheManager(cache_dir),
    )


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides
    Output: argparse.Namespace with test-safe defaults
    """
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "BUNDLE",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "200mb",
        "prefill": None,
        "session": None,
        "force": True,
        "level": "checksum",
        "slow": False,
        "embed_software": None,
        "recover_scan": False,
        "mirror": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _bundle_dir(iso: Path) -> Path:
    """
    Input:  iso - ISOBackend iso_path
    Output: Path of oddarchiver-software/ in the sessions root
    """
    return iso.with_suffix(".d") / software_bundle.BUNDLE_DIR_NAME


def _make_backend(remaining_bytes: int = DISC) -> MagicMock:
    """
    Input:  remaining_bytes - mediainfo remaining capacity
    Output: mock BurnBackend
    """
    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=1,
        remaining_bytes=remaining_bytes,
        used_bytes=0,
        label="TEST",
    )
    backend.read_paths.return_value = {}
    return backend


def _make_cache() -> MagicMock:
    """
    Input:  None
    Output: mock CacheManager
    """
    cache = MagicMock()
    cache.get_with_fallback.return_value = b""
    return cache


class _SpyCrypto(NullCrypto):
    """NullCrypto that records encrypt() payloads and encrypt_file() sources."""

    def __init__(self) -> None:
        self.encrypt_args: list[bytes] = []
        self.encrypt_file_srcs: list[Path] = []

    def encrypt(self, plaintext: bytes) -> bytes:
        self.encrypt_args.append(plaintext)
        return super().encrypt(plaintext)

    def encrypt_file(
        self, src: Path, dest: Path, offset: int = 0, length: int | None = None,
        digest=None,
    ) -> None:
        self.encrypt_file_srcs.append(Path(src))
        return super().encrypt_file(src, dest, offset, length, digest=digest)


# Tests


def test_embed_software_flag_on_init_and_sync_only():
    """
    Input:  None
    Output: None
    Details:
        --embed-software is accepted on init and sync, defaults to None,
        and is rejected on restore and verify.
    """
    parser = build_parser()
    assert parser.parse_args(["init", "/src"]).embed_software is None
    assert parser.parse_args(
        ["init", "/src", "--embed-software", "minimal"]
    ).embed_software == "minimal"
    assert parser.parse_args(
        ["sync", "/src", "--embed-software", "all"]
    ).embed_software == "all"
    with pytest.raises(SystemExit):
        parser.parse_args(["verify", "--embed-software", "minimal"])
    with pytest.raises(SystemExit):
        parser.parse_args(["restore", "/dest", "--embed-software", "all"])


def test_validate_disc_read_path_allows_software_version():
    """
    Input:  None
    Output: None
    Details:
        oddarchiver-software/VERSION must be a legal disc-read path so
        a later sync can compare the on-disc marker.
    """
    assert (
        validate_disc_read_path("oddarchiver-software/VERSION")
        == "oddarchiver-software/VERSION"
    )


def _assert_minimal_bundle_contents(bundle: Path) -> None:
    """
    Input:  bundle - software directory in the ISO tree
    Output: None
    """
    assert bundle.is_dir()
    restore_src = (bundle / "oddarchiver" / "restore.py").read_text(encoding="utf-8")
    assert "def restore" in restore_src
    verify_src = (bundle / "oddarchiver" / "verify.py").read_text(encoding="utf-8")
    assert "def verify" in verify_src
    assert not (bundle / "oddarchiver" / "usb_reauth.py").exists()
    assert not (bundle / "tests").exists()
    assert not (bundle / "docs").exists()
    assert not (bundle / "Assist").exists()
    assert not (bundle / "vendor").exists()


def test_init_embed_software_minimal(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        init --embed-software minimal writes a root-level plaintext
        directory. A plain file read opens the package source. usb_reauth.py,
        tests, docs, Assist, and vendor/ are absent.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello bundle")

    iso = tmp_path / "test.iso"
    args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        embed_software="minimal",
    )
    assert dispatch(args) == 0

    bundle = _bundle_dir(iso)
    _assert_minimal_bundle_contents(bundle)
    readme = (bundle / "README.txt").read_text(encoding="utf-8")
    assert "Python 3.10" in readme
    assert "pip install oddarchiver[encrypt]" in readme
    marker = (bundle / "VERSION").read_text(encoding="utf-8")
    assert "minimal" in marker
    backend = ISOBackend(iso, disc_size=DISC)
    assert backend.read_path("oddarchiver-software/VERSION") == marker.encode("utf-8")


def _assert_full_bundle_contents(bundle: Path) -> Path:
    """
    Input:  bundle - software directory in the ISO tree
    Output: Vendor directory in the bundle
    """
    assert (bundle / "docs").is_dir()
    assert (bundle / "pyproject.toml").is_file()
    readme = (bundle / "README.txt").read_text(encoding="utf-8")
    assert "pip install --no-index --find-links=vendor/ argon2-cffi cryptography" in readme
    vendor = bundle / "vendor"
    assert vendor.is_dir()
    names = [p.name.lower() for p in vendor.iterdir()]
    assert any("argon2" in n for n in names)
    assert any("cryptography" in n for n in names)
    return vendor


def test_init_embed_software_all_vendors_and_installs(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        init --embed-software all writes the full repo tree plus vendor/.
        pip install --no-index --find-links=vendor/ argon2-cffi cryptography
        against a throwaway venv must succeed.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello all-mode")

    iso = tmp_path / "test.iso"
    args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        embed_software="all",
        disc_size="500mb",
    )
    rc = dispatch(args)
    assert rc == 0

    bundle = _bundle_dir(iso)
    assert bundle.is_dir()
    assert (bundle / "oddarchiver" / "restore.py").is_file()
    assert (bundle / "oddarchiver" / "usb_reauth.py").is_file()
    assert (bundle / "tests").is_dir()
    vendor = _assert_full_bundle_contents(bundle)

    venv_dir = tmp_path / "vendor-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
    )
    pip = venv_dir / "bin" / "pip"
    subprocess.run(
        [
            str(pip), "install", "--no-index",
            f"--find-links={vendor}",
            "argon2-cffi", "cryptography",
        ],
        check=True,
    )
    py = venv_dir / "bin" / "python"
    subprocess.run(
        [str(py), "-c", "import argon2, cryptography"],
        check=True,
    )


def test_sync_unchanged_version_stages_zero_bundle_bytes(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A sync with the same installed version and the same mode stages
        zero new bytes for the software bundle.
    """
    recorded: list[int] = []
    real = software_bundle.stage_software_bundle

    def wrapper(staging, backend, mode, session_n):
        n = real(staging, backend, mode, session_n)
        recorded.append(n)
        return n

    monkeypatch.setattr(software_bundle, "stage_software_bundle", wrapper)

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"stable")
    iso = tmp_path / "test.iso"
    init_args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        embed_software="minimal",
    )
    assert dispatch(init_args) == 0
    assert recorded and recorded[0] > 0

    (source / "a.txt").write_bytes(b"changed")
    sync_args = _make_args(
        command="sync",
        source=str(source),
        test_iso=str(iso),
        embed_software="minimal",
    )
    assert dispatch(sync_args) == 0
    assert len(recorded) >= 2
    assert recorded[1] == 0

    backend = ISOBackend(iso, disc_size=DISC)
    manifests = load_session_manifests(backend, NullCrypto())
    disc_state = build_disc_state(manifests)
    disc_origin = build_disc_origin(manifests)
    (source / "a.txt").write_bytes(b"changed again")
    chunks = build_staging(
        session_n=len(manifests),
        source=source,
        disc_state=disc_state,
        disc_origin=disc_origin,
        backend=backend,
        cache=_make_cache(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
        embed_software="minimal",
    )
    staging = chunks[0][0]
    try:
        assert not (staging / software_bundle.BUNDLE_DIR_NAME).exists()
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_sync_version_bump_reembeds_and_updates_marker(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A sync after a version bump re-embeds the payload and updates
        the VERSION marker.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"v1")
    iso = tmp_path / "test.iso"
    init_args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
        embed_software="minimal",
    )
    assert dispatch(init_args) == 0
    old_marker = (_bundle_dir(iso) / "VERSION").read_text(encoding="utf-8")
    assert "minimal" in old_marker

    monkeypatch.setattr(software_bundle, "current_version", lambda: "9.9.9")
    (source / "a.txt").write_bytes(b"v2")
    sync_args = _make_args(
        command="sync",
        source=str(source),
        test_iso=str(iso),
        embed_software="minimal",
    )
    assert dispatch(sync_args) == 0
    new_marker = (_bundle_dir(iso) / "VERSION").read_text(encoding="utf-8")
    assert new_marker.startswith("9.9.9")
    assert "minimal" in new_marker
    assert new_marker != old_marker
    assert (_bundle_dir(iso) / "oddarchiver" / "restore.py").is_file()


def test_bundle_bytes_never_passed_to_crypto_encrypt(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        crypto.encrypt() is never called on any software-bundle byte.
        encrypt_file() is never pointed at a bundle path.
    """
    source = tmp_path / "src"
    source.mkdir()
    payload = b"unique-archive-bytes-not-in-the-bundle"
    (source / "data.txt").write_bytes(payload)

    spy = _SpyCrypto()
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=_make_backend(),
        cache=_make_cache(),
        crypto=spy,
        _staging_root=tmp_path / "staging",
        embed_software="minimal",
    )
    staging = chunks[0][0]
    try:
        bundle = staging / software_bundle.BUNDLE_DIR_NAME
        assert bundle.is_dir()
        bundle_payloads = {
            path.read_bytes()
            for path in bundle.rglob("*")
            if path.is_file()
        }
        for arg in spy.encrypt_args:
            assert arg not in bundle_payloads
        assert payload in spy.encrypt_args
        bundle_resolved = bundle.resolve()
        for src in spy.encrypt_file_srcs:
            assert not src.resolve().is_relative_to(bundle_resolved)
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)


def test_disc_without_flag_verify_restore_sync_unaffected(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A disc burned without --embed-software has no software directory
        and still verifies, restores, and syncs.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"no bundle")
    iso = tmp_path / "test.iso"
    init_args = _make_args(
        command="init",
        source=str(source),
        test_iso=str(iso),
    )
    assert dispatch(init_args) == 0
    assert not _bundle_dir(iso).exists()

    verify_args = _make_args(
        command="verify",
        test_iso=str(iso),
        level="checksum",
    )
    assert dispatch(verify_args) == 0

    dest = tmp_path / "dest"
    restore_args = _make_args(
        command="restore",
        dest=str(dest),
        test_iso=str(iso),
        force=True,
    )
    assert dispatch(restore_args) == 0
    assert (dest / "a.txt").read_bytes() == b"no bundle"

    (source / "a.txt").write_bytes(b"after sync")
    sync_args = _make_args(
        command="sync",
        source=str(source),
        test_iso=str(iso),
    )
    assert dispatch(sync_args) == 0
    assert not _bundle_dir(iso).exists()
    backend = ISOBackend(iso, disc_size=DISC)
    assert backend.mediainfo().session_count == 2
