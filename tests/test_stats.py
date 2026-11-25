"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Unit and CLI tests for oddarchiver.stats and status --stats flags.
"""
from __future__ import annotations

import json
import logging

import pytest

from oddarchiver.cli import build_parser, dispatch
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import DiscInfo
from oddarchiver.manifest import Manifest, ManifestEntry
from oddarchiver.stats import compute_stats, render_json
from tests.helpers import _make_args, _make_manifest

logger = logging.getLogger(__name__)
_log = logger

_JSON_ROOT = (
    "mode", "label", "capacity_bytes", "used_bytes", "remaining_bytes",
    "totals", "sessions", "suspect_sessions",
)
_JSON_TOTALS = (
    "original", "changed_full", "changed_delta", "dedup", "deleted",
    "control", "other", "filesystem_overhead",
)
_crypto = NullCrypto()


def _info(used: int = 1000, remaining: int = 9000, label: str = "TEST") -> DiscInfo:
    """Input: capacity fields. Output: DiscInfo for compute_stats."""
    return DiscInfo(
        session_count=1, remaining_bytes=remaining, used_bytes=used, label=label,
    )


def _full(
    path: str,
    session: int,
    size: int,
    blob: str | None = None,
    *,
    origin: int | None = None,
    chunk: int | None = None,
    count: int | None = None,
) -> ManifestEntry:
    """Input: path metadata. Output: a full ManifestEntry."""
    name = blob or f"session_{session:03d}/full/{path.replace('/', '_')}"
    return ManifestEntry(
        path=path, type="full", result_checksum="abc",
        full_size_bytes=size, file=name, blob_origin_session=origin,
        volume_chunk=chunk, volume_chunk_count=count,
    )


def _delta(path: str, session: int, full_size: int, delta_size: int) -> ManifestEntry:
    """Input: path and sizes. Output: a delta ManifestEntry."""
    name = f"session_{session:03d}/deltas/{path.replace('/', '_')}"
    return ManifestEntry(
        path=path, type="delta", result_checksum="def",
        full_size_bytes=full_size, delta_file=name, delta_size_bytes=delta_size,
    )


def _report(
    manifests: list[Manifest],
    *,
    used: int = 10_000,
    remaining: int = 90_000,
    control_raw: dict[str, bytes] | None = None,
    exact_sizes: dict[str, int] | None = None,
):
    """Input: manifests and optional size inputs. Output: DiscStats."""
    return compute_stats(
        manifests,
        control_raw or {},
        _info(used=used, remaining=remaining),
        _crypto,
        exact_sizes,
    )


def test_new_path_is_original():
    """
    Input:  one full blob for a new path
    Output: None
    Details: A first-session full blob is original, not changed_full.
    """
    manifests = [_make_manifest(0, entries=[_full("a.txt", 0, 100)])]
    stats = _report(manifests)
    assert stats.totals.original.files == 1
    assert stats.totals.original.nbytes == 100
    assert stats.totals.changed_full.files == 0


def test_live_path_full_blob_is_changed_full():
    """
    Input:  a later full blob for a path that is already live
    Output: None
    Details: Session 1 replaces a live path with a new full blob.
    """
    manifests = [
        _make_manifest(0, entries=[_full("a.txt", 0, 100)]),
        _make_manifest(1, entries=[_full("a.txt", 1, 80)]),
    ]
    stats = _report(manifests)
    assert stats.totals.original.files == 1
    assert stats.totals.changed_full.files == 1
    assert stats.totals.changed_full.nbytes == 80
    assert stats.sessions[1].changed_full.files == 1


def test_delta_blob_is_changed_delta():
    """
    Input:  a delta entry for a live path
    Output: None
    Details: A same-session-owned delta counts as changed_delta.
    """
    manifests = [
        _make_manifest(0, entries=[_full("a.txt", 0, 100)]),
        _make_manifest(1, entries=[_delta("a.txt", 1, 120, 30)]),
    ]
    stats = _report(manifests)
    assert stats.totals.changed_delta.files == 1
    assert stats.totals.changed_delta.nbytes == 30
    assert stats.sessions[1].changed_delta.files == 1


def test_cross_session_dedup_by_blob_path():
    """
    Input:  a later entry whose file path lives in session 0
    Output: None
    Details: Cross-session reuse is dedup with plaintext saved_bytes.
    """
    first = _full("a.txt", 0, 100, "session_000/full/aaa")
    reuse = _full("b.txt", 1, 100, "session_000/full/aaa")
    manifests = [
        _make_manifest(0, entries=[first]),
        _make_manifest(1, entries=[reuse]),
    ]
    stats = _report(manifests)
    assert stats.totals.dedup.files == 1
    assert stats.totals.dedup.saved_bytes == 100
    assert stats.sessions[1].dedup.files == 1
    assert stats.sessions[1].original.files == 0


def test_cross_session_dedup_by_origin_field():
    """
    Input:  blob_origin_session pointing at an earlier session
    Output: None
    Details: blob_origin_session set to another session is dedup.
    """
    manifests = [
        _make_manifest(0, entries=[_full("a.txt", 0, 50)]),
        _make_manifest(1, entries=[_full("b.txt", 1, 50, origin=0)]),
    ]
    stats = _report(manifests)
    assert stats.totals.dedup.files == 1
    assert stats.totals.dedup.saved_bytes == 50


def test_same_session_dedup_reuses_owned_blob():
    """
    Input:  two entries in one session that name the same blob path
    Output: None
    Details: The second entry is same-session dedup, not a second original.
    """
    blob = "session_000/full/shared"
    manifests = [_make_manifest(0, entries=[
        _full("a.txt", 0, 40, blob),
        _full("b.txt", 0, 40, blob),
    ])]
    stats = _report(manifests)
    assert stats.totals.original.files == 1
    assert stats.totals.dedup.files == 1
    assert stats.totals.dedup.saved_bytes == 40


def test_delete_then_readd_is_original():
    """
    Input:  delete in session 1, full blob for the same path in session 2
    Output: None
    Details: A path that is not live after a delete counts as original.
    """
    manifests = [
        _make_manifest(0, entries=[_full("a.txt", 0, 100)]),
        _make_manifest(1, deleted=["a.txt"]),
        _make_manifest(2, entries=[_full("a.txt", 2, 90)]),
    ]
    stats = _report(manifests)
    assert stats.sessions[1].deleted.files == 1
    assert stats.sessions[2].original.files == 1
    assert stats.sessions[2].changed_full.files == 0
    assert stats.totals.deleted.files == 1


def test_split_chunk_estimate_divides_full_size():
    """
    Input:  two volume-split entries that share one whole-file size
    Output: None
    Details: Estimate mode sizes each chunk as full_size_bytes / count.
    """
    entries = [
        _full("big.bin", 0, 1000, chunk=0, count=2),
        _full("big.bin", 0, 1000, blob="session_000/full/big_1", chunk=1, count=2),
    ]
    stats = _report([_make_manifest(0, entries=entries)])
    assert stats.approximate is True  # scbc ignore[is-true-false-strict] mode flag
    assert stats.totals.original.files == 2
    assert stats.totals.original.nbytes == 1000
    text_note = "Volume-split blob sizes are approximate."
    from oddarchiver.stats import render_text
    assert text_note in render_text(stats)


def test_suspect_manifest_entries_are_skipped():
    """
    Input:  a suspect session between two trusted sessions
    Output: None
    Details: Suspect entries do not change live paths or category counts.
    """
    suspect = _make_manifest(1, entries=[_full("a.txt", 1, 999)])
    suspect.suspect = True
    manifests = [
        _make_manifest(0, entries=[_full("a.txt", 0, 100)]),
        suspect,
        _make_manifest(2, entries=[_full("a.txt", 2, 80)]),
    ]
    stats = _report(manifests)
    assert stats.suspect_sessions == [1]
    assert stats.sessions[1].original.files == 0
    assert stats.sessions[2].changed_full.files == 1
    assert stats.totals.original.nbytes == 100


def test_exact_overhead_and_other_from_size_map():
    """
    Input:  fake list_file_sizes map with blob, control, and other files
    Output: None
    Details: Overhead is used minus data, control, and other. Clamp at 0.
    """
    blob = "session_000/full/a.txt"
    manifests = [_make_manifest(0, entries=[_full("a.txt", 0, 100, blob)])]
    exact = {
        blob: 100,
        "session_000/manifest.json": 40,
        "volume.json": 10,
        "TRANS.TBL": 7,
        ".lock": 3,
        "session_000/TRANS.TBL": 5,
    }
    stats = _report(manifests, used=200, exact_sizes=exact)
    assert stats.mode == "exact"
    assert stats.totals.original.nbytes == 100
    assert stats.totals.control == 50
    assert stats.totals.other == 15
    assert stats.sessions[0].control == 40
    assert stats.sessions[0].other == 5
    assert stats.totals.filesystem_overhead == 35
    negative = _report(manifests, used=10, exact_sizes=exact)
    assert negative.totals.filesystem_overhead == 0


def test_json_output_has_documented_keys():
    """
    Input:  a one-session report
    Output: None
    Details: render_json parses and holds the documented root and total keys.
    """
    manifests = [_make_manifest(0, entries=[_full("a.txt", 0, 100)])]
    payload = json.loads(render_json(_report(manifests, control_raw={
        "session_000/manifest.json": b"{}",
        "volume.json": b"{}",
    })))
    assert tuple(payload) == _JSON_ROOT
    assert tuple(payload["totals"]) == _JSON_TOTALS
    assert payload["mode"] == "estimate"
    assert payload["label"] == "TEST"
    session = payload["sessions"][0]
    for key in _JSON_TOTALS:
        assert key in session
    assert "files" in session["original"]
    assert "bytes" in session["original"]
    assert "saved_bytes" in session["dedup"]
    assert "files" in session["deleted"]
    assert "bytes" in session["control"]


def _init_iso(tmp_path):
    """Input: tmp_path. Output: source dir and ISO path after init."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello-stats")
    iso = tmp_path / "test.iso"
    rc = dispatch(_make_args(command="init", source=str(source), test_iso=str(iso)))
    assert rc == 0
    return source, iso


