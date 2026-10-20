"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for partial/failed-write detection: BurnInterrupted, the
    last-known-good ledger, --recover-scan, and INCOMPLETE trailing
    sessions.
"""
# Imports
import argparse
import json
import logging
from pathlib import Path

import pytest

from oddarchiver.cli import (
    _check_loaded_manifests,
    _read_disc_manifests,
    build_parser,
    dispatch,
)
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import BurnInterrupted, DiscBackend, DiscInfo, ISOBackend
from oddarchiver.ledger import check_session_coverage, ledger_path, load_ledger, record_confirmed
from oddarchiver.verify import _read_all_manifests, detect_incomplete_session, verify

# Globals
logger = logging.getLogger(__name__)
SMALL_DISC = 50 * 2**20
_crypto = NullCrypto()


# Functions


class _FakePopen:
    """Fake subprocess.Popen: yields canned stdout lines, returns a fixed exit code."""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self):
        return self._returncode


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input:  kwargs - field overrides for the Namespace
    Output: argparse.Namespace with defaults for test commands
    """
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "TEST",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "50mb",
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
        "recover_scan": False,
        "mirror": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _init_and_sync_two_sessions(tmp_path: Path) -> tuple[Path, Path]:
    """
    Input:  tmp_path - pytest temp directory
    Output: (source, iso) paths for a two-session TEST archive
    Details:
        Burns session 0 then session 1 via the real CLI dispatch path
        so the ledger is written after each post-burn verify.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"version 1")
    iso = tmp_path / "test.iso"
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))
    assert rc == 0
    (source / "a.txt").write_bytes(b"version 2 - different content")
    rc = dispatch(_make_args(command="sync", source=str(source), test_iso=str(iso)))
    assert rc == 0
    return source, iso


def _torn_session_000(_backend: ISOBackend):
    """
    Input:  _backend - unused; kept so call sites can pass the instance
    Output: class-level read_paths wrapper
    Details:
        load_session_manifests() now fetches every session's manifest
        candidates in one batched read_paths() call, so a torn read
        must be simulated at that level: omits session_000/manifest.*
        from the returned dict rather than raising from a per-file
        read_path() call, which the manifest loader no longer makes.
    """
    original = ISOBackend.read_paths

    def wrapped(self, paths):
        found = original(self, paths)
        found.pop("session_000/manifest.enc", None)
        found.pop("session_000/manifest.json", None)
        return found

    return wrapped


def test_torn_read_below_known_good_raises(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Live-incident shape: session_000/manifest.enc raises FileNotFoundError
        while the ledger records confirmed_sessions=2. The loader may return
        an empty list; the coverage check after that list must raise instead
        of treating the disc as empty.
    """
    _, iso = _init_and_sync_two_sessions(tmp_path)
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    monkeypatch.setattr(ISOBackend, "read_paths", _torn_session_000(backend))

    ledger = load_ledger("TEST", str(iso))
    assert ledger is not None
    assert ledger.confirmed_sessions == 2

    manifests = _read_disc_manifests(backend, crypto=_crypto)
    with pytest.raises(RuntimeError, match="Read failure"):
        check_session_coverage(
            manifests,
            label="TEST",
            backend_id=str(iso),
            disc_session_count=backend.mediainfo().session_count,
            backend=backend,
        )

    with pytest.raises(RuntimeError, match="Read failure"):
        _check_loaded_manifests(
            manifests,
            backend=backend,
            args=_make_args(command="sync", test_iso=str(iso), label="TEST"),
            disc_session_count=backend.mediainfo().session_count,
            command="sync",
        )

    tmp = tmp_path / "verify_tmp"
    tmp.mkdir()
    loaded = _read_all_manifests(backend, tmp, crypto=_crypto)
    with pytest.raises(RuntimeError, match="Read failure"):
        check_session_coverage(
            list(loaded.values()),
            label="TEST",
            backend_id=str(iso),
            disc_session_count=backend.mediainfo().session_count,
            backend=backend,
        )


