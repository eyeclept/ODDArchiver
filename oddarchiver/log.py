"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Structured plaintext logging for oddarchiver.
    Format: TIMESTAMP LEVEL [module] message  (UTC timestamps)
    Adds SUSPECT as a custom level (35) between WARNING and ERROR.
    setup_logging() wires file handler (all levels) + stderr handler (ERROR+).
    check_capacity() logs disc fill percentage at the appropriate level.
"""
# Imports
import logging
import sys
import time
from pathlib import Path

# Globals
SUSPECT_LEVEL = 35
logging.addLevelName(SUSPECT_LEVEL, "SUSPECT")

_LOG_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"

_log = logging.getLogger(__name__)

# Functions


class _UTCFormatter(logging.Formatter):
    """
    Input:  N/A — subclass of logging.Formatter
    Output: N/A
    Details:
        Forces asctime to use UTC so the trailing Z in _DATE_FMT is accurate.
    """
    converter = time.gmtime


def _fmt_bytes(n: int) -> str:
    """
    Input:  n — byte count
    Output: human-readable string (e.g. "1.2 GiB")
    """
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TiB"


def setup_logging(log_file: Path, level: int = logging.INFO) -> None:
    """
    Input:  log_file — path to append-only log file; parent dirs created if missing
            level    — minimum log level (default INFO)
    Output: None
    Details:
        Attaches two handlers to the root logger:
          - FileHandler at `log_file` for all messages at `level` and above
          - StreamHandler(stderr) for ERROR and above only
        Safe to call multiple times; duplicate handlers of the same path are avoided.
        Existing handlers are NOT removed so library callers are not disrupted.
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = _UTCFormatter(fmt=_LOG_FMT, datefmt=_DATE_FMT)

    root = logging.getLogger()
    root.setLevel(min(root.level or logging.WARNING, level) if root.level else level)

    # Avoid adding a second file handler for the same path.
    existing_paths = {
        getattr(h, "baseFilename", None)
        for h in root.handlers
        if isinstance(h, logging.FileHandler)
    }
    if str(log_file.resolve()) not in existing_paths:
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(formatter)
        root.addHandler(fh)

    # Avoid adding a second stderr handler.
    has_stderr = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) is sys.stderr
        for h in root.handlers
    )
    if not has_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.ERROR)
        sh.setFormatter(formatter)
        root.addHandler(sh)

    root.setLevel(level)


def suspect(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """
    Input:  logger — Logger instance to emit on
            msg    — log message (positional args interpolated as with logger.info)
    Output: None
    Details:
        Emits a record at SUSPECT_LEVEL (35).  Use for manifest checksum failures
        that require operator review but do not halt the current operation.
    """
    if logger.isEnabledFor(SUSPECT_LEVEL):
        logger.log(SUSPECT_LEVEL, msg, *args, **kwargs)


def check_capacity(
    used_pct: float,
    remaining_bytes: int,
    logger: logging.Logger | None = None,
) -> None:
    """
    Input:  used_pct        — percentage of disc capacity used (0–100)
            remaining_bytes — bytes still available on disc/ISO
            logger          — Logger to emit on; defaults to this module's logger
    Output: None
    Details:
        Thresholds:
          used_pct < 80  → INFO
          used_pct ≥ 80  → WARNING  (warn operator; include remaining space)
          used_pct ≥ 95  → ERROR    (disc nearly full)
    """
    lg = logger or _log
    remaining_str = _fmt_bytes(remaining_bytes)

    if used_pct >= 95:
        lg.error(
            "Disc capacity %.0f%% used — %s remaining; disc nearly full",
            used_pct,
            remaining_str,
        )
    elif used_pct >= 80:
        lg.warning(
            "Disc capacity %.0f%% used — %s remaining",
            used_pct,
            remaining_str,
        )
    else:
        lg.info(
            "Disc capacity %.0f%% used — %s remaining",
            used_pct,
            remaining_str,
        )


def main() -> None:
    """
    Input:  None
    Output: None
    Details:
        Smoke-test: emit one message at each level to stdout-based logger.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as tf:
        log_path = Path(tf.name)

    setup_logging(log_path)
    lg = logging.getLogger("oddarchiver.log.smoke")
    lg.info("Info message")
    lg.warning("Warning message")
    lg.error("Error message")
    suspect(lg, "Suspect message")
    print(f"Log written to {log_path}")
    print(log_path.read_text())


if __name__ == "__main__":
    main()
