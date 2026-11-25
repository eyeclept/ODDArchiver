"""
Author: Richard Baldwin
Date:   2026

Description:
    Regression tests for user-visible bugs found on real DVDs:
    archive-keyed ledger, blank-disc init, acquisition prompt reasons,
    empty-drive restore, wrong-key messages, needed-disc naming, scan
    skips, overwrite warnings, and read-only destination causes.
"""
# Imports
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from oddarchiver.catalog import (
    archive_ids_with_label, remember_encryption_mode, remember_inserted_disc,
)
from oddarchiver.cli import (
    _check_init_ledger, _scan_skips, _scan_source_with_progress, build_parser,
    dispatch,
)
from oddarchiver.crypto import NullCrypto, generate_keyfile
from oddarchiver.disc import ISOBackend
from oddarchiver.ledger import (
    ledger_path, load_ledger, record_confirmed,
)
from oddarchiver.manifest import Manifest, ManifestEntry, load_session_manifests, write_manifest
from oddarchiver.restore import restore, run_session_replay
from oddarchiver.restore_plan import DiscAcquisition, _needed_name
from oddarchiver.volume import VolumeMarker, read_volume_marker, write_volume_marker
from tests.helpers import _entry, fill

# Globals
logger = logging.getLogger(__name__)
_DISC = 100_000
_CRYPTO = NullCrypto()

# Functions


class _Source:
    """Local blob root standing in for a session source."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def open_session(self, session_n: int) -> Path:
        return self.root


def _isolate(tmp_path: Path, monkeypatch) -> None:
    """Point state and cache directories inside the test directory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def test_ledger_keyed_by_archive_id_not_label(tmp_path, monkeypatch):
    """
    Input:  two archives that share a label, different archive ids
    Output: each archive keeps its own confirmed count
    Details:
        A lower count from archive A must not hide a torn session on
        archive B. The ledger file name keys on a hash of archive_id.
    """
    _isolate(tmp_path, monkeypatch)
    record_confirmed(
        "SHARED", "dev", 3, archive_id="archive-a", volume_index=0,
    )
    record_confirmed(
        "SHARED", "dev", 5, archive_id="archive-b", volume_index=0,
    )
    a = load_ledger("SHARED", "dev", volume_index=0, archive_id="archive-a")
    b = load_ledger("SHARED", "dev", volume_index=0, archive_id="archive-b")
    assert a is not None and a.confirmed_sessions == 3
    assert b is not None and b.confirmed_sessions == 5
    # The lower write from A must not change B's stored row.
    record_confirmed(
        "SHARED", "dev", 1, archive_id="archive-a", volume_index=0,
    )
    b2 = load_ledger("SHARED", "dev", volume_index=0, archive_id="archive-b")
    assert b2 is not None and b2.confirmed_sessions == 5
    assert ledger_path("SHARED", "dev", 0, "archive-a") != ledger_path(
        "SHARED", "dev", 0, "archive-b",
    )


