"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Regression tests for on-disc format 2 trust decisions. Plaintext
    metadata is refused on encrypted format 2 discs. Legacy format 1
    plaintext metadata is accepted with a warning. Baseline read errors
    stop sync before any burn. enc_mode.json and blob paths are
    validated. Control-file discovery reads far fewer paths.
"""
# Imports
import dataclasses
import json
import logging
from pathlib import Path

import pytest

from oddarchiver.crypto import KeyfileCrypto, generate_keyfile
from oddarchiver.cli import _crypto_for_disc, dispatch
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import (
    Manifest,
    load_session_manifests,
    write_manifest,
)
from oddarchiver.volume import (
    BaselineReadError,
    VolumeBaseline,
    VolumeMarker,
    build_volume_baseline,
    parse_blob_session,
    read_volume_baseline,
    read_volume_marker,
    write_volume_baseline,
    write_volume_marker,
)

# Globals
_SMALL_DISC = 10 * 2**20
_TS = "2026-04-19T12:00:00Z"
logger = logging.getLogger(__name__)
_log = logger


# Functions


def _isolate(tmp_path: Path, monkeypatch) -> None:
    """Point cache and ledger at tmp_path so tests do not share home state."""
    from oddarchiver.cache import CacheManager
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "oddarchiver.cli.CacheManager",
        lambda *a, **k: CacheManager(cache_dir),
    )
    ledger = tmp_path / "ledger"
    ledger.mkdir(exist_ok=True)
    monkeypatch.setattr("oddarchiver.ledger.ledger_dir", lambda: ledger)


def _manifest(session: int, version: int, encrypted_mode: str) -> Manifest:
    """Build a valid manifest for a crafted disc."""
    return Manifest(
        version=version,
        session=session,
        timestamp=_TS,
        source="/src",
        label="FMT2",
        based_on_session=None if session == 0 else session - 1,
        encryption={"mode": encrypted_mode},
        entries=[],
        deleted=[],
        manifest_checksum="",
    )


def _keyfile(tmp_path: Path) -> tuple[KeyfileCrypto, Path]:
    """Generate a keyfile and its crypto backend."""
    keyfile = tmp_path / "k.key"
    generate_keyfile(str(keyfile))
    return KeyfileCrypto(str(keyfile)), keyfile


def _write_disc_marker(staging: Path, marker_format: int, strip_format: bool) -> None:
    """Input: staging, marker_format, strip_format. Output: None."""
    if strip_format:
        raw = dataclasses.asdict(
            VolumeMarker("arch", 1, 0, _TS, "", format=marker_format),
        )
        raw.pop("format")
        (staging / "volume.json").write_text(json.dumps(raw), encoding="utf-8")
    else:
        write_volume_marker(
            staging, VolumeMarker("arch", 1, 0, _TS, "", format=marker_format),
        )


def _write_disc_baselines(
    staging: Path, baseline: str, baseline_version: int, crypto: KeyfileCrypto,
) -> None:
    """Input: staging, baseline, baseline_version, crypto. Output: None."""
    if baseline in ("enc", "both"):
        write_volume_baseline(
            staging, VolumeBaseline(baseline_version, [], {}, {}, {}),
            crypto,
        )
    if baseline in ("json", "both"):
        write_volume_baseline(
            staging, VolumeBaseline(baseline_version, [], {}, {}, {}), None,
        )


def _build_disc(
    tmp_path: Path,
    *,
    marker_format: int = 2,
    strip_format: bool = False,
    manifest_on_disc: str = "enc",
    manifest_version: int = 2,
    add_plaintext_manifest: bool = False,
    baseline: str = "none",
    baseline_version: int = 2,
    enc_mode: str | None = "keyfile",
    bad_enc_mode: bool = False,
) -> tuple[Path, KeyfileCrypto | None, Path | None]:
    """
    Build a crafted single-session ISO and return (iso, crypto, keyfile).

    manifest_on_disc: "enc" or "json". baseline: "none", "enc", "json",
    or "both". enc_mode: "keyfile", "none", or None for no enc_mode.json.
    """
    staging = tmp_path / "staging"
    sdir = staging / "session_000"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "full").mkdir(exist_ok=True)
    (sdir / "full" / ("0" * 64)).write_bytes(b"blob")

    crypto, keyfile = _keyfile(tmp_path)

    write_manifest(
        sdir,
        _manifest(0, manifest_version, "keyfile"),
        crypto if manifest_on_disc == "enc" else None,
    )
    if add_plaintext_manifest:
        scratch = tmp_path / "plain_scratch"
        scratch.mkdir(exist_ok=True)
        write_manifest(scratch, _manifest(0, 1, "none"), None)
        (sdir / "manifest.json").write_bytes((scratch / "manifest.json").read_bytes())

    if bad_enc_mode:
        (sdir / "enc_mode.json").write_text('{"mode": "bogus"}', encoding="utf-8")
    elif enc_mode is None:
        (sdir / "enc_mode.json").unlink(missing_ok=True)
    elif enc_mode in ("none", "keyfile"):
        (sdir / "enc_mode.json").write_text(
            json.dumps({"mode": enc_mode}), encoding="utf-8",
        )

    _write_disc_marker(staging, marker_format, strip_format)
    _write_disc_baselines(staging, baseline, baseline_version, crypto)

    iso = tmp_path / "disc.iso"
    ISOBackend(iso, disc_size=_SMALL_DISC).init(staging, label="FMT2")
    return iso, crypto, keyfile


# Tests



def test_new_disc_marker_carries_format2(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        init writes "format": 2 into volume.json on the new disc. The
        session manifest has version 3. An old volume.json without the
        field reads as format 1.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"alpha")
    iso = tmp_path / "n.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
    )) == 0
    marker_file = iso.with_suffix(".d") / "volume.json"
    assert json.loads(marker_file.read_bytes())["format"] == 2
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    assert read_volume_marker(backend).format == 2
    manifests = load_session_manifests(backend, None)
    assert manifests
    assert all(m.version == 3 for m in manifests)
    raw = json.loads(marker_file.read_bytes())
    raw.pop("format")
    marker_file.write_text(json.dumps(raw), encoding="utf-8")
    assert read_volume_marker(ISOBackend(iso, disc_size=_SMALL_DISC)).format == 1


def test_sync_new_disc_carries_format2_and_manifest_v3(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A sync that opens a new volume writes format 2 and manifest version 3.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "seed.txt").write_bytes(b"seed")
    iso = tmp_path / "s.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size="100000",
    )) == 0
    (source / "a.bin").write_bytes(b"A" * 80_000)
    (source / "b.bin").write_bytes(b"B" * 80_000)
    assert dispatch(_make_args(
        command="sync", source=str(source), test_iso=str(iso),
        disc_size="100000",
    )) == 0
    vol1 = tmp_path / "s.vol1.iso"
    assert vol1.is_file()
    backend = ISOBackend(vol1, disc_size=_SMALL_DISC)
    marker = read_volume_marker(backend)
    assert marker is not None
    assert marker.format == 2
    assert json.loads((vol1.with_suffix(".d") / "volume.json").read_bytes())["format"] == 2
    manifests = load_session_manifests(
        backend, None, start_session=marker.first_session, marker=marker,
    )
    assert manifests
    assert all(m.version == 3 for m in manifests)




def test_plaintext_baseline_refused_on_format2_encrypted(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A plaintext volume_baseline.json on an encrypted format 2 disc
        is refused with BaselineReadError.
    """
    iso, crypto, _ = _build_disc(tmp_path, baseline="json")
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    with pytest.raises(BaselineReadError, match="volume_baseline.json"):
        read_volume_baseline(backend, crypto)


