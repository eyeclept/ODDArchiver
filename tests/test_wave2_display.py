"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Wave-2 regression tests. Covers the baseline reservation bound,
    ISO scaffolding and per-file on-disc budgets, session-list caching
    and retry, archive session counts without manifest reloads, logged
    tree cleanup, ISO volume labels, browse prompt parsing, and safe
    display routing.
"""
# Imports
import argparse
import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.cli import (
    _ControlFiles,
    _run_browse,
    _run_dry_run,
    _run_history,
    _run_restore,
    _run_status,
    _swap_after_full_volume,
    dispatch,
)
from oddarchiver.crypto import KeyfileCrypto, NullCrypto, generate_keyfile
from oddarchiver.disc import DiscBackend, DiscInfo, ISOBackend, archive_session_count
from oddarchiver.manifest import MANIFEST_VERSION, Manifest, ManifestEntry
from oddarchiver.restore import RestoreResult
from oddarchiver.session import (
    _baseline_plain_size,
    _baseline_reservation,
    build_staging,
    remove_tree,
)
from oddarchiver.verify import detect_incomplete_session
from oddarchiver.volume import (
    PathVersion,
    VolumeBaseline,
    VolumeMarker,
    VolumeRecord,
    build_volume_baseline,
)

# Globals
logger = logging.getLogger(__name__)
_log = logger
_crypto = NullCrypto()
_SMALL_DISC = 24 * 1024 * 1024
_HISTORY_DISC = 4 * 1024 * 1024
_TINY_DISC = 400 * 1024
_ESCAPE = "BAD\x1b[31mLABEL"


# Functions


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """
    Input: tmp_path, monkeypatch
    Output: None
    Details: Keep cache and state files out of the user home directories.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache))


