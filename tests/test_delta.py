"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for Task 4: delta.py — compute_delta, apply_delta, delta_or_full,
    threshold logic, and process_files parallelism.
"""
# Imports
import os
from pathlib import Path

import pytest

from oddarchiver.delta import (
    DELTA_THRESHOLD,
    apply_delta,
    compute_delta,
    delta_or_full,
    process_files,
)

# Globals
_OLD = b"The quick brown fox jumps over the lazy dog.\n" * 80
_NEW = b"The quick brown fox jumps over the LAZY dog.\n" * 80


# Functions


def test_round_trip_exact(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        apply_delta(old, compute_delta(old, new_path)) must return bytes
        identical to new_path contents.
    """
    new_path = tmp_path / "new.txt"
    new_path.write_bytes(_NEW)
    delta = compute_delta(_OLD, new_path)
    restored = apply_delta(_OLD, delta)
    assert restored == _NEW


def test_round_trip_binary(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Round-trip must work for arbitrary binary content, not just text.
    """
    old = bytes(range(256)) * 64
    new = bytes(reversed(range(256))) * 64
    new_path = tmp_path / "bin.dat"
    new_path.write_bytes(new)
    assert apply_delta(old, compute_delta(old, new_path)) == new


def test_delta_or_full_returns_delta_for_small_change(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        A one-character change on a large file must produce a delta well
        below DELTA_THRESHOLD, so delta_or_full must return ("delta", ...).
    """
    new_path = tmp_path / "new.txt"
    new_path.write_bytes(_NEW)
    kind, data = delta_or_full(_OLD, new_path)
    assert kind == "delta"
    assert len(data) < DELTA_THRESHOLD * len(_NEW)


def test_delta_or_full_returns_full_when_threshold_exceeded(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        When old content is tiny and new content is large random bytes, the
        delta will exceed DELTA_THRESHOLD * full_size, so ("full", ...) is
        returned and the bytes match new_path contents.
    """
    old_tiny = b"\x00" * 16
    new_random = os.urandom(32 * 1024)
    new_path = tmp_path / "rand.bin"
    new_path.write_bytes(new_random)

    kind, data = delta_or_full(old_tiny, new_path)
    assert kind == "full"
    assert data == new_random


def test_delta_or_full_logs_decision(tmp_path, caplog):
    """
    Input:  tmp_path, caplog
    Output: None
    Details:
        delta_or_full must emit an INFO log containing "storing delta" or
        "storing full" for every call.
    """
    import logging
    new_path = tmp_path / "file.txt"
    new_path.write_bytes(_NEW)
    with caplog.at_level(logging.INFO, logger="oddarchiver.delta"):
        delta_or_full(_OLD, new_path)
    messages = " ".join(r.message for r in caplog.records)
    assert "storing" in messages


def test_process_files_preserves_order(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        process_files must return results in the same order as the input
        jobs list regardless of thread completion order.
    """
    files = []
    olds = []
    for i in range(5):
        content = f"version {i} of file content repeated many times\n".encode() * 60
        p = tmp_path / f"f{i}.txt"
        p.write_bytes(content)
        files.append(p)
        old = f"old version {i} content\n".encode() * 60
        olds.append(old)

    jobs = list(zip(olds, files))
    results = process_files(jobs, max_workers=3)

    assert len(results) == len(jobs)
    for (old, path), (kind, data) in zip(jobs, results):
        assert kind in ("delta", "full")
        if kind == "full":
            assert data == path.read_bytes()
        else:
            assert apply_delta(old, data) == path.read_bytes()


def test_compute_delta_raises_on_missing_file(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        compute_delta must raise RuntimeError when new_path does not exist.
    """
    with pytest.raises((RuntimeError, FileNotFoundError)):
        compute_delta(b"old content", tmp_path / "nonexistent.txt")


def test_apply_delta_raises_on_bad_delta(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        apply_delta must raise RuntimeError when delta_bytes is not a valid
        xdelta3 delta.
    """
    with pytest.raises(RuntimeError):
        apply_delta(b"base content", b"this is not a valid delta")
