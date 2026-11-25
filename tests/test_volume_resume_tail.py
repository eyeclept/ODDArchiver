"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for tail-only resume of an interrupted volume split. A burn
    that stopped after chunk k must resume at chunk k+1 on the next
    volume, using the boundary that chunk k ended at, and must not
    append an empty session to the inserted disc first.
"""
# Imports
import argparse
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from oddarchiver.cli import build_parser, dispatch
from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend, parse_disc_size
from oddarchiver.manifest import (
    Manifest, ManifestEntry, build_split_prefixes, load_session_manifests,
)
from tests.helpers import _make_args

# Globals
DISC = 1_000_000  # Under the ISO scaffolding constant, so no scaffold bytes.
FILE_BYTES = 1_400_000  # Chunk 0 fills the disc; chunk 1 needs volume 1.
_SPLIT_CODE = "big.bin"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


class _PromptableBackend:
    """Delegates to ISOBackend but is not one, so the swap prompt runs."""

    def __init__(self, iso_path: Path, disc_size: int) -> None:
        self._inner = ISOBackend(iso_path, disc_size=disc_size)
        self.disc_size = disc_size

    def init(self, staging, label, expected_session_count=0, **kwargs):
        return self._inner.init(
            staging, label, expected_session_count, **kwargs,
        )

    def append(self, staging, label, expected_session_count, **kwargs):
        return self._inner.append(
            staging, label, expected_session_count, **kwargs,
        )

    def mediainfo(self):
        return self._inner.mediainfo()

    def read_paths(self, paths):
        return self._inner.read_paths(paths)

    def read_path(self, path):
        return self._inner.read_path(path)

    def list_file_sizes(self):
        return self._inner.list_file_sizes()

    def invalidate_session_list(self):
        return self._inner.invalidate_session_list()


def _promptable_factory(args: argparse.Namespace) -> _PromptableBackend:
    """Input: parsed CLI namespace. Output: prompting test backend."""
    size_str = getattr(args, "disc_size", "50mb")
    return _PromptableBackend(Path(args.test_iso), parse_disc_size(size_str))


def _dispatch_promptable(args: argparse.Namespace, reply: str) -> int:
    """Input: CLI arguments and prompt reply. Output: exit code."""
    with patch("oddarchiver.cli._make_backend", _promptable_factory), \
         patch("builtins.input", return_value=reply):
        return dispatch(args)


def _dispatch_argv(argv: list[str]) -> int:
    """Parse argv and dispatch, matching a real CLI invocation."""
    return dispatch(build_parser().parse_args(argv))


def _interrupted_split(tmp_path: Path) -> tuple[Path, bytes, Path]:
    """
    Input:  tmp_path
    Output: source directory, payload, and the volume 0 ISO path
    Details:
        init burns chunk 0, then answers 'q' at the swap prompt, so
        chunk 1 of 2 is never written.
    """
    source = tmp_path / "src"
    source.mkdir()
    payload = os.urandom(FILE_BYTES)
    (source / _SPLIT_CODE).write_bytes(payload)
    iso = tmp_path / "archive.iso"
    rc = _dispatch_promptable(
        _make_args(
            command="init", source=str(source), test_iso=str(iso),
            disc_size=str(DISC),
        ),
        "q",
    )
    assert rc == 1
    assert iso.is_file()
    assert not (tmp_path / "archive.vol1.iso").exists()
    return source, payload, iso


def _sync(source: Path, iso: Path) -> int:
    """Input: source and volume 0 ISO. Output: sync exit code."""
    return dispatch(_make_args(
        command="sync", source=str(source), test_iso=str(iso),
        disc_size=str(DISC),
    ))


def _only_entry(backend: ISOBackend) -> ManifestEntry:
    """Input: one-volume backend. Output: its single manifest entry."""
    from oddarchiver.volume import read_volume_marker

    marker = read_volume_marker(backend)
    start = marker.first_session if marker is not None else 0
    manifests = load_session_manifests(
        backend, crypto=NullCrypto(), start_session=start, marker=marker,
    )
    entries = [entry for m in manifests for entry in m.entries]
    assert len(entries) == 1
    return entries[0]


def _restore_all(iso: Path, dest: Path) -> int:
    """Input: newest ISO and destination. Output: restore exit code."""
    return _dispatch_argv([
        "restore", str(dest), "--all-disks", "--test-iso", str(iso),
    ])


def test_resume_stages_only_the_missing_tail_chunk(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A file split into 2 chunks across 2 volumes, interrupted after
        volume 0, resumes on volume 1 with only chunk 1. Restore returns
        the file byte-exact and the archive uses exactly 2 volumes.
    """
    source, payload, iso = _interrupted_split(tmp_path)
    first = _only_entry(ISOBackend(iso, disc_size=DISC))
    assert first.volume_chunk == 0
    assert first.volume_chunk_count == 2
    first_size = first.volume_chunk_size
    assert first_size and first_size < FILE_BYTES

    assert _sync(source, iso) == 0

    vol1 = tmp_path / "archive.vol1.iso"
    assert vol1.is_file()
    tail = _only_entry(ISOBackend(vol1, disc_size=DISC))
    assert tail.volume_chunk == 1
    assert tail.volume_chunk_count == 2
    assert tail.volume_chunk_size == FILE_BYTES - first_size
    assert tail.result_checksum == first.result_checksum

    assert sorted(p.name for p in tmp_path.glob("*.iso")) == [
        "archive.iso", "archive.vol1.iso",
    ]
    dest = tmp_path / "dest"
    assert _restore_all(vol1, dest) == 0
    assert (dest / _SPLIT_CODE).read_bytes() == payload