def test_status_stats_appends_tables(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details: status --stats prints normal status lines then estimate tables.
    """
    _source, iso = _init_iso(tmp_path)
    capsys.readouterr()
    args = build_parser().parse_args(["status", "--test-iso", str(iso), "--stats"])
    args.disc_size = "50mb"
    rc = dispatch(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Label:" in out
    assert "Disc usage (estimate):" in out
    assert "original:" in out
    idx_label = out.index("Label:")
    idx_stats = out.index("Disc usage (estimate):")
    assert idx_label < idx_stats


def test_status_stats_json_parses(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details: status --stats --json prints only a JSON object with documented keys.
    """
    _source, iso = _init_iso(tmp_path)
    capsys.readouterr()
    args = build_parser().parse_args(
        ["status", "--test-iso", str(iso), "--stats", "--json"],
    )
    args.disc_size = "50mb"
    rc = dispatch(args)
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.startswith("{")
    payload = json.loads(out)
    assert tuple(payload) == _JSON_ROOT
    assert payload["mode"] == "estimate"
    assert "Label:" not in out


def test_status_stats_exact_json_uses_iso_listing(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details: status --stats-exact --json calls ISOBackend.list_file_sizes.
    """
    _source, iso = _init_iso(tmp_path)
    capsys.readouterr()
    args = build_parser().parse_args(
        ["status", "--test-iso", str(iso), "--stats-exact", "--json"],
    )
    args.disc_size = "50mb"
    rc = dispatch(args)
    out = capsys.readouterr().out.strip()
    assert rc == 0
    payload = json.loads(out)
    assert payload["mode"] == "exact"
    assert payload["totals"]["original"]["files"] >= 1
    assert payload["totals"]["control"]["bytes"] > 0


def test_json_without_stats_exits_2(capsys):
    """
    Input:  capsys
    Output: None
    Details: --json without --stats or --stats-exact exits 2.
    """
    args = build_parser().parse_args(["status", "--json"])
    rc = dispatch(args)
    err = capsys.readouterr().err
    assert rc == 2
    assert "--json requires --stats or --stats-exact" in err


def test_stats_flags_are_mutually_exclusive():
    """
    Input:  none
    Output: None
    Details: --stats and --stats-exact together are a parse error.
    """
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["status", "--stats", "--stats-exact"])
    assert exc.value.code == 2