def _make_args(**kwargs) -> argparse.Namespace:
    """
    Input: kwargs - field overrides for the Namespace
    Output: argparse.Namespace with defaults for test commands
    """
    defaults = {
        "command": "init", "source": None, "dest": None, "device": "/dev/sr0",
        "test_iso": None, "label": "W2", "encrypt": "none", "key": None,
        "dry_run": False, "no_cache": False, "disc_size": "50mb",
        "prefill": None, "session": None, "force": False, "level": "fast",
        "mirror": None, "recover_scan": False, "paths": None,
        "show_deleted": False, "path": None, "rescue_key": None,
        "skip_volumes": None, "unskip_volumes": None, "all_disks": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _manifest(session: int, entries=(), deleted=(), timestamp="", label="") -> Manifest:
    """Build a minimal manifest for reservation and display tests."""
    return Manifest(
        version=MANIFEST_VERSION,
        session=session,
        timestamp=timestamp or "2026-10-15T00:00:00Z",
        source="/src",
        label=label,
        based_on_session=session - 1 if session else None,
        encryption={"mode": "none"},
        entries=list(entries),
        deleted=list(deleted),
        manifest_checksum="",
    )


def _entry(path: str, session: int, size: int = 10) -> ManifestEntry:
    """Build a full manifest entry pointing at one session blob."""
    return ManifestEntry(
        path=path,
        type="full",
        result_checksum="0" * 64,
        full_size_bytes=size,
        file=f"session_{session:03d}/full/{'a' * 64}",
    )


def _baseline(paths: list[str], versions: int) -> VolumeBaseline:
    """
    Input: paths    - history paths to fill
            versions - number of events per path
    Output: VolumeBaseline with a real history and live state
    """
    history = {}
    state = {}
    origin = {}
    for path in paths:
        history[path] = [
            PathVersion(
                session=i,
                volume_index=0,
                kind="full",
                result_checksum="0" * 64,
                full_size_bytes=2**20,
                timestamp="2026-10-15T00:00:00Z",
                blob_session=i,
            )
            for i in range(versions)
        ]
        state[path] = "0" * 64
        origin[path] = versions - 1
    return VolumeBaseline(
        version=2,
        volumes=[VolumeRecord(
            volume_index=0,
            label="W2",
            first_session=0,
            last_session=versions - 1,
        )],
        history=history,
        state=state,
        origin=origin,
    )


def _write_source(source: Path, count: int, size: int = 4, prefix: str = "f") -> None:
    """Create count small files with distinct content in source."""
    source.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        payload = i.to_bytes(4, "big") + b"x" * max(0, size - 4)
        (source / f"{prefix}{i:05d}.bin").write_bytes(payload)


def _iso_paths(first: Path) -> list[Path]:
    """Return first plus every sibling .volN.iso that the run produced."""
    found = [first]
    n = 1
    while True:
        extra = first.with_name(f"{first.stem}.vol{n}{first.suffix}")
        if not extra.is_file():
            return found
        found.append(extra)
        n += 1


# Tests


def test_baseline_reservation_is_safe_and_tight():
    """
    Input:  None
    Output: None
    Details:
        The reservation covers the baseline that the next disc writes
        and stays under 5 percent plus a fixed constant. The old fixed
        per-path allowance plus 4 phantom volume records over-reserved.
    """
    old_paths = [f"d/{i:04d}.bin" for i in range(20)]
    events = [f"n/{i:04d}.bin" for i in range(20)] + ["gone.txt"]
    checksums = {path: "0" * 64 for path in events}
    baseline = _baseline(old_paths, 3)
    closing = [_manifest(3, [_entry(path, 3) for path in old_paths])]
    marker = VolumeMarker(
        archive_id="archive", volume_index=1, first_session=0,
        created_at="2026-10-15T00:00:00Z",
        previous_volume_label="W2", format=2,
    )
    reserved = _baseline_reservation(
        _crypto, baseline, closing, marker, "W2", events, checksums,
    )
    run = _manifest(
        4,
        [_entry(path, 4) for path in events if path != "gone.txt"],
        deleted=["gone.txt"],
    )
    produced = build_volume_baseline(baseline, closing + [run], marker, "W2")
    actual = _baseline_plain_size(produced)
    assert reserved >= actual, f"reservation {reserved} is below actual {actual}"
    slack = reserved - actual
    assert slack <= int(actual * 0.05) + 8192, (
        f"reservation slack {slack} exceeds 5 percent of {actual} plus 8192"
    )


def test_many_tiny_files_pack_on_small_disc(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Many small files on a small disc must pack into sessions rather
        than fail. The old per-path baseline reservation drove
        full_budget to zero after a few hundred files.
    """
    source = tmp_path / "src"
    _write_source(source, 500)
    backend = ISOBackend(tmp_path / "tiny.iso", disc_size=_TINY_DISC)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=backend,
        cache=MagicMock(),
        crypto=_crypto,
        _staging_root=tmp_path / "staging",
    )
    assert chunks
    covered = 0
    for staging, session_n, _is_new in chunks:
        session_dir = staging / f"session_{session_n:03d}"
        assert session_dir.is_dir()
        covered += sum(1 for p in (session_dir / "full").glob("*"))
    assert covered == 500


def _assert_isos_fit_disc(isos: list[Path], disc_size: int) -> None:
    """Input: ISO paths and disc size. Output: each ISO fits its disc."""
    for path in isos:
        size = path.stat().st_size
        assert size <= disc_size, (
            f"{path.name} is {size} bytes; disc_size is {disc_size}"
        )


def test_ten_thousand_tiny_files_stay_within_disc_size(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        The packing budget reserves ISO scaffolding, per-file ISO and
        manifest bytes, and the baseline. Every produced ISO stays
        within disc_size for 10000 tiny files.
    """
    source = tmp_path / "src"
    _write_source(source, 10_000)
    iso = tmp_path / "many.iso"
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size=str(_SMALL_DISC), label="MANY",
    ))
    assert rc == 0
    isos = _iso_paths(iso)
    assert len(isos) >= 2
    _assert_isos_fit_disc(isos, _SMALL_DISC)