def test_resume_after_source_change_restages_whole_file(tmp_path):
    """
    Input: interrupted split, then a changed source file
    Output: volume 1 holds a whole file, not a tail chunk at the old offset
    """
    source, _old_payload, iso = _interrupted_split(tmp_path)
    first = _only_entry(ISOBackend(iso, disc_size=DISC))
    first_size = first.volume_chunk_size
    new_payload = os.urandom(300_000)
    (source / _SPLIT_CODE).write_bytes(new_payload)

    assert _sync(source, iso) == 0

    vol1 = tmp_path / "archive.vol1.iso"
    assert vol1.is_file()
    vol1_entry = _only_entry(ISOBackend(vol1, disc_size=DISC))
    assert vol1_entry.volume_chunk != 1
    assert vol1_entry.volume_chunk_size != (FILE_BYTES - first_size if first_size else None)
    if vol1_entry.volume_chunk is not None:
        assert vol1_entry.volume_chunk == 0
    dest = tmp_path / "dest"
    assert _restore_all(vol1, dest) == 0
    assert (dest / _SPLIT_CODE).read_bytes() == new_payload



def test_resume_writes_no_empty_session_on_inserted_disc(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Nothing fits the inserted disc's leftover space, so the resume
        must not append an empty session to it. Volume 0 keeps its one
        session and volume 1 holds the missing chunk.
    """
    source, _payload, iso = _interrupted_split(tmp_path)
    assert ISOBackend(iso, disc_size=DISC).mediainfo().session_count == 1

    assert _sync(source, iso) == 0

    disc0 = ISOBackend(iso, disc_size=DISC)
    assert disc0.mediainfo().session_count == 1
    assert disc0.list_session_numbers() == [0]
    vol1 = tmp_path / "archive.vol1.iso"
    assert ISOBackend(vol1, disc_size=DISC).list_session_numbers() == [1]
    assert not list(tmp_path.glob("archive.vol2.iso"))


def test_closed_disc_skips_its_burn_and_prompts_for_a_new_volume(
    tmp_path, monkeypatch,
):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        A disc at the growisofs -M 4 GiB merge limit is not appendable
        and its packing budget is 0. With one new small file, packing
        plans an empty first bin. The run must not burn that empty
        session on the closed disc; it goes straight to the new volume.
    """
    import dataclasses

    from oddarchiver.disc import GROWISOFS_MERGE_LIMIT_BYTES

    original = ISOBackend.mediainfo

    def closed_aware_mediainfo(self):
        info = original(self)
        return dataclasses.replace(
            info,
            appendable=info.used_bytes < GROWISOFS_MERGE_LIMIT_BYTES,
        )

    monkeypatch.setattr(ISOBackend, "mediainfo", closed_aware_mediainfo)

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_bytes(b"A" * 1000)
    iso = tmp_path / "archive.iso"
    assert _dispatch_argv([
        "init", str(source), "--test-iso", str(iso),
        "--disc-size", str(DISC), "--encrypt", "none",
    ]) == 0
    assert ISOBackend(iso, disc_size=DISC).list_session_numbers() == [0]

    ISOBackend(iso, disc_size=DISC).prefill(GROWISOFS_MERGE_LIMIT_BYTES)
    assert ISOBackend(iso, disc_size=DISC).mediainfo().appendable is False
    (source / "b.txt").write_bytes(b"B" * 2000)

    assert _sync(source, iso) == 0

    assert ISOBackend(iso, disc_size=DISC).list_session_numbers() == [0]
    vol1 = tmp_path / "archive.vol1.iso"
    assert vol1.is_file()
    assert ISOBackend(vol1, disc_size=DISC).list_session_numbers() == [1]
    dest = tmp_path / "dest"
    assert _restore_all(vol1, dest) == 0
    assert (dest / "b.txt").read_bytes() == b"B" * 2000


def _split_manifest(session: int, entry: ManifestEntry) -> Manifest:
    """Input: session number and one chunk entry. Output: its manifest."""
    return Manifest(
        version=3, session=session, timestamp="t", source="s", label="L",
        based_on_session=session - 1 if session else None, encryption={},
        entries=[entry], deleted=[], manifest_checksum="",
    )


def _chunk(
    chunk: int, count: int, size: int | None, checksum: str = "abc",
) -> ManifestEntry:
    """Input: chunk index/count, payload size, checksum. Output: entry."""
    return ManifestEntry(
        path=_SPLIT_CODE, type="full", result_checksum=checksum,
        full_size_bytes=1000, file=f"session_000/full/blob_{chunk}",
        streamed=True, volume_chunk=chunk, volume_chunk_count=count,
        volume_chunk_size=size, original_size=1000,
    )


def test_build_split_prefixes_requires_a_clean_recorded_prefix():
    """
    Input:  none
    Output: None
    Details:
        Consecutive chunks 0..k of one count leave a prefix. A missing
        chunk size, a changed checksum, a skipped index, or a complete
        chain leaves no prefix, so the caller stages the whole file.
    """
    prefixes = build_split_prefixes([
        _split_manifest(0, _chunk(0, 3, 400)),
        _split_manifest(1, _chunk(1, 3, 400)),
    ])
    assert prefixes[_SPLIT_CODE].next_chunk == 2
    assert prefixes[_SPLIT_CODE].payload_offset == 800

    missing_size = build_split_prefixes([
        _split_manifest(0, _chunk(0, 3, 400)),
        _split_manifest(1, _chunk(1, 3, None)),
    ])
    assert missing_size == {}

    changed = build_split_prefixes([
        _split_manifest(0, _chunk(0, 3, 400)),
        _split_manifest(1, _chunk(1, 3, 400, checksum="def")),
    ])
    assert changed == {}

    skipped = build_split_prefixes([
        _split_manifest(0, _chunk(0, 3, 400)),
        _split_manifest(1, _chunk(2, 3, 400)),
    ])
    assert skipped == {}

    complete = build_split_prefixes([
        _split_manifest(0, _chunk(0, 1, 1000)),
    ])
    assert complete == {}