def test_blank_disc_init_with_ledger_label_succeeds(tmp_path, monkeypatch, capsys):
    """
    Input:  a legacy ledger row for label TEST and a blank disc
    Output: init exit 0; no ledger refusal
    Details:
        A new archive id has no ledger entry. A blank disc that only
        shares a label with a past archive must init without
        --recover-scan.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"alpha")
    iso = tmp_path / "new.iso"
    backend = ISOBackend(iso, disc_size=_DISC)
    record_confirmed("TEST", str(iso), 2)
    ledger = load_ledger("TEST", str(iso))
    assert ledger is not None
    assert ledger.confirmed_sessions == 2
    assert archive_ids_with_label("TEST") == []
    args = argparse.Namespace(recover_scan=False)
    _check_init_ledger(args, backend, "TEST")
    capsys.readouterr()
    rc = dispatch(build_parser().parse_args([
        "init", str(source), "--test-iso", str(iso), "--label", "TEST",
        "--disc-size", str(_DISC),
    ]))
    captured = capsys.readouterr()
    assert rc == 0
    combined = captured.out + captured.err
    assert "ledger records" not in combined
    assert "recover-scan" not in combined


def test_acquisition_names_reason_for_each_reprompt(tmp_path, monkeypatch, capsys):
    """
    Input:  an unreadable disc, a foreign disc, and a disc outside the plan
    Output: one reason line per refused disc
    Details:
        The prompt must not silently reappear. Each refusal prints one
        line that says why the disc was not used, and a disc in the
        plan is still accepted afterwards. One check refuses a disc whose
        index is not in needed; a disc already in the catalog gets the
        "already read or not in this restore plan" wording. The source
        catalog disc is exempt here; the restore loop refuses it in
        acquire_next.
    """
    _isolate(tmp_path, monkeypatch)
    from oddarchiver.volume import VolumeMarker, write_volume_marker
    from tests.test_restore_plan import _stage, _source, _CRYPTO, _ID, _DISC

    from tests.helpers import _make_staging

    discs = [
        _stage(tmp_path, 0, {"a.txt": b"A"}, _CRYPTO)[0],
        _stage(tmp_path, 1, {"b.txt": b"B"}, _CRYPTO)[0],
    ]
    foreign = tmp_path / "foreign.iso"
    foreign_staging = _make_staging(tmp_path / "fstaging", 0, {"x.txt": b"x"})
    write_volume_marker(
        foreign_staging,
        VolumeMarker("other-archive", 0, 0, "2026-10-15T00:00:00Z", ""),
    )
    ISOBackend(foreign, disc_size=_DISC).init(foreign_staging, label="FOR")
    bare = tmp_path / "bare.iso"
    bare_staging = _make_staging(tmp_path / "bare-staging", 0, {"y.txt": b"y"})
    ISOBackend(bare, disc_size=_DISC).init(bare_staging, label="BARE")

    backend, catalog, _manifests = _source(discs[1])
    remember_encryption_mode(_ID, "none")
    answers = iter(["", "", "", "", ""])

    class _Drive:
        device = None

        def __init__(self) -> None:
            self._inner = ISOBackend(discs[1], disc_size=_DISC)

        def _set(self, iso):
            self._inner = ISOBackend(iso, disc_size=_DISC)

        def read_paths(self, paths):
            return self._inner.read_paths(paths)

        def mediainfo(self):
            return self._inner.mediainfo()

    drive = _Drive()
    inserts = iter([bare, foreign, discs[0], discs[1], discs[1]])

    def _ask(prompt):
        drive._set(next(inserts))
        return next(answers)

    acq = DiscAcquisition(drive, ask=_ask)
    needed = {1: 1}
    first = acq.acquire(needed, catalog, _ID)
    second = acq.acquire(needed, catalog, _ID)
    third = acq.acquire(needed, catalog, _ID)
    fourth = acq.acquire(needed, catalog, _ID)
    err = capsys.readouterr().err
    assert first == (None, None, False)
    assert second == (None, None, False)
    assert third == (None, None, False)
    assert fourth[0] is drive and fourth[1:] == (None, False)
    assert "No readable disc in the drive" in err
    assert "another archive" in err
    assert "That is disc 0, not needed for this restore plan." in err




def test_coverage_reads_marker_when_caller_omits_it(tmp_path, monkeypatch):
    """
    Input:  a ledger written for a marked archive, then a torn session list
    Output: the coverage check still raises
    Details:
        The ledger write keys on the disc marker's archive_id. A caller
        that omits the marker (sync/status/restore wrappers do) must
        still reach that row, or the torn-read guard silently disables.
        No xdelta3 is needed: the manifests are written directly.
    """
    _isolate(tmp_path, monkeypatch)
    from oddarchiver.cli import _check_loaded_manifests
    from oddarchiver.ledger import record_confirmed
    from tests.test_restore_plan import _stage, _source, _CRYPTO, _ID, _DISC

    iso, _prior = _stage(tmp_path, 0, {"a.txt": b"A"}, _CRYPTO)
    backend, catalog, manifests = _source(iso)
    marker = read_volume_marker(backend)
    record_confirmed(
        "VOL0", str(iso), 2, archive_id=_ID,
        volume_index=marker.volume_index if marker else 0,
    )
    torn: list = list(manifests[:1])
    args = argparse.Namespace(recover_scan=False, label="VOL0")
    with pytest.raises(RuntimeError, match="Read failure for session 001"):
        _check_loaded_manifests(
            torn,
            backend=backend,
            args=args,
            disc_session_count=backend.mediainfo().session_count,
            command="sync",
        )


def test_restore_empty_drive_says_no_readable_disc(tmp_path, monkeypatch, caplog):
    """
    Input:  a drive whose mediainfo reports no media
    Output: one error line that says no readable disc is loaded
    Details:
        The message must not be a raw "Failed to read N path(s)".
    """
    _isolate(tmp_path, monkeypatch)
    from oddarchiver.cli import _restore_no_disc_check
    from oddarchiver.disc import DiscInfo

    class EmptyDrive:
        device = "/dev/sr0"

        def mediainfo(self):
            raise RuntimeError(
                "dvd+rw-mediainfo failed: : no media found in drive"
            )

        def read_paths(self, paths):
            raise RuntimeError(
                f"Failed to read {len(paths)} path(s) from {self.device} via xorriso: no media"
            )

    with pytest.raises(RuntimeError, match="No readable disc is loaded"):
        _restore_no_disc_check(EmptyDrive())


def test_missing_keyfile_prints_clean_error(tmp_path, monkeypatch, capsys):
    """
    Input:  restore --key pointing at a path that does not exist
    Output: one clean error line; no Unhandled-error traceback
    """
    _isolate(tmp_path, monkeypatch)
    keyfile = tmp_path / "real.key"
    generate_keyfile(str(keyfile))
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"secret")
    iso = tmp_path / "enc.iso"
    from tests.test_wave2_trust import _make_args
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        encrypt="keyfile", key=str(keyfile), label="KEYTEST",
    )) == 0
    args = _make_args(
        command="restore", dest=str(tmp_path / "out"), test_iso=str(iso),
        key=str(tmp_path / "absent.key"), all_disks=True,
    )
    rc = dispatch(args)
    err = capsys.readouterr().err
    assert rc == 1
    assert "Keyfile not found" in err
    assert "Unhandled error" not in err


def test_needed_name_names_requested_index(tmp_path, monkeypatch):
    """
    Input:  a catalog with an unresolved session on an unknown older disc
    Output: the name names the requested disc index
    Details:
        Needed index 2 must not print another disc's label name.
    """
    from oddarchiver.catalog import ArchiveCatalog, CatalogDisc

    _isolate(tmp_path, monkeypatch)
    catalog = ArchiveCatalog(
        archive_id="arch", source_volume_index=2, last_session=3,
        discs={1: CatalogDisc(1, "T8", 0, 1)},
        history={}, complete=False,
    )
    catalog._unknown[3] = {"some.txt"}
    name = _needed_name(catalog, 2)
    assert "disc 2" in name
    assert "T8 (disc 1)" != name


def test_scan_warns_and_counts_skipped_paths(tmp_path, capsys, monkeypatch):
    """
    Input:  a source with a dangling symlink, a FIFO, and an unreadable file
    Output: one warning line per path and a skipped count
    Details:
        The scan must not drop these paths silently.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"keep")
    (source / "dangling").symlink_to(tmp_path / "missing-target")
    os.mkfifo(source / "pipe")
    locked = source / "locked.bin"
    locked.write_bytes(b"secret")
    os.chmod(locked, 0o000)
    try:
        state = _scan_source_with_progress(source)
    finally:
        os.chmod(locked, 0o600)
    out = capsys.readouterr().out
    assert "keep.txt" in state
    assert "dangling" not in state
    assert "pipe" not in state
    assert "locked.bin" not in state
    assert "skipped" in out
    assert "dangling" in out
    assert "pipe" in out
    assert "locked.bin" in out


