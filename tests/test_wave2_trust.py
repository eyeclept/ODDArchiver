"""
Tests for encryption downgrade anchors, disc-memory writes, ISO
rejection, session gaps, missing baselines, and missing ISO paths.
"""
# Imports
import argparse
import hashlib
import io
import json
import logging
import sys
from pathlib import Path

import pytest

from oddarchiver.cache import CacheManager
from oddarchiver.catalog import (
    CatalogDisc, archive_memory_path, remember_inserted_disc, _memory_path,
)
from oddarchiver.cli import _crypto_and_manifests, _crypto_for_disc, dispatch
from oddarchiver.crypto import KeyfileCrypto, generate_keyfile
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import (
    Manifest, ManifestEntry, load_session_manifests, write_manifest,
)
from oddarchiver.volume import (
    VolumeBaseline, VolumeMarker, parse_volume_marker_bytes,
    read_volume_baseline, read_volume_marker, write_volume_baseline,
    write_volume_marker,
)

# Globals
logger = logging.getLogger(__name__)
_log = logger
_SMALL = 10 * 2**20
_TS = "2026-04-19T12:00:00Z"

# Functions


def _isolate(tmp_path: Path, monkeypatch) -> None:
    """Point cache and local disc records at tmp_path."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "oddarchiver.cli.CacheManager",
        lambda *a, **k: CacheManager(cache_dir),
    )


def _make_args(**kwargs) -> argparse.Namespace:
    """Build a dispatch namespace with test defaults."""
    defaults = {
        "command": "init",
        "source": None,
        "dest": None,
        "device": "/dev/sr0",
        "test_iso": None,
        "label": "TRUST",
        "encrypt": "none",
        "key": None,
        "dry_run": False,
        "no_cache": False,
        "disc_size": "50mb",
        "prefill": None,
        "session": None,
        "force": False,
        "level": "fast",
        "slow": False,
        "mirror": None,
        "recover_scan": False,
        "paths": None,
        "show_deleted": False,
        "path": None,
        "rescue_key": None,
        "skip_volumes": None,
        "unskip_volumes": None,
        "all_disks": False,
        "skip_large": False,
        "embed_software": None,
        "read_mode": "sequential",
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _root(iso: Path) -> Path:
    """Return the ISO session tree."""
    return iso.with_suffix(".d")


def _init_encrypted(
    tmp_path: Path,
    *,
    extra: dict[str, bytes] | None = None,
    disc_size: str = "50mb",
) -> tuple[Path, Path, Path]:
    """
    Input: tmp_path, optional extra source files, disc size string
    Output: source, iso, keyfile
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"keep-bytes-v1")
    if extra:
        for name, data in extra.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    keyfile = tmp_path / "k.key"
    generate_keyfile(str(keyfile))
    iso = tmp_path / "archive.iso"
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        encrypt="keyfile", key=str(keyfile), label="TRUST",
        disc_size=disc_size,
    ))
    assert rc == 0
    return source, iso, keyfile

