"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Two-phase fill: intent stub, target burn, outcome stub, and resume.
"""
# Imports
import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

from oddarchiver.catalog import remembered_discs
from oddarchiver.cli import _confirm_skip_intent, build_parser, dispatch
from oddarchiver.disc import (
    GROWISOFS_MERGE_LIMIT_BYTES, ISOBackend, MediaWriteError,
)
from oddarchiver.volume import (
    read_fill_intent,
    read_fill_outcome,
    read_volume_baseline,
    read_volume_marker,
    unmatched_fill_intent,
)
from tests.helpers import fill
from tests.test_fill_layout import IsolatedDrive, _filled_two_volume_archive, _isolate_state

from tests.test_volume_staging import (
    _FILL_DISC,
    _FILL_DISC_BYTES,
    _build_two_volume_fill_archive,
    _fill_argv,
    _fill_dispatch,
    _iso_sessions,
)

logger = logging.getLogger(__name__)


def _spy_iso_appends(monkeypatch) -> list[tuple[str, str]]:
    """Input: pytest monkeypatch. Output: list of (iso name, stub kind) in order."""
    order: list[tuple[str, str]] = []
    real = ISOBackend.append

    def _kind(staging: Path) -> str:
        vol = staging / "volume.json"
        names = [p.name for p in staging.iterdir()]
        if any(name.startswith("session_") for name in names):
            return "session"
        if not vol.is_file():
            return "other"
        raw = json.loads(vol.read_text(encoding="utf-8"))
        if raw.get("fill_outcome") or raw.get("fill_outcome_enc"):
            return "outcome"
        if raw.get("fill_intent") or raw.get("fill_intent_enc"):
            return "intent"

        return "stub"

    def _wrap(self, staging, label, expected_session_count, **kwargs):
        order.append((Path(self.iso_path).name, _kind(Path(staging))))
        return real(
            self, staging, label, expected_session_count, **kwargs,
        )

    monkeypatch.setattr(ISOBackend, "append", _wrap)
    return order


def _read_newest_intent(vol1: Path):
    """Input: volume-1 ISO. Output: FillIntent or None."""
    return read_fill_intent(ISOBackend(vol1, disc_size=_FILL_DISC_BYTES), None)


def _read_newest_outcome(vol1: Path):
    """Input: volume-1 ISO. Output: FillOutcome or None."""
    return read_fill_outcome(ISOBackend(vol1, disc_size=_FILL_DISC_BYTES), None)


def test_full_fill_writes_intent_session_outcome_in_order(tmp_path, monkeypatch):
    """
    Input: two ISO volumes and a small file that fits volume 0
    Output: intent on newest, fill session on target, outcome on newest
    """
    _isolate_state(tmp_path, monkeypatch)
    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    order = _spy_iso_appends(monkeypatch)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    assert _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    )) == 0
    kinds = [item for item in order if item[1] in {"intent", "session", "outcome"}]
    assert kinds[0] == (vol1.name, "intent")
    assert kinds[1] == (iso.name, "session")
    assert kinds[2] == (vol1.name, "outcome")
    intent = _read_newest_intent(vol1)
    outcome = _read_newest_outcome(vol1)
    assert intent is not None
    assert outcome is not None
    assert intent.session == outcome.session
    assert outcome.status == "landed"
    assert intent.target_volume_index == 0
    assert max(_iso_sessions(iso)) == intent.session
    assert unmatched_fill_intent(
        ISOBackend(vol1, disc_size=_FILL_DISC_BYTES), None,
    ) is None


def test_quit_after_target_leaves_intent_and_resume_writes_outcome(
    tmp_path, monkeypatch, capsys,
):
    """
    Input: fill that stops after the target burn
    Output: next sync confirms session N, writes outcome, plans only new files
    """
    import oddarchiver.cli as cli_mod

    _isolate_state(tmp_path, monkeypatch)
    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    real_finish = cli_mod._finish_fill_two_phase
    monkeypatch.setattr(cli_mod, "_finish_fill_two_phase", lambda *a, **k: None)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    assert _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    )) == 0
    intent = _read_newest_intent(vol1)
    assert intent is not None
    assert _read_newest_outcome(vol1) is None
    monkeypatch.setattr(cli_mod, "_finish_fill_two_phase", real_finish)
    (source / "later.bin").write_bytes(fill(b"L", 20_000))
    capsys.readouterr()
    rc = dispatch(build_parser().parse_args([
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--dry-run",
    ]))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    outcome = _read_newest_outcome(vol1)
    assert outcome is not None
    assert outcome.status == "landed"
    assert outcome.session == intent.session
    assert "later.bin" in captured.out
    assert "fit.bin" not in captured.out.split("Changes")[-1]


def test_intent_without_target_burn_records_failed_and_replans(
    tmp_path, monkeypatch, capsys,
):
    """
    Input: intent stub and no target session
    Output: next sync records failed outcome and plans the same files
    """
    import oddarchiver.cli as cli_mod

    _isolate_state(tmp_path, monkeypatch)
    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    real_burn = cli_mod._burn_sync_group

    def _no_burn(chunks, backend, *args, **kwargs):
        return 1, backend

    monkeypatch.setattr(cli_mod, "_burn_sync_group", _no_burn)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    assert rc == 1
    intent = _read_newest_intent(vol1)
    assert intent is not None
    assert intent.session not in _iso_sessions(iso)
    monkeypatch.setattr(cli_mod, "_burn_sync_group", real_burn)
    capsys.readouterr()
    rc = dispatch(build_parser().parse_args([
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--dry-run",
    ]))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "was attempted" in captured.out
    outcome = _read_newest_outcome(vol1)
    assert outcome is not None
    assert outcome.status == "failed"
    assert "fit.bin" in captured.out


def test_newest_without_room_skips_stubs_and_lists_not_stubbed(
    tmp_path, monkeypatch, capsys,
):
    """
    Input: newest disc at the 4 GiB append limit
    Output: no intent or outcome on newest; fill layout lists it as not stubbed
    """
    _isolate_state(tmp_path, monkeypatch)
    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    ISOBackend(vol1, disc_size=_FILL_DISC_BYTES).prefill(GROWISOFS_MERGE_LIMIT_BYTES)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    capsys.readouterr()
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "skipping the intent and outcome stubs" in captured.out
    assert _read_newest_intent(vol1) is None
    assert _read_newest_outcome(vol1) is None
    fill_base = read_volume_baseline(ISOBackend(iso, disc_size=_FILL_DISC_BYTES), None)
    assert fill_base is not None
    assert fill_base.layout is not None
    assert 1 in fill_base.layout.not_stubbed


def test_fill_from_inserted_with_room_does_not_touch_other_volume(
    tmp_path, monkeypatch, capsys,
):
    """
    Input: fill started on volume 0 after it holds the newest catalog and has room
    Output: session lands on volume 0 and volume 1 is unchanged
    """
    source, iso, vol1 = _filled_two_volume_archive(tmp_path, monkeypatch)
    vol1_before = vol1.read_bytes()
    vol1_sessions = _iso_sessions(vol1)
    vol0_sessions = _iso_sessions(iso)
    (source / "tiny.bin").write_bytes(fill(b"T", 8_000))
    capsys.readouterr()
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(iso), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "does not ask for other volumes" in captured.out
    assert max(_iso_sessions(iso)) > max(vol0_sessions)
    assert _iso_sessions(vol1) == vol1_sessions
    assert vol1.read_bytes() == vol1_before


def test_fill_from_drive_inserted_does_not_prompt_other_disc(
    tmp_path, monkeypatch,
):
    """
    Input: IsolatedDrive on volume 0 after a fill; volume 0 has room
    Output: fill never calls _prompt_fill_disc; the burn stays on volume 0
    Details:
        IsolatedDrive is not ISOBackend. The current flow still fills
        the inserted volume without a swap prompt when that volume
        is the chosen target. The old swap stub never ran.
    """
    source, iso, vol1 = _filled_two_volume_archive(tmp_path, monkeypatch)
    (source / "tiny.bin").write_bytes(fill(b"T", 8_000))
    drive = IsolatedDrive(iso, _FILL_DISC_BYTES)
    prompted = {"n": 0}

    def _forbid_prompt(*args, **kwargs):
        prompted["n"] += 1
        raise AssertionError("fill must not prompt for another disc")

    monkeypatch.setattr("oddarchiver.cli._prompt_fill_disc", _forbid_prompt)
    monkeypatch.setattr(
        "oddarchiver.cli._make_backend",
        lambda _args: drive,
    )
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(iso), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    assert rc == 0
    assert prompted["n"] == 0
    assert max(_iso_sessions(iso)) > max(_iso_sessions(vol1))



def test_intent_prompt_says_press_enter(monkeypatch, capsys):
    """
    Input: intent skip prompt on a non-ISO backend
    Output: prompt tells the user to press Enter to write the stub
    """
    monkeypatch.setattr("builtins.input", lambda: "")
    assert _confirm_skip_intent(SimpleNamespace()) is False
    assert "Press Enter to write it." in capsys.readouterr().out


def test_intent_stub_media_write_error_closes_newest(
    tmp_path, monkeypatch, capsys,
):
    """
    Input: T19A WRITE@LBA failure on the intent stub
    Output: message names block 1660112; volume is not appendable;
            a later fill skips stubs and lists the volume as not stubbed
    """
    _isolate_state(tmp_path, monkeypatch)
    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    real = ISOBackend.append

    def _wrap(self, staging, label, expected_session_count, **kwargs):
        vol = Path(staging) / "volume.json"
        kind = "other"
        if vol.is_file():
            raw = json.loads(vol.read_text(encoding="utf-8"))
            if raw.get("fill_intent") or raw.get("fill_intent_enc"):
                kind = "intent"
        if Path(self.iso_path).resolve() == vol1.resolve() and kind == "intent":
            raise MediaWriteError(
                "The drive could not write this disc "
                "(write error at block 1660112). "
                "The new session did not land. A failed append does not "
                "change earlier sessions; run verify on this disc to "
                "confirm them. Do not append to this disc again: use "
                "another disc for this sync.",
                "write error at block 1660112",
            )
        return real(
            self, staging, label, expected_session_count, **kwargs,
        )

    monkeypatch.setattr(ISOBackend, "append", _wrap)
    monkeypatch.setattr("builtins.input", lambda: "")
    capsys.readouterr()
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    captured = capsys.readouterr()
    text = captured.err + captured.out
    assert rc == 1, text
    assert "write error at block 1660112" in text
    assert "earlier sessions" in text
    marker = read_volume_marker(ISOBackend(vol1, disc_size=_FILL_DISC_BYTES))
    assert marker is not None
    discs = remembered_discs(marker.archive_id)
    assert discs[marker.volume_index].appendable is False
    assert discs[marker.volume_index].appendable_reason == (
        "write error at block 1660112"
    )
    for leftover in tmp_path.rglob("oddarchiver_staging_*"):
        shutil.rmtree(leftover, ignore_errors=True)
    capsys.readouterr()
    rc2 = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    captured2 = capsys.readouterr()
    assert rc2 == 0, captured2.err
    assert "skipping the intent and outcome stubs" in captured2.out
    assert _read_newest_intent(vol1) is None
    assert _read_newest_outcome(vol1) is None
    fill_base = read_volume_baseline(
        ISOBackend(iso, disc_size=_FILL_DISC_BYTES), None,
    )
    assert fill_base is not None
    assert fill_base.layout is not None
    assert 1 in fill_base.layout.not_stubbed


def test_intent_stub_media_write_error_continue_fills(
    tmp_path, monkeypatch, capsys,
):
    """
    Input: T19A intent stub failure and an explicit yes to continue
    Output: fill lands on the older disc without an intent stub
    """
    _isolate_state(tmp_path, monkeypatch)
    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    vol0_sessions = _iso_sessions(iso)
    real = ISOBackend.append

    def _wrap(self, staging, label, expected_session_count, **kwargs):
        vol = Path(staging) / "volume.json"
        kind = "other"
        if vol.is_file():
            raw = json.loads(vol.read_text(encoding="utf-8"))
            if raw.get("fill_intent") or raw.get("fill_intent_enc"):
                kind = "intent"
        if Path(self.iso_path).resolve() == vol1.resolve() and kind == "intent":
            raise MediaWriteError(
                "The drive could not write this disc "
                "(write error at block 1660112). "
                "The new session did not land. A failed append does not "
                "change earlier sessions; run verify on this disc to "
                "confirm them. Do not append to this disc again: use "
                "another disc for this sync.",
                "write error at block 1660112",
            )
        return real(
            self, staging, label, expected_session_count, **kwargs,
        )

    monkeypatch.setattr(ISOBackend, "append", _wrap)
    monkeypatch.setattr("builtins.input", lambda: "yes")
    capsys.readouterr()
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    captured = capsys.readouterr()
    assert rc == 0, captured.err + captured.out
    assert "write error at block 1660112" in captured.err + captured.out
    assert _read_newest_intent(vol1) is None
    assert max(_iso_sessions(iso)) > max(vol0_sessions)


def test_stub_append_updates_recorded_volume_space(tmp_path, monkeypatch):
    """
    Input: fill whose intent stub lands on the newest ISO
    Output: local record matches the disc's used bytes
    Details:
        T20 on a real DVD-R showed the record still held the pre-stub
        measured bytes, so planning saw 15.9 MiB more free space than
        the disc had. After any successful append the record must match
        the medium.
    """
    _isolate_state(tmp_path, monkeypatch)
    source, iso, vol1 = _build_two_volume_fill_archive(tmp_path, monkeypatch)
    (source / "fit.bin").write_bytes(fill(b"C", 40_000))
    rc = _fill_dispatch(_fill_argv(
        "sync", str(source), "--test-iso", str(vol1), "--disc-size", _FILL_DISC,
        "--fill",
    ))
    assert rc == 0
    backend = ISOBackend(vol1, disc_size=_FILL_DISC_BYTES)
    marker = read_volume_marker(backend)
    assert marker is not None
    remembered = remembered_discs(marker.archive_id)[marker.volume_index]
    assert remembered.used_bytes == backend.mediainfo().used_bytes


def test_outcome_prompts_show_recorded_free_space(tmp_path, monkeypatch):
    """
    Input: newest disc with a recorded free-space measure
    Output: both outcome prompts carry that measure
    Details:
        T20 showed "free space unknown" at the outcome prompts while the
        record held a real remaining_bytes. The prompts must pass it on.
    """
    import oddarchiver.cli as cli_mod
    from oddarchiver.catalog import remember_volume_measure

    _isolate_state(tmp_path, monkeypatch)
    marker = SimpleNamespace(archive_id="arc-outcome", volume_index=1, first_session=1)
    remember_volume_measure(
        "arc-outcome", 1, "T20",
        used_bytes=100, remaining_bytes=777_000_000, appendable=True,
    )
    seen: list[int | None] = []

    def _capture(backend, archive_id, volume_index, label, remaining):
        seen.append(remaining)
        return "abort"

    monkeypatch.setattr(cli_mod, "_prompt_fill_disc", _capture)
    drive = SimpleNamespace(device=None)
    cli_mod._acquire_newest_for_outcome(drive, marker, "T20")
    cli_mod._record_pending_outcome(drive, marker, None, "T20", None, "landed", None)
    assert seen == [777_000_000, 777_000_000]


