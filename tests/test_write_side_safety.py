"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Regression tests for packing baseline reservation, dry-run blob
    errors, staging ciphertext on disk, listing side effects, disc-swap
    identity, and init baseline reservation.
"""
# Imports
import argparse
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.cli import _run_browse, _run_dry_run, _run_ls, dispatch
from oddarchiver.crypto import KeyfileCrypto, NullCrypto, generate_keyfile
from oddarchiver.disc import DiscInfo, ISOBackend
from oddarchiver.manifest import load_session_manifests, read_manifest
from oddarchiver.session import build_staging
from oddarchiver.volume import (
    PathVersion, VolumeBaseline, VolumeMarker, VolumeRecord,
    read_volume_baseline, read_volume_marker, write_volume_baseline,
)
import oddarchiver.session as session_mod

# Globals
logger = logging.getLogger(__name__)
_log = logger
_DISC = 12 * 1024 * 1024
_HISTORY = 2500


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
    monkeypatch.setattr(
        "oddarchiver.cli.CacheManager", lambda *a, **k: CacheManager(cache),
    )
    monkeypatch.setattr("oddarchiver.ledger.ledger_dir", lambda: tmp_path / "ledger")
    (tmp_path / "ledger").mkdir()


def _make_args(**kwargs) -> argparse.Namespace:
    """Input: field overrides. Output: Namespace for init and sync tests."""
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "SAFE",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "8mb",
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
        "mirror": None,
        "recover_scan": False,
        "paths": None,
        "show_deleted": False,
        "path": None,
        "rescue_key": None,
        "skip_volumes": None,
        "all_disks": False,
        "embed_software": None,
        "skip_large": False,
        "slow": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _history_baseline(n: int) -> VolumeBaseline:
    """Input: n path versions. Output: A version-2 baseline of that length."""
    history = {}
    state = {}
    origin = {}
    checksum = "ab" * 32
    for i in range(n):
        path = f"hist/{i:04d}.txt"
        history[path] = [PathVersion(
            0, 0, "full", checksum, 1, "2026-01-01T00:00:00Z", 0,
        )]
        state[path] = checksum
        origin[path] = 0
    return VolumeBaseline(
        2, [VolumeRecord(0, "OLD", 0, 0)], history, state, origin,
    )


def _full_backend(disc_size: int, label: str = "SAFE") -> MagicMock:
    """Input: disc size. Output: Backend with no remaining space."""
    backend = MagicMock()
    backend.disc_size = disc_size
    backend.mediainfo.return_value = DiscInfo(
        session_count=1, remaining_bytes=0, used_bytes=disc_size, label=label,
    )
    backend.read_paths.return_value = {}
    return backend


def _assert_new_disc_sizes(chunks, baseline: VolumeBaseline, crypto: KeyfileCrypto,
                           tmp_path: Path) -> None:
    """Input: staged chunks, baseline, crypto, and output root. Output: None."""
    sizes = []
    for staging, session_n, is_new in chunks:
        if not is_new:
            continue
        write_volume_baseline(staging, baseline, crypto)
        iso = tmp_path / f"hist-{session_n:03d}.iso"
        ISOBackend(iso, disc_size=_DISC).init(staging, label="SAFE")
        size = iso.stat().st_size
        sizes.append(size)
        assert size <= _DISC, f"{iso.name} is {size} bytes; disc_size is {_DISC}"
    assert sizes


def test_new_disc_iso_fits_long_encrypted_history(tmp_path, monkeypatch):
    """
    Input: Thousands of path versions and a small encrypted disc
    Output: Every produced ISO stays within disc_size
    """
    keyfile = tmp_path / "test.key"
    generate_keyfile(str(keyfile))
    crypto = KeyfileCrypto(str(keyfile))
    source = tmp_path / "src"
    source.mkdir()
    for i in range(18):
        (source / f"f{i:02d}.bin").write_bytes(bytes([i]) * (400 * 1024))
    baseline = _history_baseline(_HISTORY)
    marker = VolumeMarker("hist-archive", 0, 0, "now", "")
    captured = []
    budgets = []
    orig_pack = session_mod._pack_blobs

    def spy(blobs, first, full):
        captured.append(list(blobs))
        budgets.append(full)
        return orig_pack(blobs, first, full)

    monkeypatch.setattr(session_mod, "_pack_blobs", spy)
    chunks = build_staging(
        session_n=1,
        source=source,
        disc_state={},
        disc_origin={},
        backend=_full_backend(_DISC),
        cache=MagicMock(),
        crypto=crypto,
        _staging_root=tmp_path / "staging",
        volume_baseline=baseline,
        volume_marker=marker,
        volume_label="SAFE",
    )
    try:
        assert captured
        assert budgets
        assert budgets[0] < session_mod._volume_budget(_DISC)
        for item in captured[0]:
            assert item.blob == b""
        _assert_new_disc_sizes(chunks, baseline, crypto, tmp_path)
    finally:
        import shutil
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def _assert_prepared_blobs(captured) -> None:
    """Input: captured prepared blobs. Output: None."""
    for item in captured[0]:
        assert item.blob == b""
        if item.encoded_size:
            assert item.blob_path is not None


def _assert_scratch_blobs(captured, chunks, backend: ISOBackend,
                          iso: Path) -> None:
    """Input: prepared blobs, staged chunks, backend, and ISO path. Output: None."""
    _assert_prepared_blobs(captured)
    staging = chunks[0][0]
    manifest = read_manifest(staging / "session_000" / "manifest.json")
    blobs = {}
    for entry in manifest.entries:
        blobs[entry.path] = (staging / entry.file).read_bytes()
    assert blobs["a.txt"] == b"alpha-bytes"
    assert blobs["b.txt"] == b"beta-bytes!!"
    backend.init(staging, label="SAFE")
    burned = ISOBackend(iso, disc_size=50 * 2**20)
    for path, data in blobs.items():
        entry = next(e for e in manifest.entries if e.path == path)
        assert burned.read_path(entry.file) == data


def test_ordinary_blobs_leave_scratch_not_memory(tmp_path, monkeypatch):
    """
    Input: Two small new files and NullCrypto
    Output: Prepared items hold no ciphertext. Disc blobs match source bytes.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"alpha-bytes")
    (source / "b.txt").write_bytes(b"beta-bytes!!")
    captured = []
    orig_pack = session_mod._pack_blobs

    def spy(blobs, first, full):
        captured.append(list(blobs))
        return orig_pack(blobs, first, full)

    monkeypatch.setattr(session_mod, "_pack_blobs", spy)
    iso = tmp_path / "plain.iso"
    backend = ISOBackend(iso, disc_size=50 * 2**20)
    chunks = build_staging(
        session_n=0,
        source=source,
        disc_state={},
        disc_origin={},
        backend=backend,
        cache=MagicMock(),
        crypto=NullCrypto(),
        _staging_root=tmp_path / "staging",
    )
    try:
        assert captured
        _assert_scratch_blobs(captured, chunks, backend, iso)
    finally:
        import shutil
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def test_repeat_staging_keeps_content_but_changes_blob_id(tmp_path):
    """
    Input: The same source and NullCrypto twice
    Output: Matching content and checksums but different random blob IDs
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "same.txt").write_bytes(b"stable-payload")
    results = []
    for i in range(2):
        chunks = build_staging(
            session_n=0,
            source=source,
            disc_state={},
            disc_origin={},
            backend=ISOBackend(tmp_path / f"r{i}.iso", disc_size=50 * 2**20),
            cache=MagicMock(),
            crypto=NullCrypto(),
            _staging_root=tmp_path / f"staging{i}",
        )
        staging = chunks[0][0]
        manifest = read_manifest(staging / "session_000" / "manifest.json")
        blob_map = {
            entry.path: (staging / entry.file).read_bytes()
            for entry in manifest.entries
        }
        fields = [
            (e.path, e.type, e.result_checksum, e.full_size_bytes)
            for e in manifest.entries
        ]
        results.append((blob_map, fields, [e.file for e in manifest.entries]))
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
    assert results[0][0] == results[1][0]
    assert results[0][1] == results[1][1]
    assert results[0][2] != results[1][2]


def test_dry_run_prior_blob_error_names_path(tmp_path, monkeypatch, capsys):
    """
    Input: A changed file whose prior blob cannot be read
    Output: Exit code 1 and the path in the error
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "changed.txt").write_bytes(b"version-one")
    iso = tmp_path / "dry.iso"
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso), disc_size="50mb",
    ))
    assert rc == 0
    (source / "changed.txt").write_bytes(b"version-two-is-longer")

    def boom(*args, **kwargs):
        raise RuntimeError("decrypt failed for session_000/full/deadbeef")

    monkeypatch.setattr(
        "oddarchiver.cache.CacheManager.get_with_fallback", boom,
    )
    args = _make_args(
        command="sync", source=str(source), test_iso=str(iso), disc_size="50mb",
        dry_run=True,
    )
    rc = _run_dry_run(args, is_init=False)
    captured = capsys.readouterr()
    assert rc == 1
    assert "changed.txt" in captured.err
    assert "decrypt failed" in captured.err
    assert "treating as full" not in captured.err
    assert "treating as full" not in captured.out