def _init_plaintext(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a plaintext ISO and return its source, image, and mode record."""
    source = tmp_path / "plain-src"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"plain-content")
    iso = tmp_path / "plain.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
    )) == 0
    marker = read_volume_marker(ISOBackend(iso, disc_size=_SMALL))
    assert marker is not None
    memory = archive_memory_path(marker.archive_id)
    assert json.loads(memory.read_text(encoding="utf-8"))["encryption_mode"] == "none"
    return source, iso, memory


@pytest.mark.parametrize(
    "command", ("sync", "restore", "verify", "ls", "history", "status", "browse"),
)
def test_plaintext_first_contact_refuses_without_terminal(
    tmp_path, monkeypatch, capsys, command,
):
    """A new plaintext mode claim cannot start a nonterminal command."""
    _isolate(tmp_path, monkeypatch)
    source, iso, memory = _init_plaintext(tmp_path)
    memory.unlink()
    capsys.readouterr()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    dest = tmp_path / "output"
    manifest = iso.with_suffix(".d") / "session_000" / "manifest.json"
    before = manifest.read_bytes()
    rc = dispatch(_make_args(
        command=command, source=str(source), dest=str(dest),
        test_iso=str(iso),
    ))
    captured = capsys.readouterr()
    assert rc == 1
    assert "error:" in captured.err
    assert "Run the same command once from a terminal" in captured.err
    assert "confirm the disc is unencrypted" in captured.err
    assert not dest.exists()
    assert not memory.exists()
    assert manifest.read_bytes() == before


def test_plaintext_terminal_confirmation_saves_mode_for_nonterminal_use(
    tmp_path, monkeypatch, capsys,
):
    """One terminal confirmation lets later nonterminal reads use the ISO."""
    _isolate(tmp_path, monkeypatch)
    _source, iso, memory = _init_plaintext(tmp_path)
    memory.unlink()
    capsys.readouterr()

    class TerminalInput(io.StringIO):
        """Supply a terminal reply to the confirmation prompt."""

        def isatty(self) -> bool:
            """Report a terminal input."""
            return True

    monkeypatch.setattr(sys, "stdin", TerminalInput("y\n"))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert dispatch(_make_args(command="ls", test_iso=str(iso))) == 0
    assert json.loads(memory.read_text(encoding="utf-8"))["encryption_mode"] == "none"
    capsys.readouterr()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    dest = tmp_path / "nonterminal"
    assert dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(iso),
    )) == 0
    assert (dest / "keep.txt").read_bytes() == b"plain-content"
    assert "confirm the disc is unencrypted" not in capsys.readouterr().err


def test_plaintext_init_mode_allows_nonterminal_restore(tmp_path, monkeypatch, capsys):
    """The mode saved by init allows a cron-style restore without a prompt."""
    _isolate(tmp_path, monkeypatch)
    _source, iso, memory = _init_plaintext(tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    dest = tmp_path / "init-output"
    assert dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(iso),
    )) == 0
    assert (dest / "keep.txt").read_bytes() == b"plain-content"
    assert json.loads(memory.read_text(encoding="utf-8"))["encryption_mode"] == "none"
    assert "confirm the disc is unencrypted" not in capsys.readouterr().err


def _forge_plaintext_manifest(sdir: Path, payload: bytes) -> None:
    """Write a checksummed plaintext manifest and blob for payload."""
    digest = hashlib.sha256(payload).hexdigest()
    blob = "0" * 64
    (sdir / "full").mkdir(exist_ok=True)
    (sdir / "full" / blob).write_bytes(payload)
    write_manifest(
        sdir,
        Manifest(
            version=1, session=0, timestamp=_TS, source="/src", label="TRUST",
            based_on_session=None, encryption={"mode": "none"},
            entries=[ManifestEntry(
                path="forged.txt", type="full", result_checksum=digest,
                full_size_bytes=len(payload), file=f"session_000/full/{blob}",
            )],
            deleted=[], manifest_checksum="",
        ),
        None,
    )


def _attack_none(
    iso: Path,
    *,
    delete_enc: bool = True,
    strip_format: bool = True,
    mode_none: bool = True,
    delete_enc_mode: bool = False,
    forge: bytes | None = b"FORGED-PLAINTEXT",
) -> None:
    """Apply the live downgrade attack variants to an ISO session tree."""
    root = _root(iso)
    sdir = root / "session_000"
    if delete_enc:
        (sdir / "manifest.enc").unlink(missing_ok=True)
        (root / "volume_baseline.enc").unlink(missing_ok=True)
    if delete_enc_mode:
        (sdir / "enc_mode.json").unlink(missing_ok=True)
    elif mode_none:
        (sdir / "enc_mode.json").write_text(
            json.dumps({"mode": "none"}), encoding="utf-8",
        )
    if strip_format:
        marker = json.loads((root / "volume.json").read_text(encoding="utf-8"))
        marker.pop("format", None)
        (root / "volume.json").write_text(json.dumps(marker), encoding="utf-8")
    if forge is not None:
        _forge_plaintext_manifest(sdir, forge)


class _RecordingISO(ISOBackend):
    """ISOBackend that records every read_paths() request."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list[list[str]] = []

    def read_paths(self, paths):
        self.requests.append(list(paths))
        return super().read_paths(paths)


# Tests


def test_h1a_key_refuses_mode_none_variant_a(tmp_path, monkeypatch, capsys):
    """
    Input: encrypted ISO with mode set to none and .enc files kept
    Output: restore --key exits 1 and does not write forged bytes
    Details: Variant A. Anchor (a) or (b) must refuse the disc.
    """
    _isolate(tmp_path, monkeypatch)
    _source, iso, keyfile = _init_encrypted(tmp_path)
    _attack_none(iso, delete_enc=False, strip_format=False, mode_none=True)
    dest = tmp_path / "out"
    rc = dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(iso),
        key=str(keyfile), force=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "This encrypted disc has a plaintext session_000/manifest.json." in err
    assert not (dest / "forged.txt").exists()


def test_h1a_passphrase_env_refuses_deleted_enc_variant_b(tmp_path, monkeypatch, capsys):
    """
    Input: encrypted ISO with enc files deleted and ODDARCHIVER_PASSPHRASE set
    Output: restore exits 1
    Details: Variant B. Anchor (a) must refuse when the env supplies a key.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"keep-bytes-v1")
    monkeypatch.setenv("ODDARCHIVER_PASSPHRASE", "correct-horse")
    iso = tmp_path / "archive.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        encrypt="passphrase", label="TRUST",
    )) == 0
    _attack_none(iso, delete_enc=True, delete_enc_mode=True, strip_format=True)
    dest = tmp_path / "out"
    rc = dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(iso), force=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "This encrypted disc has a plaintext session_000/manifest.json." in err
    assert not (dest / "forged.txt").exists()
    assert not (dest / "keep.txt").exists()


def test_h1a_rescue_key_refuses_stripped_variant_c(tmp_path, monkeypatch, capsys):
    """
    Input: stripped format-1-looking disc and --rescue-key
    Output: restore exits 1
    Details: Variant C. Anchor (a) must refuse a supplied rescue key.
    """
    _isolate(tmp_path, monkeypatch)
    _source, iso, keyfile = _init_encrypted(tmp_path)
    _attack_none(iso, delete_enc=True, strip_format=True, mode_none=True)
    dest = tmp_path / "out"
    rc = dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(iso),
        key=str(keyfile), rescue_key="", force=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "this archive has no rescue key; it predates this feature" in err
    assert not (dest / "forged.txt").exists()


def test_h1b_enc_files_with_mode_none_refused_without_key(tmp_path, monkeypatch, capsys):
    """
    Input: disc that still holds manifest.enc but reports mode none
    Output: restore without --key exits 1
    Details: Anchor (b). Encrypted metadata plus mode none is a refuse.
    """
    _isolate(tmp_path, monkeypatch)
    _source, iso, _key = _init_encrypted(tmp_path)
    _attack_none(iso, delete_enc=False, strip_format=True, mode_none=True, forge=None)
    dest = tmp_path / "out"
    rc = dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(iso), force=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "encrypted metadata" in err


def _assert_h1c_disc1(vol1: Path, keyfile: Path) -> None:
    """
    Input: second ISO and its keyfile
    Output: assertions about the encrypted baseline and second-disc manifest
    """
    baseline = read_volume_baseline(
        ISOBackend(vol1, disc_size=_SMALL),
        KeyfileCrypto(str(keyfile)),
    )
    assert baseline is not None
    assert any(vol.encrypted is True for vol in baseline.volumes)  # scbc ignore[is-true-false-strict] nullable bool requires exact True
    on_disc1 = load_session_manifests(
        ISOBackend(vol1, disc_size=_SMALL), KeyfileCrypto(str(keyfile)),
        start_session=1,
    )
    assert [entry.path for m in on_disc1 for entry in m.entries] == ["big2.bin"]


def _assert_h1c_restored(rc, captured, dest: Path) -> None:
    """
    Input: restore exit code, captured output, and destination
    Output: assertions about rejected first disc and restored second disc
    """
    assert rc == 0
    assert (
        "Refuse this disc (disc 0): This encrypted disc has a plaintext "
        "session_000/manifest.json. Do not trust this disc."
    ) in captured.err
    assert "error:" not in captured.err
    assert {
        p.relative_to(dest).as_posix(): p.read_bytes()
        for p in dest.rglob("*") if p.is_file()
    } == {"big2.bin": b"C" * 80_000}
    missing = captured.out.split("  missing:\n", 1)[1].split()
    assert sorted(missing) == ["big.bin", "keep.txt"]
    assert "forged.txt" not in captured.out


def test_h1c_catalog_refuses_downgraded_sibling(tmp_path, monkeypatch, capsys):
    """
    Input: encrypted two-disc archive with disc 0 forged to plaintext
    Output: restore --all-disks refuses disc 0 and restores disc 1 files
    Details: Anchor (c). The plaintext manifest on disc 0 is a load error
        for an acquired disc. The restore refuses disc 0, marks it
        missing, and reports its files as missing. The restore writes
        nothing from disc 0.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src_h1c"
    keyfile = tmp_path / "k_h1c.key"
    generate_keyfile(str(keyfile))
    iso = tmp_path / "h1c.iso"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"keep-bytes-v1")
    (source / "big.bin").write_bytes(b"B" * 80_000)
    (source / "big2.bin").write_bytes(b"C" * 80_000)
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        encrypt="keyfile", key=str(keyfile), disc_size="100000",
        label="TRUST",
    )) == 0
    vol1 = tmp_path / "h1c.vol1.iso"
    assert vol1.is_file()
    _assert_h1c_disc1(vol1, keyfile)
    _attack_none(iso, delete_enc=True, strip_format=True, mode_none=True)
    dest = tmp_path / "out"
    capsys.readouterr()
    rc = dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(vol1),
        key=str(keyfile), all_disks=True, force=True,
        disc_size="100000",
    ))
    captured = capsys.readouterr()
    _assert_h1c_restored(rc, captured, dest)