def test_probe_above_known_good_does_not_raise(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A read failure for session 2 when confirmed_sessions=2 is a
        legitimate end-of-scan and must not raise.
    """
    _, iso = _init_and_sync_two_sessions(tmp_path)
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    original = ISOBackend.read_paths

    def wrapped(self, paths):
        found = original(self, paths)
        for key in list(found):  # scbc ignore[unnecessary-list-call] keys are removed during iteration
            if key.startswith("session_002/"):
                found.pop(key, None)
        return found

    monkeypatch.setattr(ISOBackend, "read_paths", wrapped)
    manifests = _read_disc_manifests(backend, crypto=_crypto)
    assert len(manifests) == 2
    check_session_coverage(
        manifests,
        label="TEST",
        backend_id=str(iso),
        disc_session_count=backend.mediainfo().session_count,
        backend=backend,
    )


def test_append_raises_burn_interrupted_signal_prior_readable(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        A signal-terminated growisofs process must raise BurnInterrupted,
        not a plain RuntimeError. When prior sessions re-verify, the
        message says they are readable.
    """
    staging = tmp_path / "staging"
    (staging / "session_001").mkdir(parents=True)
    monkeypatch.setattr(
        DiscBackend, "mediainfo",
        lambda self, retries=6, retry_delay=5.0: DiscInfo(
            session_count=1, remaining_bytes=10**9, used_bytes=0, label="TEST",
        ),
    )
    monkeypatch.setattr(
        "oddarchiver.disc.archive_session_count", lambda backend: 1,
    )
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(["killed by signal\n"], returncode=-15),
    )

    def fake_read(self, path, retries=6, retry_delay=5.0):
        if path.startswith("session_000/"):
            return b"{}"
        raise FileNotFoundError(path)

    monkeypatch.setattr(DiscBackend, "read_path", fake_read)
    backend = DiscBackend("/dev/sr0")
    with pytest.raises(BurnInterrupted) as exc_info:
        backend.append(staging, "TEST", 1)
    message = str(exc_info.value)
    assert "burn for session 001 failed (exit -15)" in message
    assert "re-verified readable" in message
    assert type(exc_info.value) is BurnInterrupted  # scbc ignore[type-equality] exact type excludes subclasses


