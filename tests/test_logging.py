"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for oddarchiver/log.py — setup_logging, check_capacity, SUSPECT level.
"""
# Imports
import logging
import re
import sys
from pathlib import Path

import pytest

from oddarchiver.log import (
    SUSPECT_LEVEL,
    check_capacity,
    setup_logging,
    suspect,
)

# Helpers


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Isolate each test: save root logger state and restore after."""
    root = logging.getLogger()
    original_level = root.level
    original_handlers = root.handlers[:]
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)


# Tests


def test_log_file_created_with_correct_format(tmp_path):
    """Log file is created and first message matches TIMESTAMP LEVEL [module] message."""
    log_file = tmp_path / "subdir" / "oddarchiver.log"
    setup_logging(log_file)

    lg = logging.getLogger("oddarchiver.test_format")
    lg.info("hello world")

    assert log_file.exists(), "log file should be created by setup_logging"
    content = log_file.read_text()
    # Pattern: 2026-04-24T12:34:56Z INFO [oddarchiver.test_format] hello world
    pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z INFO \[oddarchiver\.test_format\] hello world"
    assert re.search(pattern, content), f"log line format mismatch:\n{content}"


def test_all_levels_match_format(tmp_path):
    """INFO, WARNING, ERROR, and SUSPECT lines each match TIMESTAMP LEVEL [module] message."""
    log_file = tmp_path / "levels.log"
    setup_logging(log_file)

    lg = logging.getLogger("oddarchiver.test_levels")
    lg.info("info msg")
    lg.warning("warn msg")
    lg.error("error msg")
    suspect(lg, "suspect msg")

    content = log_file.read_text()
    ts = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    mod = r"\[oddarchiver\.test_levels\]"
    assert re.search(rf"{ts} INFO {mod} info msg", content)
    assert re.search(rf"{ts} WARNING {mod} warn msg", content)
    assert re.search(rf"{ts} ERROR {mod} error msg", content)
    assert re.search(rf"{ts} SUSPECT {mod} suspect msg", content)


def test_suspect_level_value():
    """SUSPECT_LEVEL is between WARNING (30) and ERROR (40)."""
    assert logging.WARNING < SUSPECT_LEVEL < logging.ERROR


def test_capacity_79_pct_logs_info(tmp_path):
    """Capacity < 80% → INFO level."""
    log_file = tmp_path / "cap.log"
    setup_logging(log_file)
    lg = logging.getLogger("oddarchiver.test_cap_info")
    check_capacity(79.0, 5 * 1024 ** 3, lg)
    content = log_file.read_text()
    assert "INFO" in content
    assert "WARNING" not in content
    assert "ERROR" not in content


def test_capacity_81_pct_logs_warning(tmp_path):
    """Capacity ≥ 80% → WARNING level."""
    log_file = tmp_path / "cap.log"
    setup_logging(log_file)
    lg = logging.getLogger("oddarchiver.test_cap_warn")
    check_capacity(81.0, 2 * 1024 ** 3, lg)
    content = log_file.read_text()
    assert "WARNING" in content
    assert "ERROR" not in content


def test_capacity_96_pct_logs_error(tmp_path):
    """Capacity ≥ 95% → ERROR level."""
    log_file = tmp_path / "cap.log"
    setup_logging(log_file)
    lg = logging.getLogger("oddarchiver.test_cap_error")
    check_capacity(96.0, 500 * 1024 ** 2, lg)
    content = log_file.read_text()
    assert "ERROR" in content


def test_no_stdout_handler(tmp_path):
    """setup_logging attaches no stdout handler; only file + stderr."""
    log_file = tmp_path / "stdout_check.log"
    setup_logging(log_file)
    root = logging.getLogger()
    for h in root.handlers:
        stream = getattr(h, "stream", None)
        assert stream is not sys.stdout, "stdout must not be a logging target"


def test_parent_dirs_created(tmp_path):
    """setup_logging creates missing parent directories."""
    log_file = tmp_path / "a" / "b" / "c" / "oddarchiver.log"
    assert not log_file.parent.exists()
    setup_logging(log_file)
    assert log_file.parent.exists()


def test_duplicate_handlers_not_added(tmp_path):
    """Calling setup_logging twice with the same path does not add duplicate handlers."""
    log_file = tmp_path / "dedup.log"
    setup_logging(log_file)
    count_before = sum(
        1 for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", "") == str(log_file.resolve())
    )
    setup_logging(log_file)
    count_after = sum(
        1 for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", "") == str(log_file.resolve())
    )
    assert count_after == count_before