def test_h1d_memory_refuses_later_mode_none(tmp_path, monkeypatch, capsys):
    """
    Input: successful encrypted restore, then a mode-none attack
    Output: later restore without --key exits 1 and writes nothing
    Details: Anchor (d). The forged plaintext manifest removes
        enc_mode.json and manifest.enc, so the disc has no mode evidence.
        The local record supplies keyfile, so the restore asks for the key
        and keeps the record unchanged.
    """
    _isolate(tmp_path, monkeypatch)
    _source, iso, keyfile = _init_encrypted(tmp_path)
    marker = read_volume_marker(ISOBackend(iso, disc_size=_SMALL))
    assert marker is not None
    dest1 = tmp_path / "out1"
    assert dispatch(_make_args(
        command="restore", dest=str(dest1), test_iso=str(iso),
        key=str(keyfile), force=True,
    )) == 0
    assert (dest1 / "keep.txt").read_bytes() == b"keep-bytes-v1"
    mem = archive_memory_path(marker.archive_id)
    stored = json.loads(mem.read_text(encoding="utf-8"))
    assert stored["encryption_mode"] == "keyfile"
    _attack_none(iso, delete_enc=True, strip_format=True, mode_none=True)
    dest2 = tmp_path / "out2"
    rc = dispatch(_make_args(
        command="restore", dest=str(dest2), test_iso=str(iso), force=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "error: --key is required for keyfile-encrypted disc." in err
    assert not dest2.exists() or list(dest2.iterdir()) == []
    assert json.loads(mem.read_text(encoding="utf-8"))["encryption_mode"] == "keyfile"


def test_h1e_sync_will_not_write_plaintext(tmp_path, monkeypatch, capsys):
    """
    Input: encrypted archive whose catalog records encrypted discs
    Output: sync without a key does not burn plaintext
    Details: Anchor (e).
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src_h1e"
    keyfile = tmp_path / "k_h1e.key"
    generate_keyfile(str(keyfile))
    iso = tmp_path / "h1e.iso"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"keep-bytes-v1")
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        encrypt="keyfile", key=str(keyfile), disc_size="50000000",
        label="TRUST",
    )) == 0
    assert dispatch(_make_args(
        command="ls", test_iso=str(iso), key=str(keyfile),
    )) == 0
    target = iso
    root = _root(target)
    (root / "session_000" / "enc_mode.json").write_text(
        json.dumps({"mode": "none"}), encoding="utf-8",
    )
    (root / "session_000" / "manifest.enc").unlink(missing_ok=True)
    sessions = sorted(p.name for p in root.glob("session_*"))
    (source / "later.txt").write_bytes(b"must-not-land-as-plaintext")
    rc = dispatch(_make_args(
        command="sync", source=str(source), test_iso=str(target),
        disc_size="50000000", recover_scan=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "Local disc record" in err
    assert "says this archive is encrypted. This disc reports no encryption." in err
    assert sorted(p.name for p in root.glob("session_*")) == sessions


def test_h1_old_format1_encrypted_disc_still_restores(tmp_path, monkeypatch, capsys):
    """
    Input: format-1 encrypted disc with plaintext v1 baseline
    Output: restore --key exits 0 and restores keep.txt bytes
    Details: 02d325d-era discs must keep working.
    """
    _isolate(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    sdir = staging / "session_000"
    sdir.mkdir(parents=True)
    payload = b"legacy-keep"
    digest = hashlib.sha256(payload).hexdigest()
    blob = "a" * 64
    (sdir / "full").mkdir()
    keyfile = tmp_path / "k.key"
    generate_keyfile(str(keyfile))
    crypto = KeyfileCrypto(str(keyfile))
    (sdir / "full" / blob).write_bytes(crypto.encrypt(payload))
    write_manifest(
        sdir,
        Manifest(
            version=1, session=0, timestamp=_TS, source="/src", label="OLD",
            based_on_session=None, encryption={"mode": "keyfile"},
            entries=[ManifestEntry(
                path="keep.txt", type="full", result_checksum=digest,
                full_size_bytes=len(payload), file=f"session_000/full/{blob}",
            )],
            deleted=[], manifest_checksum="",
        ),
        crypto,
    )
    raw = {
        "archive_id": "legacyarch01",
        "volume_index": 1,
        "first_session": 0,
        "created_at": _TS,
        "previous_volume_label": "VOL0",
    }
    (staging / "volume.json").write_text(json.dumps(raw), encoding="utf-8")
    write_volume_baseline(
        staging,
        VolumeBaseline(1, [], {}, {"keep.txt": digest}, {"keep.txt": 0}),
        None,
    )
    iso = tmp_path / "old.iso"
    ISOBackend(iso, disc_size=_SMALL).init(staging, label="OLD")
    dest = tmp_path / "out"
    rc = dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(iso),
        key=str(keyfile), force=True,
    ))
    captured = capsys.readouterr()
    assert rc == 0
    assert (dest / "keep.txt").read_bytes() == payload
    assert "plaintext" in captured.err or "old encrypted" in captured.err


def test_m1_suspect_manifests_do_not_poison_memory(tmp_path, monkeypatch, capsys):
    """
    Input: marked disc with only SUSPECT manifests
    Output: no last_session -1 in the local disc record
    Details: M1. A bad record is not written. The command prints a warning.
    """
    _isolate(tmp_path, monkeypatch)
    marker = VolumeMarker("memtest01", 0, 0, _TS, "", format=2)
    suspect = Manifest(
        version=0, session=-1, timestamp="", source="", label="",
        based_on_session=None, encryption={}, entries=[], deleted=[],
        manifest_checksum="", suspect=True,
    )
    remember_inserted_disc(marker, "TRUST", [suspect])
    err = capsys.readouterr().err
    assert "did not save a disc record" in err
    path = _memory_path("memtest01")
    if path.exists():
        stored = json.loads(path.read_text(encoding="utf-8"))
        for rec in stored.get("discs", {}).values():
            assert rec.get("last_session") != -1


def test_m1_reader_names_file_to_move(tmp_path, monkeypatch):
    """
    Input: unreadable local disc record
    Output: RuntimeError names the file to move
    Details: M1 fail-closed reader.
    """
    _isolate(tmp_path, monkeypatch)
    from oddarchiver.catalog import remember_discs
    from oddarchiver.catalog import ArchiveCatalog

    path = _memory_path("broken01")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"broken")
    catalog = ArchiveCatalog("broken01", 0, 0, {
        0: CatalogDisc(0, "L", 0, 0),
    }, {}, True)
    with pytest.raises(RuntimeError, match="Move or delete this file") as exc:
        remember_discs(catalog)
    assert str(path) in str(exc.value)


def _build_m2_foreign_disc(tmp_path: Path, iso: Path) -> None:
    """
    Input: test root and first archive ISO
    Output: first ISO and its extracted tree replaced with a foreign archive
    """
    foreign_src = tmp_path / "foreign"
    foreign_src.mkdir()
    (foreign_src / "x.txt").write_bytes(b"other-archive")
    foreign_key = tmp_path / "f.key"
    generate_keyfile(str(foreign_key))
    foreign_iso = tmp_path / "foreign.iso"
    assert dispatch(_make_args(
        command="init", source=str(foreign_src), test_iso=str(foreign_iso),
        encrypt="keyfile", key=str(foreign_key), label="OTHER",
    )) == 0
    iso.write_bytes(foreign_iso.read_bytes())
    foreign_d = foreign_iso.with_suffix(".d")
    local_d = iso.with_suffix(".d")
    if local_d.exists():
        for child in local_d.iterdir():
            if child.is_file():
                child.unlink()
    import shutil
    if local_d.exists():
        shutil.rmtree(local_d)
    shutil.copytree(foreign_d, local_d)


def test_m2_rejected_iso_sibling_counts_as_missing(tmp_path, monkeypatch, capsys):
    """
    Input: two-disc archive whose disc 0 ISO is a foreign archive
    Output: restore --all-disks terminates and treats disc 0 as missing
    Details: M2. ISO mode must not loop.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src_m2"
    keyfile = tmp_path / "k_m2.key"
    generate_keyfile(str(keyfile))
    iso = tmp_path / "m2.iso"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"keep-bytes-v1")
    (source / "big.bin").write_bytes(b"B" * 80_000)
    (source / "big2.bin").write_bytes(b"C" * 80_000)
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        encrypt="keyfile", key=str(keyfile), disc_size="100000",
        label="TRUST",
    )) == 0
    vol1 = tmp_path / "m2.vol1.iso"
    assert vol1.is_file()
    _build_m2_foreign_disc(tmp_path, iso)
    dest = tmp_path / "out"
    rc = dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(vol1),
        key=str(keyfile), all_disks=True, force=True, paths=["keep.txt"],
        disc_size="100000",
    ))
    err = capsys.readouterr().err
    assert rc in (0, 1)
    assert "Rejected disc 0" in err or "Missing disc 0" in err
    assert "forged" not in err.lower()