def test_dry_run_wrong_key_exits_nonzero(tmp_path, capsys):
    """
    Input: An encrypted archive and a second keyfile
    Output: Nonzero exit, and stderr names the manifest and --key
    Details:
        The wrong key fails on the session manifest before any file blob
        is read, so the error names the manifest, not secret.txt.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "secret.txt").write_bytes(b"classified")
    iso = tmp_path / "enc.iso"
    keyfile = tmp_path / "right.key"
    generate_keyfile(str(keyfile))
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        encrypt="keyfile", key=str(keyfile), disc_size="50mb",
    ))
    assert rc == 0
    (source / "secret.txt").write_bytes(b"classified-and-changed")
    wrong = tmp_path / "wrong.key"
    generate_keyfile(str(wrong))
    args = _make_args(
        command="sync", source=str(source), test_iso=str(iso),
        key=str(wrong), disc_size="50mb", dry_run=True,
    )
    rc = _run_dry_run(args, is_init=False)
    captured = capsys.readouterr()
    assert rc != 0
    assert "manifest" in captured.err
    assert "--key" in captured.err


def test_ls_all_disks_reads_volume_json_and_mediainfo_once(
    tmp_path, monkeypatch, capsys,
):
    """
    Input: A burned ISO listed with --all-disks
    Output: One volume.json read and one mediainfo call
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"one")
    iso = tmp_path / "once.iso"
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso), disc_size="50mb",
        label="ONCE",
    ))
    assert rc == 0
    reads = []
    infos = []
    orig_read = ISOBackend.read_paths
    orig_info = ISOBackend.mediainfo

    def counting_read(self, paths):
        reads.extend(paths)
        return orig_read(self, paths)

    def counting_info(self):
        infos.append(1)
        return orig_info(self)

    monkeypatch.setattr(ISOBackend, "read_paths", counting_read)
    monkeypatch.setattr(ISOBackend, "mediainfo", counting_info)
    args = _make_args(
        command="ls", test_iso=str(iso), all_disks=True, disc_size="50mb",
        label="ONCE",
    )
    assert _run_ls(args) == 0
    capsys.readouterr()
    assert reads.count("volume.json") == 1
    assert len(infos) == 1


