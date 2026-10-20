"""Test old archive discovery and current archive read counts."""
# Imports
import logging

import pytest

from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.restore_plan import restore_archive
from oddarchiver.restore_state import load_restore_state
from tests.test_depth_section41 import _ID as _OLD_ID
from tests.test_depth_section41 import _source as _old_source
from tests.test_depth_section41 import _stage as _old_stage
from tests.test_restore_plan import _ID as _NEW_ID
from tests.test_restore_plan import _source as _new_source
from tests.test_restore_plan import _three_discs, _track_iso_blob_reads

# Globals
logger = logging.getLogger(__name__)
_log = logger
_CRYPTO = NullCrypto()
_DISC = 20 * 2**20


# Functions
@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Input: temporary directory. Output: None. Details: Isolate archive memory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


class OrderedAcquisition:
    """Give the requested discs in a specified order."""

    image = None

    def __init__(self, discs, order):
        """Input: disc paths and order. Output: acquisition schedule."""
        self.discs = discs
        self.order = iter(order)
        self.calls = []

    def acquire(self, needed, catalog, archive_id):
        """Input: needed map. Output: selected backend or missing disc."""
        self.calls.append(dict(needed))
        action = next(self.order)
        if isinstance(action, tuple):
            assert action[1] in needed
            return None, action[1], False
        return ISOBackend(self.discs[action], disc_size=_DISC), None, False


def _old_chain(tmp_path):
    """Input: temporary directory. Output: three old discs and final bytes."""
    base = b"A" * 8000
    middle = base[:-8] + b"B" * 8
    latest = middle[:-8] + b"C" * 8
    iso0, state0, origin0 = _old_stage(
        tmp_path, 0, {"changed.txt": base}, _CRYPTO,
    )
    iso1, state1, origin1 = _old_stage(
        tmp_path, 1, {}, _CRYPTO, prior_state=state0,
        prior_origin=origin0, delta={"changed.txt": (base, middle)},
    )
    iso2, _state2, _origin2 = _old_stage(
        tmp_path, 2, {}, _CRYPTO, prior_state=state1,
        prior_origin=origin1, delta={"changed.txt": (middle, latest)},
    )
    return [iso0, iso1, iso2], latest


def test_missing_old_base_reports_missing(tmp_path, capsys):
    """Input: missing base disc. Output: missing file and named disc."""
    discs, _latest = _old_chain(tmp_path)
    backend, catalog, manifests = _old_source(discs[2])
    acquisition = OrderedAcquisition(discs, [1, ("missing", 0)])
    dest = tmp_path / "missing"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    assert result.failures == 0
    assert result.missing == ["changed.txt"]
    assert not (dest / "changed.txt").exists()
    assert load_restore_state(dest, _OLD_ID).missing_discs == [0]
    assert "disc 0" in capsys.readouterr().err.lower()


def test_old_chain_forward_acquisition_reads_once(tmp_path, monkeypatch):
    """Input: disc 0 then disc 1. Output: final bytes with one read per disc."""
    discs, latest = _old_chain(tmp_path)
    reads = _track_iso_blob_reads(monkeypatch, discs)
    backend, catalog, manifests = _old_source(discs[2])
    acquisition = OrderedAcquisition(discs, [0, 1])
    dest = tmp_path / "forward"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    assert result.failures == 0
    assert (dest / "changed.txt").read_bytes() == latest
    assert [index for index, _blobs in reads] == [2, 0, 1]
    assert load_restore_state(dest, _OLD_ID).read_discs == [0, 1, 2]
    assert len(acquisition.calls) == 2


def test_format2_three_disc_plan_and_reads_unchanged(tmp_path, monkeypatch):
    """Input: format 2 archive. Output: old plan and one blob read per disc."""
    discs = _three_discs(tmp_path)
    reads = _track_iso_blob_reads(monkeypatch, discs)
    backend, catalog, manifests = _new_source(discs[2])
    acquisition = OrderedAcquisition(discs, [1, 0])
    dest = tmp_path / "current"
    result = restore_archive(
        dest, backend, _CRYPTO, catalog, manifests=manifests,
        acquisition=acquisition,
    )
    assert result.failures == 0
    assert acquisition.calls == [{0: 1, 1: 1}, {0: 1}]
    assert [index for index, _blobs in reads] == [2, 1, 0]
    assert load_restore_state(dest, _NEW_ID).read_discs == [0, 1, 2]
    assert (dest / "a.txt").read_bytes() == b"A-new"
    assert (dest / "old-only.txt").read_bytes() == b"old-only"
    assert (dest / "latest.txt").read_bytes() == b"latest"