def test_m3_gap_with_later_session_raises(tmp_path):
    """
    Input: ISO with session 0 and session 2, no session 1
    Output: load_session_manifests names the missing session
    Details: M3. Do not return a truncated list.
    """
    staging = tmp_path / "gap"
    for n, name in ((0, "a.txt"), (2, "c.txt")):
        sdir = staging / f"session_{n:03d}"
        sdir.mkdir(parents=True)
        payload = name.encode()
        digest = hashlib.sha256(payload).hexdigest()
        blob = f"{n:064x}"
        (sdir / "full").mkdir()
        (sdir / "full" / blob).write_bytes(payload)
        write_manifest(
            sdir,
            Manifest(
                version=2, session=n, timestamp=_TS, source="/s", label="GAP",
                based_on_session=None if n == 0 else n - 1,
                encryption={"mode": "none"},
                entries=[ManifestEntry(
                    path=name, type="full", result_checksum=digest,
                    full_size_bytes=len(payload),
                    file=f"session_{n:03d}/full/{blob}",
                )],
                deleted=[], manifest_checksum="",
            ),
            None,
        )
    write_volume_marker(
        staging, VolumeMarker("gaparch", 0, 0, _TS, "", format=2),
    )
    iso = tmp_path / "gap.iso"
    ISOBackend(iso, disc_size=_SMALL).init(staging, label="GAP")
    backend = ISOBackend(iso, disc_size=_SMALL)
    with pytest.raises(ValueError, match="Session 1 is missing") as exc:
        load_session_manifests(backend)
    assert "session 2 exists" in str(exc.value).lower() or "2 exists" in str(exc.value)