def test_long_history_isos_stay_within_disc_size(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A growing history makes the baseline on each new disc larger.
        Every ISO still stays within disc_size.
    """
    source = tmp_path / "src"
    _write_source(source, 800, prefix="a")
    iso = tmp_path / "hist.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size=str(_HISTORY_DISC), label="HIST",
    )) == 0
    for prefix in ("b", "c"):
        _write_source(source, 800, prefix=prefix)
        latest = _iso_paths(iso)[-1]
        assert dispatch(_make_args(
            command="sync", source=str(source), test_iso=str(latest),
            disc_size=str(_HISTORY_DISC), label="HIST",
        )) == 0
    isos = _iso_paths(iso)
    assert len(isos) >= 3
    _assert_isos_fit_disc(isos, _HISTORY_DISC)


def test_init_spill_isos_fit_the_disc(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A three-disc init spill of many small files must not overflow
        any ISO. This is the strict budget check.
    """
    disc_size = 8 * 1024 * 1024
    source = tmp_path / "src"
    source.mkdir()
    for i in range(40):
        (source / f"f{i:02d}.bin").write_bytes(bytes([i % 256]) * (400 * 1024))
    iso = tmp_path / "spill.iso"
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size=str(disc_size), label="SPILL",
    ))
    assert rc == 0
    isos = _iso_paths(iso)
    assert len(isos) >= 3
    for path in isos:
        size = path.stat().st_size
        assert size <= disc_size, f"{path.name} is {size} bytes; disc_size is {disc_size}"


def test_disc_backend_lists_sessions_once_per_command(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        The session list is cached on the backend, so repeated calls in
        one command cost one xorriso listing.
    """
    calls = []

    def fake_run(cmd, capture_output):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, b"'session_000'\n'session_001'\n", b"",
        )

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    backend = DiscBackend("/dev/sr9")
    assert backend.list_session_numbers() == [0, 1]
    assert backend.list_session_numbers() == [0, 1]
    assert len(calls) == 1
    backend.invalidate_session_list()
    assert backend.list_session_numbers() == [0, 1]
    assert len(calls) == 2


def test_burn_invalidates_the_cached_session_list(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        The ISO backend caches its listing between burns. A burn adds a
        session, so the burn must drop the cache.
    """
    iso = tmp_path / "x.iso"
    backend = ISOBackend(iso, disc_size=2 * 1024 * 1024)
    staging0 = tmp_path / "staging0"
    (staging0 / "session_000").mkdir(parents=True)
    (staging0 / "session_000" / "a.txt").write_bytes(b"a")
    backend.init(staging0, label="W2")
    assert backend.list_session_numbers() == [0]
    stray = backend._sessions_root / "session_009"
    stray.mkdir()
    assert backend.list_session_numbers() == [0]
    stray.rmdir()
    staging1 = tmp_path / "staging1"
    (staging1 / "session_001").mkdir(parents=True)
    (staging1 / "session_001" / "b.txt").write_bytes(b"b")
    backend.append(staging1, label="W2", expected_session_count=1)
    assert backend.list_session_numbers() == [0, 1]


def test_disc_backend_session_list_retries_once_then_fails(monkeypatch):
    """
    Input:  monkeypatch
    Output: None
    Details:
        A failed listing retries once and then raises a clear error. It
        never falls back to a 3000 control-path probe.
    """
    attempts = []

    def fake_run(cmd, capture_output):
        attempts.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, b"", b"no disc")

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    backend = DiscBackend("/dev/sr9")
    with pytest.raises(RuntimeError, match="Cannot list sessions"):
        backend.list_session_numbers()
    assert len(attempts) == 2


