"""Restore must not re-prompt for a disc after every file is done."""

import logging
from pathlib import Path

from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.restore_plan import restore_archive
from oddarchiver.restore_state import load_restore_state
from tests.test_restore_plan import (
    Acquisition,
    _CRYPTO,
    _DISC,
    _source,
    _stage,
)

logger = logging.getLogger(__name__)


class _OnceThenFail(Acquisition):
    """Hand one disc, then fail if restore asks again."""

    def acquire(self, needed, catalog, archive_id):
        """Input: needed map. Output: next disc or raise on a repeat prompt."""
        self.calls.append(dict(needed))
        try:
            action = next(self.order)
        except StopIteration:
            raise AssertionError(
                f"restore re-prompted for discs {sorted(needed)} after files were done"
            ) from None
        assert action in needed
        return ISOBackend(self.discs[action], disc_size=_DISC), None, False


def _two_file_archive(tmp_path: Path):
    """Input: temp root. Output: disc 0 ISO, disc 1 ISO, catalog from disc 1, manifests."""
    iso0, prior = _stage(tmp_path, 0, {"a.bin": b"aaa"}, _CRYPTO)
    iso1, _prior = _stage(tmp_path, 1, {"b.bin": b"bbb"}, _CRYPTO, prior)
    backend, catalog, manifests = _source(iso1)
    return iso0, iso1, backend, catalog, manifests


def test_restore_does_not_reprompt_disc0_after_all_files_done(tmp_path, monkeypatch):
    """
    Input: two volumes, start on disc 1, disc 0 stays listed as unlearned
    Output: restore acquires disc 0 once and finishes with both files
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        "oddarchiver.restore_plan.discs_with_unlearned_sessions",
        lambda catalog, learned, missing: [0],
    )
    iso0, iso1, backend, catalog, manifests = _two_file_archive(tmp_path)
    dest = tmp_path / "out"
    acquisition = _OnceThenFail([iso0, iso1], [0])
    result = restore_archive(
        dest, backend, NullCrypto(), catalog, manifests=manifests,
        acquisition=acquisition,
    )
    assert result.paused is False
    assert result.failures == 0
    assert (dest / "a.bin").read_bytes() == b"aaa"
    assert (dest / "b.bin").read_bytes() == b"bbb"
    assert len(acquisition.calls) == 1
    assert list(acquisition.calls[0]) == [0]
    saved = load_restore_state(dest, catalog.archive_id)
    assert set(saved.read_discs) == {0, 1}
    assert saved.done_files["a.bin"] == 0
    assert saved.done_files["b.bin"] == 1