def test_m4_all_suspect_exits_nonzero(tmp_path, monkeypatch, capsys):
    """
    Input: encrypted disc restored with the wrong key
    Output: restore --all-disks exits 1 and names the unreadable manifests
    Details: M4. Do not restore zero files with exit 0.
    """
    _isolate(tmp_path, monkeypatch)
    _source, iso, _key = _init_encrypted(tmp_path)
    wrong = tmp_path / "wrong.key"
    generate_keyfile(str(wrong))
    dest = tmp_path / "out"
    rc = dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(iso),
        key=str(wrong), all_disks=True, force=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "SUSPECT" in err or "unreadable" in err or "Check the key" in err
    assert list(dest.rglob("*")) == [] or not (dest / "keep.txt").exists()


def test_m6_missing_baseline_format2_is_error(tmp_path, monkeypatch, capsys):
    """
    Input: format 2 later disc with no baseline file
    Output: sync and --all-disks exit 1
    Details: M6.
    """
    _isolate(tmp_path, monkeypatch)
    source = tmp_path / "src_m6"
    keyfile = tmp_path / "k_m6.key"
    generate_keyfile(str(keyfile))
    iso = tmp_path / "m6.iso"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"keep-bytes-v1")
    (source / "big.bin").write_bytes(b"B" * 80_000)
    (source / "big2.bin").write_bytes(b"C" * 80_000)
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
        encrypt="keyfile", key=str(keyfile), disc_size="100000",
        label="TRUST",
    )) == 0
    vol1 = tmp_path / "m6.vol1.iso"
    assert vol1.is_file()
    root = _root(vol1)
    (root / "volume_baseline.enc").unlink(missing_ok=True)
    (root / "volume_baseline.json").unlink(missing_ok=True)
    (source / "more.txt").write_bytes(b"after-missing-baseline")
    rc = dispatch(_make_args(
        command="sync", source=str(source), test_iso=str(vol1),
        key=str(keyfile), disc_size="100000",
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "no volume baseline" in err
    dest = tmp_path / "out"
    rc = dispatch(_make_args(
        command="restore", dest=str(dest), test_iso=str(vol1),
        key=str(keyfile), all_disks=True, force=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "no volume baseline" in err


def test_m6_format1_later_disc_warns_and_continues(tmp_path, capsys):
    """
    Input: format 1 later disc with no baseline
    Output: catalog build warns and continues
    Details: M6 pre-baseline era.
    """
    from oddarchiver.catalog import build_archive_catalog

    staging = tmp_path / "oldvol"
    sdir = staging / "session_001"
    sdir.mkdir(parents=True)
    payload = b"old"
    digest = hashlib.sha256(payload).hexdigest()
    blob = "b" * 64
    (sdir / "full").mkdir()
    (sdir / "full" / blob).write_bytes(payload)
    write_manifest(
        sdir,
        Manifest(
            version=1, session=1, timestamp=_TS, source="/s", label="OLD1",
            based_on_session=0, encryption={"mode": "none"},
            entries=[ManifestEntry(
                path="old.txt", type="full", result_checksum=digest,
                full_size_bytes=len(payload), file=f"session_001/full/{blob}",
            )],
            deleted=[], manifest_checksum="",
        ),
        None,
    )
    raw = {
        "archive_id": "oldformat1",
        "volume_index": 1,
        "first_session": 1,
        "created_at": _TS,
        "previous_volume_label": "VOL0",
    }
    (staging / "volume.json").write_text(json.dumps(raw), encoding="utf-8")
    iso = tmp_path / "old1.iso"
    ISOBackend(iso, disc_size=_SMALL).init(staging, label="OLD1")
    backend = ISOBackend(iso, disc_size=_SMALL)
    marker = read_volume_marker(backend)
    manifests = load_session_manifests(backend, start_session=1, marker=marker)
    catalog = build_archive_catalog(backend, None, manifests, marker, label="OLD1")
    err = capsys.readouterr().err
    assert "no volume baseline" in err
    assert catalog.source_volume_index == 1
    assert not catalog.complete


def _assert_m9_control_batch(backend, raw) -> None:
    """
    Input: recording backend and raw control data
    Output: assertions about one batch and its parsed marker
    """
    assert len(backend.requests) == 1
    batch = backend.requests[0]
    assert "volume.json" in batch
    assert any(p.endswith("enc_mode.json") for p in batch)
    assert getattr(raw, "marker", None) is not None
    parsed = parse_volume_marker_bytes(raw.get("volume.json"))
    assert parsed is not None
    assert parsed.format == 2


def test_m9_volume_json_in_same_control_batch(tmp_path):
    """
    Input: plaintext ISO
    Output: one read_paths batch includes volume.json and enc_mode.json
    Details: M9.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"hello")
    iso = tmp_path / "m9.iso"
    assert dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
    )) == 0
    backend = _RecordingISO(iso, disc_size=_SMALL)
    crypto, manifests, raw = _crypto_and_manifests(backend)
    assert crypto is not None
    assert manifests
    _assert_m9_control_batch(backend, raw)


def test_l4_restore_missing_iso_exits_1(tmp_path, capsys):
    """
    Input: restore --test-iso of a path that does not exist
    Output: exit 1 and a clear error
    Details: L4.
    """
    missing = tmp_path / "no-such.iso"
    rc = dispatch(_make_args(
        command="restore", dest=str(tmp_path / "out"),
        test_iso=str(missing), force=True,
    ))
    err = capsys.readouterr().err
    assert rc == 1
    assert "does not exist" in err
    assert str(missing) in err


@pytest.mark.parametrize("command", ["history", "ls", "verify", "status"])
def test_l4_other_commands_missing_iso_exits_1(tmp_path, capsys, command):
    """
    Input: read command with a missing --test-iso path
    Output: exit 1
    Details: L4. init is exempt because it creates the image.
    """
    missing = tmp_path / "gone.iso"
    kwargs = {"command": command, "test_iso": str(missing)}
    if command == "verify":
        kwargs["level"] = "fast"
    rc = dispatch(_make_args(**kwargs))
    err = capsys.readouterr().err
    assert rc == 1
    assert "does not exist" in err


def test_l4_init_creates_missing_iso(tmp_path):
    """
    Input: init --test-iso of a path that does not exist yet
    Output: exit 0 and the ISO is created
    Details: L4 exception for init.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"init-create")
    iso = tmp_path / "new.iso"
    assert not iso.exists()
    rc = dispatch(_make_args(
        command="init", source=str(source), test_iso=str(iso),
    ))
    assert rc == 0
    assert iso.exists()


def test_volume_record_missing_encrypted_is_unknown(tmp_path):
    """
    Input: version 2 baseline JSON without encrypted
    Output: reader stores encrypted as None
    Details: H1(c) missing field defaults to unknown.
    """
    staging = tmp_path / "base"
    staging.mkdir()
    (staging / "volume_baseline.json").write_text(json.dumps({
        "version": 2,
        "volumes": [{
            "volume_index": 0, "label": "V0",
            "first_session": 0, "last_session": 0,
        }],
        "history": {},
        "state": {},
        "origin": {},
    }), encoding="utf-8")
    iso = tmp_path / "base.iso"
    ISOBackend(iso, disc_size=_SMALL).init(staging, label="V0")
    baseline = read_volume_baseline(ISOBackend(iso, disc_size=_SMALL), None)
    assert baseline is not None
    assert baseline.volumes[0].encrypted is None


def test_crypto_for_disc_direct_mode_none_with_key(tmp_path, monkeypatch):
    """
    Input: mode none disc and a key path
    Output: _crypto_for_disc raises
    Details: Anchor (a) at the helper.
    """
    _isolate(tmp_path, monkeypatch)
    _source, iso, keyfile = _init_encrypted(tmp_path)
    _attack_none(iso, delete_enc=True, strip_format=True, mode_none=True, forge=None)
    backend = ISOBackend(iso, disc_size=_SMALL)
    with pytest.raises(ValueError, match="You supplied a key"):
        _crypto_for_disc(backend, key_path=str(keyfile))
