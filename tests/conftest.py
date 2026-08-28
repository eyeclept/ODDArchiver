"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Session-wide test fixtures.
"""
# Imports
import pytest

# Fixtures


@pytest.fixture(autouse=True)
def _isolated_staging_root(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch - pytest built-in fixtures
    Output: None
    Details:
        Forces oddarchiver.session._default_staging_root() to resolve under a
        fresh per-test tmp_path by overriding XDG_RUNTIME_DIR, for every test
        in the suite. Without this, any code path that reaches build_staging()
        without an explicit _staging_root override -- notably cli.py's
        _run_init/_run_sync, exercised directly (not mocked) by
        tests/test_cli_e2e.py -- falls back to the real
        $XDG_RUNTIME_DIR/oddarchiver location also used by production
        oddarchiver processes on this machine. A test run landing there while
        a real oddarchiver burn is staging into the same deterministically
        named session directory silently deletes the real build's in-flight
        files mid-burn; see Assist/HANDOVER_disc_sync_bug.md for the incident
        this fixture exists to prevent from recurring.
        Individual tests that need to exercise _default_staging_root() itself
        (e.g. tests/test_security.py) still set XDG_RUNTIME_DIR explicitly via
        their own monkeypatch call; that later setenv simply overrides this
        fixture's value for the remainder of that test, and monkeypatch
        restores both in the correct order on teardown.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg_runtime"))
