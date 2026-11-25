"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Planner trust, disc-range, resume, and scaling behavior.
"""
# Imports
from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path

import pytest

from oddarchiver import cli
from oddarchiver.catalog import ArchiveCatalog, CatalogDisc, learn_disc, volume_for_session
from oddarchiver.crypto import KeyfileCrypto, NullCrypto, generate_keyfile
from oddarchiver.delta import compute_delta
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest
from oddarchiver.restore_plan import DiscAcquisition, restore_archive
from oddarchiver.restore_state import load_restore_state
from oddarchiver.volume import PathVersion, VolumeMarker
from tests.test_restore_plan import Acquisition, _source, _stage, _ID

# Globals
logger = logging.getLogger(__name__)
_DISC = 20 * 2**20
_CRYPTO = NullCrypto()


# Functions


def _manifest(session, entries=(), suspect=False):
    """Input: session and entries. Output: one authenticated test manifest."""
    return Manifest(
        version=2, session=session, timestamp=f"2026-10-15T00:00:{session:02d}Z",
        source="/src", label="VOL", based_on_session=session - 1 if session else None,
        encryption={"mode": "none"}, entries=list(entries), deleted=[],
        manifest_checksum="", suspect=suspect,
    )


def _entry(path, session, checksum):
    """Input: path and session. Output: one full manifest entry."""
    return ManifestEntry(
        path=path, type="full", result_checksum=checksum, full_size_bytes=1,
        file=f"session_{session:03d}/full/{hashlib.sha256(path.encode()).hexdigest()}",
    )


def test_f2_forges_out_of_run_collision_and_bad_blob_are_ignored():
    """F2 keeps only valid events and refuses a forged collision."""
    catalog = ArchiveCatalog(
        "f2", 0, 1, {}, {
            "same.txt": [PathVersion(1, 0, "full", "old", 1, "old", 1)],
        }, False,
    )
    marker = VolumeMarker("f2", 0, 0, "now", "")
    good = _entry("same.txt", 0, "good")
    forged = _entry("same.txt", 1, "forged")
    outside = _entry("outside.txt", 2, "outside")
    bad = ManifestEntry(
        path="bad.txt", type="full", result_checksum="bad", full_size_bytes=1,
        file="not-a-session/blob",
    )
    changed = learn_disc(catalog, marker, "VOL0", [
        _manifest(0, [good, bad]), _manifest(1, [forged]), _manifest(5, [outside]),
    ])
    assert changed == {"same.txt"}
    assert [(v.session, v.result_checksum, v.verified)
            for v in catalog.history["same.txt"]] == [(0, "good", True), (1, "old", True)]
    assert "outside.txt" not in catalog.history
    assert "bad.txt" not in catalog.history


def test_f2_keeps_encrypted_flag_and_range_index():
    """F2 preserves an encrypted record and resolves the learned range."""
    catalog = ArchiveCatalog(
        "f2-enc", 0, 0, {0: CatalogDisc(0, "OLD", 0, 0, encrypted=True)}, {}, False,
    )
    learn_disc(catalog, VolumeMarker("f2-enc", 0, 0, "now", ""), "NEW",
               [_manifest(0, [_entry("a.txt", 0, "a")])])
    assert catalog.discs[0].encrypted is True  # scbc ignore[is-true-false-strict] asserts an exact bool, not truthiness
    assert volume_for_session(catalog, 0) == 0


def test_f4_delta_base_requests_oldest_disc_forward_and_reverse(tmp_path):
    """F4 restores a cross-disc delta in either acquisition order."""
    original = b"base" * 1000
    latest = original[:-8] + b"V2-TAIL!"
    iso0, prior = _stage(tmp_path, 0, {"changed.txt": original}, _CRYPTO)
    iso1, prior = _stage(tmp_path, 1, {}, _CRYPTO, prior,
                         delta={"changed.txt": (original, original[:-4] + b"V1!")})
    iso2, _prior = _stage(tmp_path, 2, {}, _CRYPTO, prior,
                          delta={"changed.txt": (original[:-4] + b"V1!", latest)})
    discs = [iso0, iso1, iso2]
    for order in ([1, 0], [0, 1]):
        backend, catalog, manifests = _source(iso2)
        dest = tmp_path / ("forward" if order == [1, 0] else "reverse")
        result = restore_archive(
            dest, backend, _CRYPTO, catalog, manifests=manifests,
            acquisition=Acquisition(discs, order),
        )
        assert result.failures == 0
        assert (dest / "changed.txt").read_bytes() == latest


_OLD_ID = "f4-format-one"


def _stage_old_full_entries(staging, n, crypto, full):
    """
    Input: staging root, session, crypto, and full file bytes
    Output: full manifest entries
    """
    entries = []
    for path, content in (full or {}).items():
        rel = f"session_{n:03d}/full/{hashlib.sha256(path.encode()).hexdigest()}"
        (staging / rel).parent.mkdir(parents=True, exist_ok=True)
        (staging / rel).write_bytes(crypto.encrypt(content))
        entries.append(ManifestEntry(
            path=path, type="full", file=rel, full_size_bytes=len(content),
            result_checksum=hashlib.sha256(content).hexdigest(),
        ))
    return entries


def _stage_old_delta_entries(staging, tmp_path, n, crypto, delta):
    """
    Input: staging root, test directory, session, crypto, and delta file bytes
    Output: delta manifest entries
    """
    entries = []
    for path, (base, content) in (delta or {}).items():
        new_file = tmp_path / f"old-delta-source-{n}"
        new_file.write_bytes(content)
        difference = compute_delta(base, new_file)
        rel = f"session_{n:03d}/deltas/{hashlib.sha256(path.encode()).hexdigest()}"
        (staging / rel).parent.mkdir(parents=True, exist_ok=True)
        (staging / rel).write_bytes(crypto.encrypt(difference))
        entries.append(ManifestEntry(
            path=path, type="delta", delta_file=rel,
            delta_size_bytes=len(difference), full_size_bytes=len(content),
            result_checksum=hashlib.sha256(content).hexdigest(),
            source_checksum=hashlib.sha256(base).hexdigest(),
        ))
    return entries


def _old_layout_disc(tmp_path, n, crypto, *, full=None, delta=None, prior=None):
    """
    Input: tmp_path, disc and session number, crypto, files, prior v1 state
    Output: (ISO path, next v1 state and origin)
    Details: Write the 02d325d layout. Each session has manifest.enc and
        enc_mode.json. Discs 1 and later have a plaintext v1
        volume_baseline.json with only state and origin. volume.json has
        no format field.
    """
    staging = tmp_path / f"old-stage-{n}"
    session_dir = staging / f"session_{n:03d}"
    session_dir.mkdir(parents=True)
    state, origin = (dict(prior[0]), dict(prior[1])) if prior else ({}, {})
    entries = _stage_old_full_entries(staging, n, crypto, full)
    entries.extend(_stage_old_delta_entries(staging, tmp_path, n, crypto, delta))
    write_manifest(session_dir, Manifest(
        version=1, session=n, timestamp=f"2026-10-15T00:00:{n:02d}Z",
        source="/src", label=f"OLD{n}", based_on_session=n - 1 if n else None,
        encryption={"mode": "keyfile"}, entries=entries, deleted=[],
        manifest_checksum="",
    ), crypto)
    assert sorted(p.name for p in session_dir.iterdir() if p.is_file()) == [
        "enc_mode.json", "manifest.enc",
    ]
    (staging / "volume.json").write_text(json.dumps({
        "archive_id": _OLD_ID, "volume_index": n, "first_session": n,
        "created_at": "2026-10-15T00:00:00Z",
        "previous_volume_label": f"OLD{n - 1}" if n else "",
    }, indent=2), encoding="utf-8")
    if n:
        (staging / "volume_baseline.json").write_text(
            json.dumps({"state": state, "origin": origin}, indent=2), encoding="utf-8",
        )
    for entry in entries:
        state[entry.path] = entry.result_checksum
        origin[entry.path] = n
    iso = tmp_path / ("old.iso" if n == 0 else f"old.vol{n}.iso")
    ISOBackend(iso, disc_size=_DISC).init(staging, label=f"OLD{n}", expected_session_count=0)
    return iso, (state, origin)


class _Drive:
    """One drive whose disc changes on insert. It counts blob batches per disc."""

    def __init__(self, discs):
        """Input: ISO paths in disc order. Output: drive with the newest disc inserted."""
        self.discs = list(discs)
        self.blob_reads: list[int] = []
        self.insert(len(discs) - 1)

    def insert(self, index):
        """Input: disc index. Output: None. Details: Put this disc in the drive."""
        self.inserted = index
        self._inner = ISOBackend(self.discs[index], disc_size=_DISC)

    def read_paths(self, paths):
        """Input: disc paths. Output: found control bytes."""
        return self._inner.read_paths(paths)

    def read_path(self, path):
        """Input: one disc path. Output: its bytes."""
        return self._inner.read_path(path)

    def extract_paths(self, paths, dest_root, max_bytes=None):
        """Input: disc paths and target. Output: bytes written. Details: Count blob batches."""
        if any("/full/" in path or "/deltas/" in path for path in paths):
            self.blob_reads.append(self.inserted)
        return self._inner.extract_paths(paths, dest_root, max_bytes=max_bytes)

    def extract_path_to_file(self, path, dest):
        """Input: disc path and target file. Output: None."""
        return self._inner.extract_path_to_file(path, dest)

    def mediainfo(self):
        """Input: None. Output: DiscInfo of the inserted disc."""
        return self._inner.mediainfo()

    def list_session_numbers(self):
        """Input: None. Output: session numbers on the inserted disc."""
        return self._inner.list_session_numbers()


def _format_one_archive(tmp_path):
    """
    Input: tmp_path
    Output: disc paths, keyfile, newest changed.txt bytes, latest.txt bytes
    Details: changed.txt has a full base on disc 0 and deltas on discs 1
        and 2. latest.txt is a full file on disc 2.
    """
    keyfile = tmp_path / "old.key"
    generate_keyfile(str(keyfile))
    crypto = KeyfileCrypto(str(keyfile))
    original = b"format-one-base-" * 400
    middle = original[:-8] + b"V1-TAIL!"
    newest = middle[:-8] + b"V2-TAIL!"
    latest = b"latest full file on disc 2"
    iso0, prior = _old_layout_disc(tmp_path, 0, crypto, full={"changed.txt": original})
    iso1, prior = _old_layout_disc(
        tmp_path, 1, crypto, delta={"changed.txt": (original, middle)}, prior=prior,
    )
    iso2, _prior = _old_layout_disc(
        tmp_path, 2, crypto, full={"latest.txt": latest},
        delta={"changed.txt": (middle, newest)}, prior=prior,
    )
    baseline = json.loads((iso2.with_suffix(".d") / "volume_baseline.json").read_text())
    assert set(baseline) == {"state", "origin"}
    assert baseline["origin"] == {"changed.txt": 1}
    assert "format" not in json.loads((iso2.with_suffix(".d") / "volume.json").read_text())
    return [iso0, iso1, iso2], keyfile, newest, latest


def _cli_restore(tmp_path, monkeypatch, discs, keyfile, dest, answers):
    """
    Input: discs, keyfile, dest, list of (disc index, callable, or None, prompt answer)
    Output: (exit code, drive, prompt count)
    Details: Run restore --all-disks from disc 2 through the real CLI and
        the real drive prompt. Each prompt inserts the next disc first. A
        callable gets the drive and changes the disc itself.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    drive = _Drive(discs)
    monkeypatch.setattr(cli, "_make_backend", lambda _args: drive)
    queue = list(answers)
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        action, text = queue.pop(0)
        if callable(action):
            action(drive)
        elif action is not None:
            drive.insert(action)
        return text

    monkeypatch.setattr("builtins.input", answer)
    args = cli.build_parser().parse_args([
        "restore", str(dest), "--all-disks", "--all-files", "--key", str(keyfile),
    ])
    rc = cli.dispatch(args)
    assert queue == []
    return rc, drive, len(prompts)