def test_archive_session_count_uses_known_session_list(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        archive_session_count uses a session list the caller knows and
        does not reload every control file.
    """
    class _Backend(ISOBackend):
        def read_paths(self, paths):
            raise AssertionError("archive_session_count must not reload control files")

    backend = _Backend(tmp_path / "x.iso", disc_size=2 * 1024 * 1024)
    assert archive_session_count(backend, listed=[5, 6], first_session=5) == 7


def test_verify_trailing_session_needs_no_blind_probe(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        detect_incomplete_session uses the session listing and the
        control bytes the caller already fetched. It does not probe the
        medium with read_path.
    """
    iso = tmp_path / "x.iso"
    backend = ISOBackend(iso, disc_size=2 * 1024 * 1024)
    staging = tmp_path / "staging"
    (staging / "session_000").mkdir(parents=True)
    (staging / "session_000" / "a.txt").write_bytes(b"a")
    backend.init(staging, label="W2")
    trailing = iso.with_suffix(".d") / "session_001"
    trailing.mkdir()
    (trailing / "manifest.json").write_text("{not json", encoding="utf-8")

    calls = []

    def forbid(path, *args, **kwargs):
        calls.append(path)
        raise AssertionError("verify must not probe read_path")

    backend.read_path = forbid
    loaded = {0: _manifest(0, [_entry("a.txt", 0)])}
    assert detect_incomplete_session(backend, loaded) == 1
    assert calls == []


def test_remove_tree_failure_is_visible(tmp_path, monkeypatch, caplog, capsys):
    """
    Input:  tmp_path, monkeypatch, caplog, capsys
    Output: None
    Details:
        A failed plaintext temp removal logs a warning and prints a
        user note. The directory name stays visible to the user.
    """
    scratch = tmp_path / "decrypted"
    scratch.mkdir()
    (scratch / "secret.txt").write_bytes(b"secret")

    def boom(path, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("oddarchiver.session.shutil.rmtree", boom)
    caplog.set_level(logging.WARNING)
    assert not remove_tree(scratch, "verify temp")
    assert scratch.exists()
    assert "Cannot remove verify temp" in caplog.text
    assert "decrypted plaintext" in capsys.readouterr().err


def test_encrypted_iso_label_reads_from_volume_descriptor(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        An encrypted ISO has no plaintext manifest.json. The label
        comes from the ISO volume identifier in the image.
    """
    keyfile = tmp_path / "k.key"
    generate_keyfile(str(keyfile))
    crypto = KeyfileCrypto(str(keyfile))
    iso = tmp_path / "enc.iso"
    backend = ISOBackend(iso, disc_size=2 * 1024 * 1024)
    staging = tmp_path / "staging"
    (staging / "session_000").mkdir(parents=True)
    (staging / "session_000" / "a.txt").write_bytes(b"a")
    (staging / "session_000" / "manifest.enc").write_bytes(crypto.encrypt(b"{}"))
    (staging / "session_000" / "enc_mode.json").write_text('{"mode": "keyfile"}')
    backend.init(staging, label="SECRET")
    assert not (iso.with_suffix(".d") / "manifest.json").exists()
    assert backend.mediainfo().label.startswith("SECRET")
    raw = iso.read_bytes()[32768 + 40:32768 + 40 + 32]
    assert raw.rstrip(b" ").decode("ascii", errors="replace").startswith("SECRET")


def test_browse_number_prompt_rejects_unicode_and_long_numbers(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        The browse number prompt uses isdecimal with a six-digit cap.
        It re-prompts on bad input instead of raising ValueError.
    """
    iso = tmp_path / "x.iso"
    backend = ISOBackend(iso, disc_size=2 * 1024 * 1024)
    staging = tmp_path / "staging"
    (staging / "session_000").mkdir(parents=True)
    (staging / "session_000" / "a.txt").write_bytes(b"a")
    backend.init(staging, label="W2")
    answers = iter(["\u00b2", "1234567", "q"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(answers))
    monkeypatch.setattr("oddarchiver.cli._make_backend", lambda _args: backend)
    monkeypatch.setattr(
        "oddarchiver.cli._crypto_and_manifests",
        lambda *a, **k: (
            NullCrypto(), [_manifest(0, [_entry("a.txt", 0)])],
            _ControlFiles({}, None),
        ),
    )
    rc = _run_browse(_make_args(command="browse", test_iso=str(iso)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Unknown command" in out
    assert out.count("Enter a file number with at most six digits.") >= 1


def test_status_history_and_restore_escape_disc_strings(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        Status labels, history timestamps, and restore stale, missing,
        and unresolved paths print through catalog.safe_display. Raw
        escape sequences never reach the terminal.
    """
    iso = tmp_path / "x.iso"
    iso.write_bytes(b"")
    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=1, remaining_bytes=1024,
        used_bytes=0, label=_ESCAPE,
    )
    backend.read_paths.return_value = {}
    backend.list_session_numbers.return_value = []
    monkeypatch.setattr("oddarchiver.cli._make_backend", lambda _args: backend)

    manifest = _manifest(0, [_entry("a.txt", 0)], timestamp=_ESCAPE, label="W2")
    monkeypatch.setattr(
        "oddarchiver.cli._crypto_and_manifests",
        lambda *a, **k: (NullCrypto(), [manifest], _ControlFiles({}, None)),
    )
    _run_status(_make_args(command="status", test_iso=str(iso)))
    _run_history(_make_args(command="history", test_iso=str(iso)))
    out = capsys.readouterr()
    assert "\x1b" not in out.out + out.err
    assert "BAD" in out.out

    result = RestoreResult(
        restored=0,
        failures=0,
        stale=[(_ESCAPE, 1)],
        missing=[_ESCAPE],
        unresolved=[(_ESCAPE, _ESCAPE)],
    )
    monkeypatch.setattr("oddarchiver.restore.restore", lambda **kwargs: result)
    rc = _run_restore(_make_args(
        command="restore", dest=str(tmp_path), test_iso=str(iso),
    ))
    assert rc == 0
    out = capsys.readouterr()
    assert "\x1b" not in out.out + out.err


def test_disc_swap_and_dry_run_escape_disc_strings(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        The disc-swap prompt and the dry-run label print disc-derived
        strings through catalog.safe_display.
    """
    prompts = []
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": prompts.append(prompt) or "q",
    )
    backend = MagicMock()
    backend.mediainfo.return_value = DiscInfo(
        session_count=0, remaining_bytes=1024,
        used_bytes=0, label=_ESCAPE,
    )
    backend.read_paths.return_value = {}
    assert _swap_after_full_volume(backend, _ESCAPE, 1, None, None) is None
    assert prompts and "\x1b" not in prompts[0] and "BAD" in prompts[0]

    source = tmp_path / "src"
    _write_source(source, 1, prefix="new")
    manifest = _manifest(0, [_entry("a.txt", 0)], label=_ESCAPE)
    backend.mediainfo.return_value = DiscInfo(
        session_count=1, remaining_bytes=1024,
        used_bytes=0, label=_ESCAPE,
    )
    monkeypatch.setattr("oddarchiver.cli._make_backend", lambda _args: backend)
    monkeypatch.setattr(
        "oddarchiver.cli._crypto_and_manifests",
        lambda *a, **k: (NullCrypto(), [manifest], _ControlFiles({}, None)),
    )
    rc = _run_dry_run(_make_args(
        command="sync", source=str(source), test_iso="x.iso",
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "BAD" in out


def test_restore_plan_error_prints_escape_paths(monkeypatch, capsys):
    """
    Input:  monkeypatch, capsys
    Output: None
    Details:
        restore_plan prints archive paths in error messages through
        catalog.safe_display.
    """
    from oddarchiver.manifest import ManifestEntry as Entry
    from oddarchiver.restore_plan import _check_entry
    from oddarchiver.volume import PathVersion as Version

    entry = Entry(
        path=_ESCAPE, type="full", result_checksum="0" * 64,
        full_size_bytes=10, file="session_000/full/" + "a" * 64,
    )
    version = Version(
        session=0, volume_index=0, kind="delta", result_checksum="1" * 64,
        full_size_bytes=10, timestamp="2026-10-15T00:00:00Z", blob_session=0,
    )
    with pytest.raises(RuntimeError) as exc:
        _check_entry(entry, version, 0, _ESCAPE)
    message = str(exc.value)
    assert "\x1b" not in message
    assert "BAD" in message