def _build_split_volume(tmp_path: Path, encrypt: str) -> tuple[Path, str | None, bytes]:
    """Input: temporary root and encryption mode. Output: second ISO, key, baseline."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * 80_000)
    (source / "b.bin").write_bytes(b"B" * 80_000)
    iso = tmp_path / "split.iso"
    key = None
    if encrypt == "keyfile":
        key = tmp_path / "archive.key"
        generate_keyfile(str(key))
        key = str(key)
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size="100000", label="SPLIT", encrypt=encrypt, key=key,
    ))
    assert rc == 0
    vol1 = tmp_path / "split.vol1.iso"
    assert vol1.is_file()
    baseline_name = (
        "volume_baseline.enc" if encrypt == "keyfile" else "volume_baseline.json"
    )
    other_name = (
        "volume_baseline.json" if encrypt == "keyfile" else "volume_baseline.enc"
    )
    on_disc = ISOBackend(vol1, disc_size=100_000).read_paths(
        [baseline_name, other_name],
    )
    assert set(on_disc) == {baseline_name}
    return vol1, key, on_disc[baseline_name]


def _build_baseline_spies(monkeypatch, baseline_bytes: bytes):
    """Input: monkeypatch and baseline bytes. Output: four tracked call lists."""
    import oddarchiver.catalog as catalog_mod
    import oddarchiver.volume as volume_mod

    calls: list[list[str]] = []
    orig_read = ISOBackend.read_paths

    def counting_read(self, paths):
        calls.append(list(paths))
        return orig_read(self, paths)

    baseline_reads = []
    orig_baseline = volume_mod.read_volume_baseline

    def counting_baseline(*args, **kwargs):
        baseline_reads.append(args)
        return orig_baseline(*args, **kwargs)

    baseline_decrypts = []
    orig_decrypt = KeyfileCrypto.decrypt

    def counting_decrypt(self, data):
        if data == baseline_bytes:
            baseline_decrypts.append(data)
        return orig_decrypt(self, data)

    baseline_parses = []
    orig_loads = json.loads
    baseline_text = baseline_bytes.decode("utf-8", errors="replace")

    def counting_loads(data, *args, **kwargs):
        if data == baseline_bytes or data == baseline_text:
            baseline_parses.append(data)
        return orig_loads(data, *args, **kwargs)

    monkeypatch.setattr(ISOBackend, "read_paths", counting_read)
    monkeypatch.setattr(volume_mod, "read_volume_baseline", counting_baseline)
    monkeypatch.setattr(catalog_mod, "read_volume_baseline", counting_baseline)
    monkeypatch.setattr(KeyfileCrypto, "decrypt", counting_decrypt)
    monkeypatch.setattr(json, "loads", counting_loads)
    return calls, baseline_reads, baseline_decrypts, baseline_parses


@pytest.mark.parametrize("encrypt", ["none", "keyfile"])
def test_ls_without_all_disks_skips_baseline(tmp_path, monkeypatch, capsys, encrypt):
    """
    Input:  A two-volume archive listed without --all-disks, plaintext
            or keyfile encrypted
    Output: None
    Details:
        The unified control batch (F8) reads both baseline names and
        volume.json in one read_paths call. ls without --all-disks makes
        only that one call. It never calls read_volume_baseline, never
        decrypts the baseline bytes, and never parses the baseline JSON.
    """
    vol1, key, baseline_bytes = _build_split_volume(tmp_path, encrypt)
    calls, baseline_reads, baseline_decrypts, baseline_parses = (
        _build_baseline_spies(monkeypatch, baseline_bytes)
    )
    args = _make_args(
        command="ls", test_iso=str(vol1), all_disks=False, disc_size="100000",
        label="SPLIT", key=key,
    )
    assert _run_ls(args) == 0
    out = capsys.readouterr().out
    assert "b.bin" in out
    assert len(calls) == 1, calls
    assert {"volume_baseline.enc", "volume_baseline.json", "volume.json"} <= set(calls[0])
    assert baseline_reads == []
    assert baseline_decrypts == []
    assert baseline_parses == []


class _SwapBackend:
    """Serve one of several ISO backends, as if the same drive changed discs."""

    def __init__(self, discs: list[ISOBackend]):
        self._discs = discs
        self.index = len(discs) - 1
        self.volume_json_reads = 0

    def swap(self, index: int) -> None:
        """Input: disc index. Output: None. Details: Point later reads at that disc."""
        self.index = index

    @property
    def current(self) -> ISOBackend:
        """Input: None. Output: The ISO currently treated as inserted."""
        return self._discs[self.index]

    def read_paths(self, paths):
        """Input: disc paths. Output: bytes from the inserted disc."""
        names = list(paths)
        if "volume.json" in names:
            self.volume_json_reads += 1
        return self.current.read_paths(paths)
    def extract_paths(self, paths, dest_root, max_bytes=None):
        """Input: disc paths and target. Output: bytes copied from the inserted disc."""
        return self.current.extract_paths(paths, dest_root, max_bytes=max_bytes)


    def read_path(self, path: str) -> bytes:
        """Input: disc path. Output: bytes from the inserted disc."""
        return self.current.read_path(path)

    def extract_path_to_file(self, path: str, dest: Path) -> None:
        """Input: disc path and dest. Output: None. Details: Copy from the inserted disc."""
        return self.current.extract_path_to_file(path, dest)

    def mediainfo(self):
        """Input: None. Output: Media info of the inserted disc."""
        return self.current.mediainfo()

    def list_session_numbers(self):
        """Input: None. Output: Session numbers on the inserted disc."""
        return self.current.list_session_numbers()


def test_browse_all_disks_identifies_swapped_disc_on_same_backend(
    tmp_path, monkeypatch, capsys,
):
    """
    Input: Two-volume archive whose volume.json changes between reads
    Output: browse --all-disks r restores the file from the newly inserted disc
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * 80_000)
    (source / "b.bin").write_bytes(b"B" * 80_000)
    iso = tmp_path / "swap.iso"
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size="100000", label="SWAP",
    ))
    assert rc == 0
    vol1 = tmp_path / "swap.vol1.iso"
    assert vol1.is_file()
    discs = [
        ISOBackend(iso, disc_size=100_000),
        ISOBackend(vol1, disc_size=100_000),
    ]
    backend = _SwapBackend(discs)
    dest = tmp_path / "out"
    answers = iter(["1", "r", str(dest)])

    def fake_input(prompt=""):
        if prompt.strip() == ">":
            backend.swap(0)
            return ""
        return next(answers)

    monkeypatch.setattr("oddarchiver.cli._make_backend", lambda args: backend)
    monkeypatch.setattr("builtins.input", fake_input)
    args = _make_args(
        command="browse", all_disks=True, test_iso=None, disc_size="100000",
        label="SWAP",
    )
    rc = _run_browse(args)
    capsys.readouterr()
    assert rc == 0
    assert (dest / "a.bin").read_bytes() == b"A" * 80_000
    assert backend.volume_json_reads >= 2


