"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Session-wide test fixtures.
"""
# Imports
import logging
import pytest

# Globals
logger = logging.getLogger(__name__)

# Fixtures


@pytest.fixture(autouse=True)
def _uncompressed_blobs(monkeypatch):
    """Store raw blobs by default so packing tests keep fixed byte sizes."""
    import oddarchiver.compress as compress_mod
    if "compression_on" in _COMPRESS_ON:
        return
    monkeypatch.setattr(compress_mod, "DEFAULT_CODEC", compress_mod.CODEC_NONE)


_COMPRESS_ON: set[str] = set()


@pytest.fixture
def compression_on(monkeypatch):
    """Enable LZMA for a test that exercises compression."""
    import oddarchiver.compress as compress_mod
    _COMPRESS_ON.add("compression_on")
    monkeypatch.setattr(compress_mod, "DEFAULT_CODEC", compress_mod.CODEC_LZMA)
    yield
    _COMPRESS_ON.discard("compression_on")


@pytest.fixture(autouse=True)
def _isolated_staging_root(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch - pytest built-in fixtures
    Output: None
    Details:
        Give each test private HOME, runtime, cache, and state directories.
        A test can override these values with its own monkeypatch.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg_runtime"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg_cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_state"))


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """
    Input:  None
    Output: None
    Details:
        Remove and close root handlers a test adds, and restore the root level.
        A leaked stderr handler would write to a closed capture stream in later tests.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for handler in root.handlers[:]:
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(saved_level)