@pytest.mark.parametrize("order", [[1, 0], [0, 1]], ids=["reverse", "forward"])
def test_f4_format_one_delta_base_from_cli(tmp_path, monkeypatch, capsys, order):
    """
    Input: 02d325d three-disc archive and an acquisition order
    Output: restore --all-disks restores the newest bytes from each disc once
    Details: The v1 baseline is only a hint. The planner asks for older
        discs until it learns the full base on disc 0.
    """
    discs, keyfile, newest, latest = _format_one_archive(tmp_path)
    dest = tmp_path / "out"
    rc, drive, prompts = _cli_restore(
        tmp_path, monkeypatch, discs, keyfile, dest,
        [(index, "") for index in order],
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert prompts == 2
    assert {p.name: p.read_bytes() for p in dest.iterdir()} == {
        "changed.txt": newest, "latest.txt": latest,
    }
    assert sorted(drive.blob_reads) == [0, 1, 2]
    assert "  missing:" not in captured.out
    assert "is missing" not in captured.err
    assert "Refuse this disc" not in captured.err


def test_f4_format_one_declared_missing_base(tmp_path, monkeypatch, capsys):
    """
    Input: 02d325d three-disc archive with disc 0 declared missing
    Output: changed.txt is reported missing and nothing wrong is written
    """
    discs, keyfile, _newest, latest = _format_one_archive(tmp_path)
    dest = tmp_path / "out"
    rc, drive, prompts = _cli_restore(
        tmp_path, monkeypatch, discs, keyfile, dest, [(1, ""), (None, "m 0")],
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert prompts == 2
    assert {p.name: p.read_bytes() for p in dest.iterdir()} == {"latest.txt": latest}
    assert captured.out.split("  missing:\n", 1)[1].split() == ["changed.txt"]
    assert "Disc 0 is missing." in captured.err
    assert 0 not in drive.blob_reads


def test_acquired_drive_disc_load_error_refuses_and_prompts_again(
    tmp_path, monkeypatch, capsys,
):
    """
    Input: 02d325d archive. The first copy of disc 1 has a plaintext manifest
    Output: the restore refuses that disc, asks again, and finishes
    Details: A load error on an acquired disc refuses only that disc. A
        drive does not mark the disc missing.
    """
    discs, keyfile, newest, latest = _format_one_archive(tmp_path)
    forged = discs[1].with_suffix(".d") / "session_001" / "manifest.json"

    def insert_forged(drive):
        """Input: drive. Output: None. Details: Insert disc 1 with a forged manifest."""
        forged.write_text("{}", encoding="utf-8")
        drive.insert(1)

    def insert_good(drive):
        """Input: drive. Output: None. Details: Insert a good copy of disc 1."""
        forged.unlink()
        drive.insert(1)

    dest = tmp_path / "out"
    rc, drive, prompts = _cli_restore(
        tmp_path, monkeypatch, discs, keyfile, dest,
        [(insert_forged, ""), (insert_good, ""), (0, "")],
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert prompts == 3
    assert (
        "Refuse this disc (disc 1): This encrypted disc has a plaintext "
        "session_001/manifest.json. Do not trust this disc."
    ) in captured.err
    assert "is missing" not in captured.err
    assert "  missing:" not in captured.out
    assert sorted(drive.blob_reads) == [0, 1, 2]
    assert {p.name: p.read_bytes() for p in dest.iterdir()} == {
        "changed.txt": newest, "latest.txt": latest,
    }


def test_f6_tampered_dest_after_newer_restore_repeats_entry(tmp_path):
    """F6 drops a newer skip when the destination checksum is wrong.

    Restore checks disc 0 manifests once even though no file needs it.
    """
    iso0, prior = _stage(tmp_path, 0, {"a.txt": b"old"}, _CRYPTO)
    iso1, _prior = _stage(tmp_path, 1, {"a.txt": b"new"}, _CRYPTO, prior)
    discs = [iso0, iso1]
    backend, catalog, manifests = _source(iso1)
    dest = tmp_path / "tampered"
    first = Acquisition(discs, [0])
    assert restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests, acquisition=first,
    ).failures == 0
    assert first.calls == [{0: 0}]
    (dest / "a.txt").write_bytes(b"tampered")
    backend, catalog, manifests = _source(iso1)
    second = Acquisition(discs, [])
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests, acquisition=second,
    )
    assert second.calls == []  # saved state already lists disc 0 as checked
    assert result.failures == 0
    assert (dest / "a.txt").read_bytes() == b"new"


def test_f10_range_memory_requires_authenticated_slots(tmp_path, monkeypatch):
    """F10 persists only a marker-aligned manifest run."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from oddarchiver.catalog import archive_memory_path, remember_inserted_disc
    marker = VolumeMarker("f10", 3, 3, "now", "OLD")
    remember_inserted_disc(marker, "VOL3", [
        _manifest(4, []), _manifest(5, []),
    ])
    assert not archive_memory_path("f10").exists()
    remember_inserted_disc(marker, "VOL3", [
        _manifest(3, []), _manifest(4, []),
    ])
    stored = archive_memory_path("f10").read_text()
    assert '"first_session": 3' in stored
    assert '"last_session": 4' in stored


def test_e2_session_index_resolves_large_catalog():
    """E2 uses the session range index instead of scanning all histories."""
    catalog = ArchiveCatalog("scale", 49, 0, {}, {}, False)
    for index in range(50):
        catalog.discs[index] = CatalogDisc(index, f"D{index}", index * 2, index * 2 + 1)
    for number in range(100_000):
        catalog.history[f"f{number}.bin"] = [
            PathVersion(0, 0, "full", "x", 1, "t", 0),
        ]
    assert volume_for_session(catalog, 77) == 38
    assert volume_for_session(catalog, 98) == 49
    assert volume_for_session(catalog, 100) is None


def _rename_iso(iso, name):
    """Input: test ISO and new name. Output: renamed ISO with its session tree and meta file."""
    target = iso.with_name(name)
    iso.rename(target)
    iso.with_suffix(".d").rename(target.with_suffix(".d"))
    meta = iso.with_name(iso.name + ".meta.json")
    if meta.exists():
        meta.rename(target.with_name(target.name + ".meta.json"))
    return target


def test_e3_siblings_are_discovered_once_per_restore(tmp_path, monkeypatch):
    """E3 reuses the sibling marker mapping across acquisition calls."""
    iso0, prior = _stage(tmp_path, 0, {"a.txt": b"old"}, _CRYPTO)
    iso1, _prior = _stage(
        tmp_path, 1, {}, _CRYPTO, prior,
        delta={"a.txt": (b"old", b"new")},
    )
    first = _rename_iso(iso0, "archive.iso")
    last = _rename_iso(iso1, "archive.vol1.iso")
    backend, catalog, manifests = _source(last)
    calls = []
    original = ISOBackend.__init__
    def tracked(self, iso_path, disc_size=None):
        calls.append(Path(iso_path).name)
        original(self, iso_path, disc_size)
    monkeypatch.setattr(ISOBackend, "__init__", tracked)
    acquisition = DiscAcquisition(backend, image=last)
    result = restore_archive(
        tmp_path / "siblings", backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    assert result.failures == 0
    assert (tmp_path / "siblings" / "a.txt").read_bytes() == b"new"
    assert set(acquisition._siblings) == {0, 1}
    assert calls.count(first.name) == 1


def test_acquired_disc_invalidates_cached_session_list():
    """A disc swap must drop the backend's cached root listing before reading."""
    from oddarchiver.restore_plan import _read_acquired_manifests
    from oddarchiver.volume import VolumeMarker

    class _StaleBackend:
        """Backend that fails unless the session listing cache is invalidated."""

        def __init__(self) -> None:
            self.fresh = False

        def invalidate_session_list(self) -> None:
            self.fresh = True

        def list_session_numbers(self) -> list[int]:
            if not self.fresh:
                raise AssertionError("stale session listing was used")
            return []

        def read_paths(self, paths):
            return {}

    marker = VolumeMarker(
        archive_id="a", volume_index=0, first_session=0,
        created_at="2026-01-01T00:00:00Z", previous_volume_label="",
    )
    catalog = ArchiveCatalog(
        archive_id="a", source_volume_index=0, last_session=0,
        discs={}, history={}, complete=True,
    )
    manifests, refusal = _read_acquired_manifests(
        _StaleBackend(), NullCrypto(), catalog, marker, 0, None,
    )
    assert refusal is None
    assert manifests == []


def test_requested_for_disc_keeps_every_chunk_in_one_session():
    """Two chunks of one file in one session must both be requested from that disc."""
    from oddarchiver.manifest import Manifest
    from oddarchiver.restore_plan import _requested_for_disc
    from oddarchiver.volume import PathVersion

    entries = [
        ManifestEntry(
            path="big.bin", type="full", result_checksum="x",
            full_size_bytes=100, file=f"session_000/full/blob{chunk}",
            volume_chunk=chunk, volume_chunk_count=2, blob_origin_session=0,
        )
        for chunk in (0, 1)
    ]
    manifest = Manifest(
        version=2, session=0, timestamp="t", source="s", label="L",
        based_on_session=None, encryption={}, entries=entries,
        deleted=[], manifest_checksum="",
    )
    chain = [
        PathVersion(
            session=0, volume_index=0, kind="full", result_checksum="x",
            full_size_bytes=100, timestamp="t", blob_session=0,
            volume_chunk=chunk, volume_chunk_count=2,
        )
        for chunk in (0, 1)
    ]
    catalog = ArchiveCatalog(
        archive_id="a", source_volume_index=0, last_session=0,
        discs={}, history={}, complete=True,
    )
    requested, bad = _requested_for_disc(catalog, {"big.bin": chain}, [manifest], 0)
    assert bad == set()
    assert [entry.volume_chunk for _p, _s, entry in requested] == [0, 1]