def _assert_spill_manifest(backend: ISOBackend, marker: VolumeMarker,
                           index: int) -> None:
    """Input: disc backend, marker, and index. Output: None."""
    manifests = load_session_manifests(
        backend, None, start_session=marker.first_session, marker=marker,
    )
    assert manifests
    assert all(m.version == 3 for m in manifests)
    if index >= 1:
        baseline = read_volume_baseline(
            backend, None, marker=marker, manifests=manifests,
        )
        assert baseline is not None
        assert baseline.version == 2


def _assert_spill_discs(isos: list[Path], disc_size: int) -> None:
    """Input: spilled ISO paths and capacity. Output: None."""
    assert len(isos) >= 3
    for index, path in enumerate(isos):
        size = path.stat().st_size
        assert size <= disc_size, (
            f"{path.name} is {size} bytes; disc_size is {disc_size}"
        )
        backend = ISOBackend(path, disc_size=disc_size)
        marker = read_volume_marker(backend)
        assert marker is not None
        assert marker.format == 2
        assert json.loads((path.with_suffix(".d") / "volume.json").read_bytes())["format"] == 2
        _assert_spill_manifest(backend, marker, index)


def test_init_spill_reserves_baseline_on_every_new_disc(tmp_path, monkeypatch):
    """
    Input: Init of many small files that spill across at least 3 discs
    Output: New-disc packing budget shrinks. Disc 1+ has a readable baseline.
    """
    disc_size = 8 * 1024 * 1024
    source = tmp_path / "src"
    source.mkdir()
    for i in range(40):
        (source / f"f{i:02d}.bin").write_bytes(bytes([i % 256]) * (400 * 1024))
    budgets = []
    orig_pack = session_mod._pack_blobs

    def spy(blobs, first, full):
        budgets.append(full)
        return orig_pack(blobs, first, full)

    monkeypatch.setattr(session_mod, "_pack_blobs", spy)
    iso = tmp_path / "spill.iso"
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size=str(disc_size), label="SPILL",
    ))
    assert rc == 0
    assert budgets
    assert budgets[0] < session_mod._volume_budget(disc_size)
    isos = [iso]
    n = 1
    while True:
        extra = tmp_path / f"spill.vol{n}.iso"
        if not extra.is_file():
            break
        isos.append(extra)
        n += 1
    _assert_spill_discs(isos, disc_size)