def test_restore_warns_on_overwritten_differing_file(tmp_path, monkeypatch, capsys):
    """
    Input:  a destination file whose content differs from the archive
    Output: a warning naming the overwritten path
    Details:
        The restore still overwrites, but it must say so.
    """
    _isolate(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "file.bin"
    target.write_bytes(b"different destination bytes")
    entry = _entry(tmp_path, 0, b"archived payload bytes")
    run_session_replay(
        dest, _Source(tmp_path / "disc"), NullCrypto(),
        {"file.bin": [(0, entry)]}, {}, 0, True, None, {}, False,
    )
    out = capsys.readouterr().out
    assert "overwrote existing file" in out
    assert "file.bin" in out
    assert target.read_bytes() == b"archived payload bytes"


def test_restore_read_only_dest_includes_error_cause(tmp_path, monkeypatch, capsys):
    """
    Input:  a destination file that cannot be replaced
    Output: the failure line includes the first error cause and path
    Details:
        The write failure is counted and reported with its cause. The
        command does not abort at the first file.
    """
    _isolate(tmp_path, monkeypatch)
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "file.bin"
    target.write_bytes(b"old")
    entry = _entry(tmp_path, 0, b"new bytes")
    real_replace = os.replace

    def deny(src, dst):
        if Path(dst) == target:
            raise PermissionError(13, "Permission denied", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", deny)
    restored, failed, *_rest = run_session_replay(
        dest, _Source(tmp_path / "disc"), NullCrypto(),
        {"file.bin": [(0, entry)]}, {}, 0, True, None, {}, False,
    )
    err = capsys.readouterr().err
    assert (restored, failed) == (0, 1)
    assert "could not write" in err
    assert "Permission denied" in err
    assert str(target) in err
    assert target.read_bytes() == b"old"


def test_init_refuses_disc_with_interrupted_burn_and_no_archive(
    monkeypatch, tmp_path, capsys,
):
    """
    Real-disc regression (T12): after a killed burn and a drive power
    cycle, the disc reported one incomplete MMC session with no readable
    ISO tree. init printed "already initialized; skipping" and exited 0,
    so the user believed an archive existed. init must refuse instead.
    """
    from oddarchiver.cli import build_parser, dispatch
    from oddarchiver.disc import DiscBackend, DiscInfo

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("a")
    monkeypatch.setattr(
        DiscBackend, "mediainfo",
        lambda self, retries=6, retry_delay=5.0: DiscInfo(
            session_count=1, remaining_bytes=10**9, used_bytes=29 * 2**20, label="",
        ),
    )

    def unreadable(self):
        raise RuntimeError("Cannot list sessions: xorriso : FAILURE : Cannot read ISO image tree")

    monkeypatch.setattr(DiscBackend, "list_session_numbers", unreadable)
    args = build_parser().parse_args(
        ["init", str(source), "--device", "/dev/sr0", "--encrypt", "none"],
    )
    assert dispatch(args) == 1
    err = capsys.readouterr().err
    assert "no readable archive" in err
    assert "already initialized" not in err


@pytest.mark.parametrize(
    ("count", "text"),
    [
        (3_000_000_000, "2.8 GiB"),
        (4_706_074_624, "4.4 GiB"),
        (1536, "1.5 KiB"),
        (5 * 2**40, "5.0 TiB"),
        (2048 * 2**40, "2048.0 TiB"),
    ],
)
def test_fmt_bytes_keeps_the_fraction(count, text):
    """
    Input:  count, text - a byte count and its expected display
    Output: None
    Details:
        Real DVD dry run printed 3,000,000,000 B as "2.0 GiB" and a
        4.38 GiB disc as "4.0 GiB": integer division dropped each fraction.
    """
    from oddarchiver.log import _fmt_bytes

    assert _fmt_bytes(count) == text


def test_unknown_disc_size_names_the_presets():
    """
    Input:  None
    Output: None
    Details:
        "--disc-size dvd" printed "invalid literal for int()"; the error
        must name the bad value and the valid presets.
    """
    from oddarchiver.disc import parse_disc_size

    with pytest.raises(ValueError) as raised:
        parse_disc_size("dvd")
    message = str(raised.value)
    assert "'dvd'" in message and "dvd-sl" in message
    assert "invalid literal" not in message


def test_fill_swap_lists_the_inserted_disc_sessions(tmp_path, monkeypatch, capsys):
    """
    Input: two ISO volumes; IsolatedDrive starts on volume 1 then swaps to 0
    Output: fill restage uses volume 0 sessions; restore does not report
        Session 1 is missing
    """
    from tests.test_fill_layout import IsolatedDrive
    from tests.test_volume_staging import (
        _FILL_DISC, _FILL_DISC_BYTES, _build_two_volume_fill_archive,
        _fill_argv, _fill_dispatch,
    )

    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    drive = IsolatedDrive(vol1, _FILL_DISC_BYTES)
    assert 0 not in drive.list_session_numbers()

    def _make(_args):
        return drive

    def _ask(prompt=""):
        text = str(prompt).lower()
        if "volume 0" in text:
            drive.load(iso)
        elif "volume 1" in text:
            drive.load(vol1)
        return ""

    monkeypatch.setattr("oddarchiver.cli._make_backend", _make)
    monkeypatch.setattr("builtins.input", _ask)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1),
        "--disc-size", _FILL_DISC, "--fill",
    ))
    assert rc == 0
    backend = ISOBackend(iso, disc_size=_FILL_DISC_BYTES)
    marker = read_volume_marker(backend)
    assert marker is not None
    load_session_manifests(
        backend, _CRYPTO, start_session=marker.first_session, marker=marker,
    )
    dest = tmp_path / "restored"
    capsys.readouterr()
    restore_rc = dispatch(build_parser().parse_args([
        "restore", str(dest), "--test-iso", str(iso), "--all-disks",
    ]))
    err = capsys.readouterr().err
    assert restore_rc == 0
    assert "Session 1 is missing" not in err

