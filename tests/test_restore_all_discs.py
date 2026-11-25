"""Restore --all-disks must read every available disc's manifests once."""

import hashlib
import logging

import pytest

from oddarchiver.catalog import (
    ArchiveCatalog, CatalogDisc, remember_discs, remember_encryption_mode,
)
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest
from oddarchiver.restore_plan import restore_archive
from oddarchiver.volume import VolumeMarker, write_volume_marker
from tests.test_restore_plan import (
    Acquisition,
    _CRYPTO,
    _DISC,
    _ID,
    _source,
    _stage,
)

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _superseded_three_discs(tmp_path):
    """Input: temp root. Output: three ISOs; every file on volume 0 is superseded."""
    iso0, prior = _stage(tmp_path, 0, {"a.txt": b"A-old"}, _CRYPTO)
    iso1, prior = _stage(tmp_path, 1, {"a.txt": b"A-new"}, _CRYPTO, prior)
    iso2, _prior = _stage(tmp_path, 2, {"c.txt": b"C"}, _CRYPTO, prior)
    return [iso0, iso1, iso2]


def _append_fill_file(tmp_path, iso0):
    """Input: temp root and volume 0 ISO. Output: fill file bytes on session 3."""
    content = b"fill-F"
    staging = tmp_path / "fill-staging"
    session_dir = staging / "session_003"
    session_dir.mkdir(parents=True)
    rel = f"session_003/full/{hashlib.sha256(b'F').hexdigest()}"
    blob = staging / rel
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(_CRYPTO.encrypt(content))
    entry = ManifestEntry(
        path="F.txt", type="full", file=rel,
        result_checksum=hashlib.sha256(content).hexdigest(),
        full_size_bytes=len(content),
    )
    manifest = Manifest(
        version=1, session=3, timestamp="2026-10-15T00:00:03Z",
        source="/src", label="VOL0", based_on_session=2,
        encryption={}, entries=[entry], deleted=[], manifest_checksum="",
    )
    write_manifest(session_dir, manifest)
    marker = VolumeMarker(
        _ID, 0, 0, manifest.timestamp, "",
        session_ranges=[[0, 0], [3, 3]],
    )
    write_volume_marker(staging, marker)
    ISOBackend(iso0, disc_size=_DISC).append(
        staging, label="VOL0", expected_session_count=1,
    )
    return content


def _remember_three_volumes():
    """Input: None. Output: local records for volumes 0-2 without the fill session."""
    remember_encryption_mode(_ID, "none")
    remember_discs(ArchiveCatalog(
        _ID, 1, 1,
        {
            0: CatalogDisc(0, "VOL0", 0, 0),
            1: CatalogDisc(1, "VOL1", 1, 1),
            2: CatalogDisc(2, "VOL2", 2, 2),
        },
        {},
        True,
    ))


def test_all_disks_restores_fill_file_from_unchecked_older_disc(tmp_path, capsys):
    """
    Input: three ISOs; volume 0 holds fill file F unknown to volumes 1-2
    Output: restore from volume 1 with volume 2 missing restores F
    """
    discs = _superseded_three_discs(tmp_path)
    content = _append_fill_file(tmp_path, discs[0])
    _remember_three_volumes()
    backend, catalog, manifests = _source(discs[1])
    dest = tmp_path / "out"
    acquisition = Acquisition(discs, [0])
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        skip_volumes=[2], acquisition=acquisition,
    )
    assert result.failures == 0
    assert result.paused is False
    assert (dest / "a.txt").read_bytes() == b"A-new"
    assert (dest / "F.txt").read_bytes() == content
    assert not (dest / "c.txt").exists()
    assert acquisition.calls
    assert acquisition.calls[0] == {0: 0}
    assert 2 not in {index for call in acquisition.calls for index in call}
    err = capsys.readouterr().err
    assert "Unchecked volumes:" in err
    assert "disc 2" in err
    assert "incomplete" in err