def test_append_raises_burn_interrupted_signal_prior_unreadable(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        When prior sessions cannot be re-verified, BurnInterrupted must
        tell the caller not to retry blindly.
    """
    staging = tmp_path / "staging"
    (staging / "session_001").mkdir(parents=True)
    monkeypatch.setattr(
        DiscBackend, "mediainfo",
        lambda self, retries=6, retry_delay=5.0: DiscInfo(
            session_count=1, remaining_bytes=10**9, used_bytes=0, label="TEST",
        ),
    )
    monkeypatch.setattr(
        "oddarchiver.disc.archive_session_count", lambda backend: 1,
    )
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(["killed by signal\n"], returncode=-9),
    )

    def fake_read(self, path, retries=6, retry_delay=5.0):
        raise RuntimeError("retries exhausted")

    monkeypatch.setattr(DiscBackend, "read_path", fake_read)
    backend = DiscBackend("/dev/sr0")
    with pytest.raises(BurnInterrupted) as exc_info:
        backend.append(staging, "TEST", 1)
    message = str(exc_info.value)
    assert "could NOT be re-verified" in message
    assert "verify --level fast" in message


def test_append_hardware_fault_is_burn_interrupted(monkeypatch, tmp_path):
    """
    Input:  monkeypatch, tmp_path
    Output: None
    Details:
        A non-zero growisofs exit whose output matches a SCSI Unit
        Attention / bus-reset signature is BurnInterrupted. A plain
        non-zero exit without that signature stays RuntimeError.
    """
    staging = tmp_path / "staging"
    (staging / "session_001").mkdir(parents=True)
    monkeypatch.setattr(
        DiscBackend, "mediainfo",
        lambda self, retries=6, retry_delay=5.0: DiscInfo(
            session_count=1, remaining_bytes=10**9, used_bytes=0, label="TEST",
        ),
    )
    monkeypatch.setattr(
        "oddarchiver.disc.archive_session_count", lambda backend: 1,
    )
    monkeypatch.setattr(DiscBackend, "read_path", lambda self, path, **k: b"{}")
    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(
            ["Power on, reset, or bus device reset / Unit Attention\n"],
            returncode=1,
        ),
    )
    backend = DiscBackend("/dev/sr0")
    with pytest.raises(BurnInterrupted):
        backend.append(staging, "TEST", 1)

    monkeypatch.setattr(
        "oddarchiver.disc.subprocess.Popen",
        lambda cmd, **kwargs: _FakePopen(["FATAL: unable to open device.\n"], returncode=1),
    )
    with pytest.raises(RuntimeError) as exc_info:
        backend.append(staging, "TEST", 1)
    assert type(exc_info.value) is RuntimeError  # scbc ignore[type-equality] exact type excludes BurnInterrupted


def test_ledger_round_trip_missing_is_cautious(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Ledger write/read round-trips. A missing ledger on an initialized
        disc with zero readable manifests is a loud failure, not a
        permissive empty-archive state.
    """
    record_confirmed("TEST", "TEST", 2)
    loaded = load_ledger("TEST", "TEST")
    assert loaded is not None
    assert loaded.confirmed_sessions == 2
    assert loaded.last_verified
    path = ledger_path("TEST", "TEST")
    assert path.is_file()
    assert path.parent == tmp_path / "xdg_state" / "oddarchiver"
    raw = json.loads(path.read_text())
    assert raw["confirmed_sessions"] == 2

    path.unlink()
    assert load_ledger("TEST", "TEST") is None
    with pytest.raises(RuntimeError, match="Read failure"):
        check_session_coverage(
            [],
            label="TEST",
            backend_id="TEST",
            disc_session_count=2,
        )


def test_no_ledger_zero_readable_does_not_invent_session_count(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        With no ledger, zero readable manifests, and MMC session_count
        of 1 (the BD-R SRM+POW value), the error must not claim exactly
        one missing session or list session 000 as the complete missing
        set. The true archive session count is unknown in this case.
    """
    with pytest.raises(RuntimeError) as exc_info:
        check_session_coverage(
            [],
            label="TEST",
            backend_id="TEST",
            disc_session_count=1,
        )
    message = str(exc_info.value)
    assert "Read failure" in message
    assert "initialized" in message.lower()
    assert "could not be determined" in message
    assert "1 session(s) (000-000) should exist" not in message
    assert "Read failure for session 000" not in message


def test_no_ledger_zero_readable_recover_scan_warns(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        --recover-scan downgrades the no-ledger zero-readable case to a
        WARNING and returns without raising. The warning must not claim
        a fabricated expected count of 1 or list session 000 as the
        complete missing set.
    """
    caplog.set_level(logging.WARNING)
    check_session_coverage(
        [],
        label="TEST",
        backend_id="TEST",
        disc_session_count=1,
        recover_scan=True,
    )
    text = caplog.text
    assert "recover-scan" in text
    assert "unknown" in text.lower()
    assert "expected 1 session(s)" not in text
    assert "unconfirmed sessions: 000" not in text


def test_ledger_present_shortfall_names_sessions_3_and_4(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        With confirmed_sessions=5 and 3 readable manifests, the missing
        set is still sessions 3 and 4. MMC session_count=1 must not
        change that ledger-present path.
    """
    record_confirmed("TEST", "TEST", 5)
    with pytest.raises(RuntimeError) as exc_info:
        check_session_coverage(
            [object(), object(), object()],
            label="TEST",
            backend_id="TEST",
            disc_session_count=1,
        )
    message = str(exc_info.value)
    assert "Read failure for session 003" in message
    assert "5 session(s) (000-004) should exist" in message
    assert "Last readable session: 002" in message
    assert "Read failure for session 000" not in message


def test_recover_scan_proceeds_and_warns(tmp_path, monkeypatch, caplog):
    """
    Input:  tmp_path, monkeypatch, caplog
    Output: None
    Details:
        sync --recover-scan proceeds past a ledger mismatch and logs a
        WARNING naming the unconfirmed sessions.
    """
    source, iso = _init_and_sync_two_sessions(tmp_path)
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    monkeypatch.setattr(ISOBackend, "read_paths", _torn_session_000(backend))
    caplog.set_level(logging.WARNING)

    called = []

    def fake_staging(**kwargs):
        called.append(True)
        raise RuntimeError("stop after coverage check")

    monkeypatch.setattr("oddarchiver.session.build_staging", fake_staging)
    args = _make_args(
        command="sync",
        source=str(source),
        test_iso=str(iso),
        recover_scan=True,
        label="TEST",
    )
    rc = dispatch(args)
    assert called
    assert rc == 1
    text = caplog.text
    assert "recover-scan" in text
    assert "000" in text and "001" in text


def _assert_incomplete_output(out: str) -> None:
    """
    Input:  out - fast verification output
    Output: None
    """
    assert "Session 000" in out and "OK" in out
    assert "Session 001" in out and "OK" in out
    assert "INCOMPLETE (interrupted burn, not corruption)" in out
    for line in out.splitlines():
        if line.startswith("Session 002"):
            assert "FAIL" not in line
            assert "INCOMPLETE" in line


def test_verify_fast_reports_trailing_incomplete_not_fail(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        A truncated trailing manifest reports INCOMPLETE, not FAIL.
        Prior complete sessions still report OK.
    """
    _, iso = _init_and_sync_two_sessions(tmp_path)
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    sess2 = backend._sessions_root / "session_002"
    sess2.mkdir()
    (sess2 / "manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        verify(backend, _crypto, level="fast")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    _assert_incomplete_output(out)
    tmp = tmp_path / "vm"
    tmp.mkdir()
    loaded = _read_all_manifests(backend, tmp, crypto=_crypto)
    assert detect_incomplete_session(backend, loaded) == 2


def test_sync_refuses_from_scratch_diff_after_torn_read(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        After two confirmed burns, a torn read of session_000 must make
        sync exit nonzero naming the mismatch, without writing a third
        session.
    """
    source, iso = _init_and_sync_two_sessions(tmp_path)
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    assert backend.mediainfo().session_count == 2
    monkeypatch.setattr(ISOBackend, "read_paths", _torn_session_000(backend))

    rc = dispatch(_make_args(
        command="sync",
        source=str(source),
        test_iso=str(iso),
        label="TEST",
    ))
    captured = capsys.readouterr()
    assert rc != 0
    assert "Read failure" in captured.err or "error:" in captured.err
    assert ISOBackend(iso, disc_size=SMALL_DISC).mediainfo().session_count == 2


def test_recover_scan_flag_on_init_sync_status():
    """
    Input:  None
    Output: None
    Details:
        --recover-scan is accepted on init, sync, and status, default off.
    """
    parser = build_parser()
    init_args = parser.parse_args(["init", "/src", "--recover-scan"])
    assert init_args.recover_scan is True  # scbc ignore[is-true-false-strict] argparse Namespace value is unannotated
    sync_args = parser.parse_args(["sync", "/src"])
    assert sync_args.recover_scan is False  # scbc ignore[is-true-false-strict] argparse Namespace value is unannotated
    status_args = parser.parse_args(["status", "--recover-scan"])
    assert status_args.recover_scan is True  # scbc ignore[is-true-false-strict] argparse Namespace value is unannotated


def test_status_surfaces_incomplete_trailing_session(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        status must name an incomplete trailing session and tell the
        user not to eject.
    """
    _, iso = _init_and_sync_two_sessions(tmp_path)
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    sess2 = backend._sessions_root / "session_002"
    sess2.mkdir()
    (sess2 / "manifest.json").write_text("{not json", encoding="utf-8")
    rc = dispatch(_make_args(command="status", test_iso=str(iso), label="TEST"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "INCOMPLETE" in out
    assert "Do not eject" in out
    assert "verify --level checksum" in out
