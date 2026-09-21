"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Local ledger of the last known good session count. It records how
    many sessions a post-burn fast verify has confirmed. The key is
    the pair of archive label and backend identity, so a later torn
    read cannot look like an empty disc. The label alone is not a
    safe key. Two different discs or ISOs can share a label that a
    user chose (this tool's own --label default is "ARCHIVE").
"""
# Imports
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.manifest import Manifest

# Globals
_log = logging.getLogger(__name__)

_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Ledger:
    """Persisted last-known-good session count for one archive label."""

    confirmed_sessions: int
    last_verified: str
    label: str = ""


# Functions


def backend_identity(backend: "BurnBackend") -> str:
    """
    Input:  backend - BurnBackend instance
    Output: str - canonical identifier (device path or ISO path)
    Details:
        This function uses duck typing. It does not import the concrete
        DiscBackend or ISOBackend classes. This avoids an import cycle
        between cli and ledger. The function copies the logic of
        cli._backend_id. If a backend has neither attribute, the function
        returns the class name. This gives each backend type a stable key
        instead of a crash.
    """
    device = getattr(backend, "device", None)
    if device:
        return str(device)
    iso_path = getattr(backend, "iso_path", None)
    if iso_path:
        return str(iso_path)
    return f"unknown:{type(backend).__name__}"


def ledger_dir() -> Path:
    """
    Input:  None
    Output: Path - directory that holds <label>.ledger.json files
    Details:
        The function uses $XDG_STATE_HOME/oddarchiver when the
        XDG_STATE_HOME variable is set. Otherwise, it uses
        ~/.local/state/oddarchiver, as specified in Assist/TODO.md 22c.
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "oddarchiver"
    return Path.home() / ".local" / "state" / "oddarchiver"


def ledger_path(label: str, backend_id: str) -> Path:
    """
    Input:  label      - archive volume label
            backend_id - device path or ISO path that identifies the
                         physical medium or target (matches cli._backend_id)
    Output: Path to the ledger JSON file for that archive
    Details:
        The function uses label and backend_id together as the key, not
        label alone. Two different discs or ISOs can share a label.
        If the function used label alone as the key, it would combine
        their confirmed session counts. The function hashes backend_id
        to keep the filename stable and safe for the filesystem,
        regardless of path length or characters.
    """
    safe = _SAFE_LABEL_RE.sub("_", label).strip("._") or "archive"
    digest = hashlib.sha256(backend_id.encode("utf-8")).hexdigest()[:12]
    return ledger_dir() / f"{safe}-{digest}.ledger.json"


def load_ledger(label: str, backend_id: str) -> Ledger | None:
    """
    Input:  label, backend_id - archive label and physical medium identity
    Output: Ledger, or None if the file is missing or corrupt
    Details:
        A missing or corrupt ledger is only advisory. The caller uses a
        cautious baseline instead of failing. A corrupt file logs a
        warning. A missing file is normal during first init. It stays
        silent, with no warning logged.
    """
    path = ledger_path(label, backend_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("Corrupt ledger at %s: %s", path, exc)
        return None
    try:
        confirmed = int(raw["confirmed_sessions"])
        last_verified = str(raw["last_verified"])
    except (KeyError, TypeError, ValueError) as exc:
        _log.warning("Corrupt ledger at %s: missing fields: %s", path, exc)
        return None
    if confirmed < 0:
        _log.warning("Corrupt ledger at %s: confirmed_sessions=%s", path, confirmed)
        return None
    return Ledger(
        confirmed_sessions=confirmed,
        last_verified=last_verified,
        label=str(raw.get("label", label)),
    )


def record_confirmed(label: str, backend_id: str, confirmed_sessions: int) -> None:
    """
    Input:  label              - archive volume label
            backend_id         - physical medium identity (part of the ledger key)
            confirmed_sessions - number of sessions that passed a post-burn fast verify
    Output: None
    Details:
        The function writes the ledger only after a post-burn fast verify
        passes for a session. The write is atomic: the function writes to a
        .tmp file, then calls os.replace. The function never lowers an
        existing confirmed count. This rule stops a later recover-scan of a
        torn read from erasing the last known-good baseline.
    """
    existing = load_ledger(label, backend_id)
    if existing is not None and existing.confirmed_sessions > confirmed_sessions:
        _log.warning(
            "Refusing to lower ledger for %r from %d to %d",
            label, existing.confirmed_sessions, confirmed_sessions,
        )
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "confirmed_sessions": confirmed_sessions,
        "last_verified": now,
        "label": label,
    }
    path = ledger_path(label, backend_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    _log.info(
        "Ledger updated for %r: confirmed_sessions=%d at %s",
        label, confirmed_sessions, now,
    )


def check_session_coverage(
    manifests: Sequence["Manifest"],
    *,
    label: str,
    backend_id: str,
    recover_scan: bool = False,
    disc_session_count: int = 0,
    backend: "BurnBackend | None" = None,
) -> None:
    """
    Input:  manifests          - session manifests already read from the disc
            label              - archive volume label (part of the ledger key)
            backend_id         - physical medium identity (part of the ledger key)
            recover_scan       - if True, log a WARNING and return instead of raising an error
            disc_session_count - MMC session_count used as initialized-or-blank, not as an archive session count
            backend            - optional backend, used to name the gap error
    Output: None
    Details:
        This function raises RuntimeError when the manifest list is shorter
        than the ledger's confirmed_sessions value. This means a read
        failure below the known-good count, not a new archive.
        A gap above confirmed_sessions is a normal end of scan. It does not
        raise an error.
        A missing or corrupt ledger makes the function maximally cautious.
        If the disc is initialized (disc_session_count > 0) and yields zero
        readable manifests, the function still raises a loud failure. It
        does not treat this case as an empty archive.
        disc_session_count is an initialized-or-blank signal, not an
        archive session count. On BD-R with SRM+POW formatting, mediainfo
        reports session_count=1 no matter how many archive sessions the
        disc holds. growisofs appends each new session as a track inside
        one MMC session.
        When the ledger is missing and no manifest is readable, the true
        archive session count is unknown. The function does not invent a
        count or a missing-index list from disc_session_count.
        The --recover-scan option skips the raise. When a known-good count
        exists, it logs which sessions the function could not confirm.
        When the count is unknown, it logs that fact and returns.
    """
    readable = len(manifests)
    ledger = load_ledger(label, backend_id) if label else None

    if ledger is None:
        if readable == 0 and disc_session_count > 0:
            _handle_shortfall(
                readable, None, [], label, recover_scan,
                backend, source="no ledger; archive session count unknown",
            )
        return

    expected = ledger.confirmed_sessions
    if readable >= expected:
        return
    missing = list(range(readable, expected))
    _handle_shortfall(
        readable, expected, missing, label, recover_scan, backend,
        source=f"ledger last_verified={ledger.last_verified}",
    )


def _handle_shortfall(
    readable: int,
    expected: int | None,
    missing: list[int],
    label: str,
    recover_scan: bool,
    backend: "BurnBackend | None",
    source: str,
) -> None:
    """
    Input:  readable - how many manifests were read
            expected - known-good session count, or None when the count is unknown
            missing  - gap indexes when expected is known; empty when unknown
            label, recover_scan, backend, source - report context
    Output: None
    Details:
        If recover_scan is set, this function logs a WARNING message and
        returns. Otherwise, it raises a RuntimeError.
        When expected is None, the ledger is missing and no manifest was
        readable. The function does not invent a session count or a
        missing-index list. MMC session_count is not an archive session
        count, so the true count cannot be recovered from mediainfo.
        When expected is set, the error names the gap and the known-good
        session count. It also states the next step: retry after eject
        and reinsert, or use --recover-scan if the missing sessions are
        confirmed gone.
    """
    if expected is None:
        if recover_scan:
            _log.warning(
                "recover-scan: disc is initialized for %r (%s) but no "
                "session manifest could be read; true archive session "
                "count is unknown.",
                label, source,
            )
            return
        message = (
            "Read failure: disc is initialized but no session manifest "
            "could be read, and the true archive session count could "
            f"not be determined ({source}). Last readable session: none. "
            "This is a read failure, not an empty archive. Retry after "
            "eject/reinsert, or pass --recover-scan if you have confirmed "
            "the disc is genuinely empty of readable sessions."
        )
        _log.error("%s", message)
        raise RuntimeError(message)
    named = ", ".join(f"{s:03d}" for s in missing) or "(none)"
    gap_detail = ""
    if backend is not None and missing:
        gap_detail = " " + _describe_gap_error(backend, missing[0])
    if recover_scan:
        _log.warning(
            "recover-scan: ledger/disc expected %d session(s) for %r (%s) "
            "but only %d readable; unconfirmed sessions: %s.%s",
            expected, label, source, readable, named, gap_detail,
        )
        return
    last_readable = f"{readable - 1:03d}" if readable else "none"
    message = (
        f"Read failure for session {missing[0]:03d}: cannot treat as absent; "
        f"{expected} session(s) (000-{expected - 1:03d}) should exist "
        f"({source}). Last readable session: {last_readable}.{gap_detail} "
        f"This is a read failure, not an empty archive. Retry after "
        f"eject/reinsert, or pass --recover-scan if you have confirmed "
        f"the missing sessions are genuinely gone."
    )
    _log.error("%s", message)
    raise RuntimeError(message)


def _describe_gap_error(backend: "BurnBackend", session_idx: int) -> str:
    """
    Input:  backend     - BurnBackend to re-probe
            session_idx - first missing session index
    Output: str - one-line description of why that session is unreadable
    Details:
        This function tells apart two errors. The first is xorriso's
        message 'Cannot determine attributes of', which means the disc
        tree lacks the entry. The second is a RuntimeError after the
        retries run out. The original scan caught and discarded both
        errors. This function re-probes only to get the error text,
        after the code already has the list of manifests.
    """
    last_exc: BaseException | None = None
    for disc_path in (
        f"session_{session_idx:03d}/manifest.enc",
        f"session_{session_idx:03d}/manifest.json",
    ):
        try:
            backend.read_path(disc_path)
        except FileNotFoundError as exc:
            _log.warning("Gap probe %s: %s", disc_path, exc)
            text = str(exc)
            if "Cannot determine attributes of" in text:
                return (
                    f"Underlying failure: xorriso reports the medium tree "
                    f"lacks {disc_path!r} (Cannot determine attributes of)."
                )
            return (
                f"Underlying failure: {disc_path!r} is not present "
                f"({text})."
            )
        except RuntimeError as exc:
            _log.exception("Gap probe %s exhausted retries", disc_path)
            return (
                f"Underlying failure: read of {disc_path!r} exhausted "
                f"retries ({exc})."
            )
        except (OSError, ValueError) as exc:
            _log.exception("Gap probe %s failed", disc_path)
            last_exc = exc
            continue
    if last_exc is not None:
        return f"Underlying failure: {last_exc}."
    return "Underlying failure: session unreadable."


if __name__ == "__main__":
    pass