def test_plaintext_baseline_refused_after_stripping_format(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Stripping format from volume.json does not bypass the refusal.
        The version 2 manifest proves the disc is format 2.
    """
    iso, crypto, _ = _build_disc(tmp_path, strip_format=True, baseline="json")
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    with pytest.raises(BaselineReadError, match="volume_baseline.json"):
        read_volume_baseline(backend, crypto)


def test_forged_plaintext_baseline_beside_enc_is_refused(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A format 2 encrypted disc with a valid volume_baseline.enc and a
        forged volume_baseline.json is refused. It must not prefer .enc.
    """
    iso, crypto, _ = _build_disc(tmp_path, baseline="enc")
    root = iso.with_suffix(".d")
    assert (root / "volume_baseline.enc").is_file()
    forged = {
        "version": 2,
        "volumes": [],
        "history": {
            "pwned.txt": [{
                "session": 0, "volume_index": 0, "kind": "full",
                "result_checksum": "ab" * 32, "full_size_bytes": 1,
                "timestamp": _TS, "blob_session": 0,
            }],
        },
        "state": {"pwned.txt": "ab" * 32},
        "origin": {"pwned.txt": 0},
    }
    (root / "volume_baseline.json").write_text(json.dumps(forged), encoding="utf-8")
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    with pytest.raises(BaselineReadError, match="volume_baseline.json"):
        read_volume_baseline(backend, crypto)


def test_plaintext_manifest_refused_on_format2_encrypted(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A plaintext manifest.json on an encrypted format 2 disc is
        refused with a clear error.
    """
    iso, crypto, _ = _build_disc(tmp_path, add_plaintext_manifest=True)
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    with pytest.raises(ValueError, match="manifest.json"):
        load_session_manifests(backend, crypto)


def test_plaintext_manifest_refused_after_stripping_format(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Stripping format from volume.json does not allow a plaintext
        manifest on an encrypted disc that carries a version 2 manifest.
    """
    iso, crypto, _ = _build_disc(
        tmp_path, strip_format=True, add_plaintext_manifest=True,
    )
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    with pytest.raises(ValueError, match="manifest.json"):
        load_session_manifests(backend, crypto)


def test_plaintext_metadata_refused_on_format1_with_key(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        With a key, a format 1 disc with a plaintext manifest is refused
        and the error names the file. A format 1 disc with manifest.enc
        and a plaintext version 1 baseline (02d325d) still reads, and the
        baseline warning names the file once.
    """
    iso, crypto, _ = _build_disc(
        tmp_path,
        marker_format=1,
        manifest_on_disc="json",
        manifest_version=1,
        baseline="json",
        baseline_version=1,
        enc_mode=None,
    )
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    with pytest.raises(ValueError, match="session_000/manifest.json"):
        load_session_manifests(backend, crypto)
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    iso, crypto, _ = _build_disc(
        legacy_dir,
        marker_format=1,
        manifest_on_disc="enc",
        manifest_version=1,
        baseline="json",
        baseline_version=1,
        enc_mode=None,
    )
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    manifests = load_session_manifests(backend, crypto)
    assert [m.session for m in manifests] == [0]
    assert not manifests[0].suspect
    capsys.readouterr()
    baseline = read_volume_baseline(backend, crypto, manifests=manifests)
    assert baseline is not None and baseline.version == 1
    err = capsys.readouterr().err
    assert err.count("plaintext metadata: volume_baseline.json") == 1


def test_invalid_enc_mode_errors(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A present enc_mode.json with an unsupported mode is an error,
        not a fallback to mode none.
    """
    iso, _crypto, _key = _build_disc(tmp_path, bad_enc_mode=True)
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    with pytest.raises(ValueError, match="enc_mode.json"):
        _crypto_for_disc(backend)


def test_invalid_enc_mode_json_shape_errors(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        enc_mode.json that is not an object is an error in both the
        crypto detection and the manifest scan.
    """
    iso, crypto, _key = _build_disc(tmp_path)
    root = iso.with_suffix(".d") / "session_000" / "enc_mode.json"
    root.write_text('["keyfile"]', encoding="utf-8")
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    with pytest.raises(ValueError, match="enc_mode.json"):
        _crypto_for_disc(backend)
    with pytest.raises(ValueError, match="enc_mode.json"):
        load_session_manifests(backend, crypto)


def test_invalid_enc_mode_exits_nonzero_through_cli(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        status on a disc with an invalid enc_mode.json exits 1 and names
        the file. It does not continue as mode none.
    """
    _isolate(tmp_path, monkeypatch)
    iso, _crypto, _key = _build_disc(tmp_path, bad_enc_mode=True)
    rc = dispatch(_make_args(command="status", test_iso=str(iso)))
    assert rc == 1
    assert "session_000/enc_mode.json" in capsys.readouterr().err


def test_plaintext_manifest_refused_when_enc_mode_declares_encryption(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A format 1 disc whose enc_mode.json declares keyfile cannot hold
        a plaintext manifest. The scan refuses it.
    """
    iso, crypto, _ = _build_disc(
        tmp_path, marker_format=1, manifest_on_disc="json",
        manifest_version=1, enc_mode="keyfile",
    )
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    with pytest.raises(ValueError, match="session_000/manifest.json"):
        load_session_manifests(backend, crypto)


def _baseline_disc(tmp_path: Path, payload: dict) -> ISOBackend:
    """Build a plaintext disc whose volume_baseline.json holds payload."""
    staging = tmp_path / "bl_staging"
    sdir = staging / "session_000"
    sdir.mkdir(parents=True)
    write_manifest(sdir, _manifest(0, 2, "none"), None)
    (staging / "volume_baseline.json").write_text(json.dumps(payload), encoding="utf-8")
    iso = tmp_path / "bl.iso"
    backend = ISOBackend(iso, disc_size=_SMALL_DISC)
    backend.init(staging, label="BL")
    return backend


_GOOD_VERSION = {
    "session": 0, "volume_index": 0, "kind": "full", "result_checksum": "c",
    "full_size_bytes": 1, "timestamp": _TS, "blob_session": 0,
}


@pytest.mark.parametrize("field,value", [
    ("volume_index", -1),
    ("session", "0"),
    ("kind", "unknown"),
    ("result_checksum", 5),
    ("full_size_bytes", -3),
    ("blob_session", True),
])
def test_baseline_rejects_bad_path_version_fields(tmp_path, field, value):
    """
    Input:  tmp_path, field, value
    Output: None
    Details:
        A version 2 baseline with a wrong type or range in a PathVersion
        raises BaselineReadError, not a traceback or a bad object.
    """
    event = dict(_GOOD_VERSION, **{field: value})
    backend = _baseline_disc(tmp_path, {
        "version": 2, "volumes": [], "history": {"a.txt": [event]},
        "state": {}, "origin": {},
    })
    with pytest.raises(BaselineReadError, match="volume_baseline.json"):
        read_volume_baseline(backend, None)


def test_baseline_accepts_good_path_version(tmp_path):
    """Input: tmp_path. Output: None. Details: The good event decodes."""
    backend = _baseline_disc(tmp_path, {
        "version": 2, "volumes": [{"volume_index": 0, "label": "V0",
                                   "first_session": 0, "last_session": 0}],
        "history": {"a.txt": [_GOOD_VERSION]}, "state": {}, "origin": {},
    })
    baseline = read_volume_baseline(backend, None)
    assert baseline.history["a.txt"][0].blob_session == 0
    assert baseline.volumes[0].label == "V0"


def test_baseline_rejects_negative_volume_record_index(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details: A negative volume_index in a volume record is refused.
    """
    backend = _baseline_disc(tmp_path, {
        "version": 2, "volumes": [{"volume_index": -2, "label": "V",
                                   "first_session": 0, "last_session": 0}],
        "history": {}, "state": {}, "origin": {},
    })
    with pytest.raises(BaselineReadError, match="volume_index"):
        read_volume_baseline(backend, None)


def test_marker_with_negative_index_is_not_trusted(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        volume.json with a negative volume_index or an unknown format
        reads as no marker.
    """
    for bad in ({"volume_index": -1}, {"format": 7}, {"first_session": "0"}):
        raw = dict(dataclasses.asdict(VolumeMarker("a", 0, 0, _TS, "")), **bad)
        staging = tmp_path / f"m{len(list(tmp_path.iterdir()))}"
        (staging / "session_000").mkdir(parents=True)
        write_manifest(staging / "session_000", _manifest(0, 2, "none"), None)
        (staging / "volume.json").write_text(json.dumps(raw), encoding="utf-8")
        iso = staging.with_suffix(".iso")
        ISOBackend(iso, disc_size=_SMALL_DISC).init(staging, label="M")
        assert read_volume_marker(ISOBackend(iso, disc_size=_SMALL_DISC)) is None


def test_malformed_blob_path_gives_clean_error():
    """
    Input:  None
    Output: None
    Details:
        parse_blob_session rejects a malformed blob path with ValueError.
    """
    with pytest.raises(ValueError, match="Invalid manifest blob path"):
        parse_blob_session("bad/path")
    assert parse_blob_session("session_007/full/abc") == 7


def test_build_volume_baseline_rejects_malformed_blob_path():
    """
    Input:  None
    Output: None
    Details:
        A manifest entry with a malformed blob path raises a clear error
        from build_volume_baseline, not a traceback.
    """
    entry_manifest = Manifest(
        version=2, session=0, timestamp=_TS, source="/s", label="L",
        based_on_session=None, encryption={},
        entries=[_entry("a.txt", "nosession/blob")],
        deleted=[], manifest_checksum="",
    )
    with pytest.raises(ValueError, match="Invalid manifest blob path"):
        build_volume_baseline(None, [entry_manifest], None, "L")


def _entry(path: str, file_field: str):
    """Build a full ManifestEntry pointing at file_field."""
    from oddarchiver.manifest import ManifestEntry
    return ManifestEntry(
        path=path, type="full", result_checksum="c",
        full_size_bytes=1, file=file_field,
    )


def _gap_iso(tmp_path: Path) -> Path:
    """Build a plaintext ISO with sessions 0, 1, and 3 (gap at 2)."""
    staging = tmp_path / "gap_staging"
    for n in (0, 1, 3):
        sdir = staging / f"session_{n:03d}"
        sdir.mkdir(parents=True)
        write_manifest(sdir, _manifest(n, 2, "none"), None)
    iso = tmp_path / "gap.iso"
    ISOBackend(iso, disc_size=_SMALL_DISC).init(staging, label="GAP")
    return iso


class _RecordingISO(ISOBackend):
    """ISOBackend that records every read_paths() request."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list[list[str]] = []

    def read_paths(self, paths):
        self.requests.append(list(paths))
        return super().read_paths(paths)


class _NoListBackend:
    """Serve control files without list_session_numbers."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def read_paths(self, paths):
        """Input: requested paths. Output: known files only."""
        return {path: self.files[path] for path in paths if path in self.files}


def test_control_paths_read_count_drops_and_gap_raises(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        One read_paths() call asks for the control files of the three
        listed sessions only (9 paths, not 3000). Session 2 is missing
        while session 3 exists, so the scan raises an error that names
        session 2 instead of returning a truncated list.
    """
    backend = _RecordingISO(_gap_iso(tmp_path), disc_size=_SMALL_DISC)
    with pytest.raises(ValueError, match="Session 2 is missing"):
        load_session_manifests(backend, None)
    assert len(backend.requests) == 1
    assert len(backend.requests[0]) == 9
    assert "session_003/manifest.json" in backend.requests[0]
    assert not any(p.startswith("session_002/") for p in backend.requests[0])


def test_control_paths_fallback_without_session_list(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A backend that cannot list sessions still finds session 50 after
        sessions 0 through 49.
    """
    files = {}
    for n in range(51):
        sdir = tmp_path / "sessions" / f"{n:03d}"
        sdir.mkdir(parents=True)
        write_manifest(sdir, _manifest(n, 2, "none"), None)
        files[f"session_{n:03d}/manifest.json"] = (
            sdir / "manifest.json"
        ).read_bytes()
    backend = _NoListBackend(files)
    manifests = load_session_manifests(backend, None)
    assert [m.session for m in manifests] == list(range(51))
    assert manifests[-1].session == 50


def test_disc_backend_lists_sessions_with_one_xorriso_call(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        DiscBackend reads the root names with one xorriso -ls call and
        parses its quoted output. Other names are ignored.
    """
    import subprocess
    from oddarchiver.disc import DiscBackend
    calls = []

    def fake_run(cmd, capture_output):
        calls.append(cmd)
        out = b"'session_000'\n'session_001'\n'session_003'\n'volume.json'\n"
        return subprocess.CompletedProcess(cmd, 0, out, b"")

    monkeypatch.setattr("oddarchiver.disc.subprocess.run", fake_run)
    assert DiscBackend("/dev/sr9").list_session_numbers() == [0, 1, 3]
    assert calls == [["xorriso", "-indev", "/dev/sr9", "-ls", "/"]]


def _make_args(**kwargs):
    """Build an argparse Namespace for dispatch-based tests."""
    import argparse
    defaults = {
        "command": "init", "source": None, "dest": None, "device": "/dev/sr0",
        "test_iso": None, "label": "FMT2", "encrypt": "none", "key": None,
        "dry_run": False, "no_cache": False, "disc_size": "50mb",
        "prefill": None, "session": None, "force": False, "level": "fast",
        "mirror": None, "recover_scan": False, "paths": None,
        "show_deleted": False, "path": None, "rescue_key": None,
        "skip_volumes": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_corrupt_enc_baseline_sync_exits_nonzero_before_burn(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        A corrupt volume_baseline.enc makes sync exit nonzero before it
        burns anything. The ISO keeps its session count.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.bin").write_bytes(b"A" * 80_000)
    (source / "b.bin").write_bytes(b"B" * 80_000)
    keyfile = tmp_path / "k.key"
    generate_keyfile(str(keyfile))
    iso = tmp_path / "archive.iso"
    args = _make_args(
        command="init", source=str(source), test_iso=str(iso),
        disc_size="100000", label="FMT2",
        encrypt="keyfile", key=str(keyfile),
    )
    assert dispatch(args) == 0
    vol1 = tmp_path / "archive.vol1.iso"
    assert vol1.is_file()

    # Corrupt the encrypted baseline on the latest disc.
    sessions_root = vol1.with_suffix(".d")
    enc_baseline = sessions_root / "volume_baseline.enc"
    assert enc_baseline.is_file()
    enc_baseline.write_bytes(b"not-a-valid-ciphertext")

    before = len(list(sessions_root.glob("session_*")))
    capsys.readouterr()
    args = _make_args(
        command="sync", source=str(source), test_iso=str(vol1),
        disc_size="100000", label="FMT2",
        encrypt="keyfile", key=str(keyfile),
    )
    rc = dispatch(args)
    err = capsys.readouterr().err
    after = len(list(sessions_root.glob("session_*")))
    assert rc != 0
    assert "volume_baseline.enc" in err
    assert after == before
