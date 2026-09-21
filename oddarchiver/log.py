"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    This module gives oddarchiver structured plaintext logging.
    The format is: TIMESTAMP LEVEL [module] message. Timestamps use UTC.
    The module adds SUSPECT as a custom level (35) between WARNING and ERROR.
    setup_logging() sets up a file handler for all levels and a stderr handler for ERROR and above.
    check_capacity() logs the disk fill percentage at the matching level.
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
    Input:  N/A - subclass of logging.Formatter
    Output: N/A
    Details:
        Forces asctime to use UTC so the trailing Z in _DATE_FMT is accurate.
    """
    converter = time.gmtime


def _fmt_bytes(n: int) -> str:
    """
    Input:  n - the byte count
    Output: a string a person can read easily, for example "1.2 GiB"
    """
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TiB"


def setup_logging(log_file: Path, level: int = logging.INFO) -> None:
    """
    Input:  log_file - the path to the append-only log file. The function creates
                        missing parent directories.
            level    - the minimum log level. The default is INFO.
    Output: None
    Details:
        The function adds two handlers to the root logger:
          - A FileHandler at `log_file` for all messages at `level` and above.
          - A StreamHandler(stderr) for ERROR level and above only.
        You can call this function more than once. It skips duplicate handlers
        for the same path.
        The function keeps existing handlers. This does not disrupt library callers.
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = _UTCFormatter(fmt=_LOG_FMT, datefmt=_DATE_FMT)

    root = logging.getLogger()
    root.setLevel(min(root.level or logging.WARNING, level) if root.level else level)

    # Do not add a second file handler for the same path.
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

    # Do not add a second stderr handler.
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
    Input:  logger - the Logger object that writes the record
            msg    - the log message. Positional args interpolate into it, as with logger.info.
    Output: None
    Details:
        This function writes a record at SUSPECT_LEVEL (35). Use this function for
        manifest checksum failures that need operator review but do not stop the
        current operation.
    """
    if logger.isEnabledFor(SUSPECT_LEVEL):
        logger.log(SUSPECT_LEVEL, msg, *args, **kwargs)


def check_capacity(
    used_pct: float,
    remaining_bytes: int,
    logger: logging.Logger | None = None,
) -> None:
    """
    Input:  used_pct        - percentage of disc capacity in use (0 to 100)
            remaining_bytes - bytes still free on the disc or ISO
            logger          - logger for the message. If not given, the
                              function uses the logger of this module.
    Output: None
    Details:
        Thresholds:
          used_pct below 80    - INFO
          used_pct 80 or more  - WARNING (warns the operator, includes remaining space)
          used_pct 95 or more  - ERROR (disc is nearly full)
    """
    lg = logger or _log
    remaining_str = _fmt_bytes(remaining_bytes)

    if used_pct >= 95:
        lg.error(
            "Disc capacity %.0f%% used; %s remaining. Disc nearly full.",
            used_pct,
            remaining_str,
        )
    elif used_pct >= 80:
        lg.warning(
            "Disc capacity %.0f%% used; %s remaining",
            used_pct,
            remaining_str,
        )
    else:
        lg.info(
            "Disc capacity %.0f%% used; %s remaining",
            used_pct,
            remaining_str,
        )


def main() -> None:
    """
    Input:  None
    Output: None
    Details:
        This is a smoke test. It sends one message at each log level through
        setup_logging to a temporary file.
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
