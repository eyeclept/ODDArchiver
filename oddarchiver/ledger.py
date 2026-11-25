"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Local ledger of the last known good session count. It records how
    many sessions a post-burn fast verify has confirmed. The key is
    the pair of archive label and backend identity, so a later torn
    read cannot look like an empty disc.

    The label alone is not a safe key. Two different discs or ISOs
    can share a label that a user chose (the --label default of this
    tool is "ARCHIVE").
"""
# Imports
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import re
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.manifest import Manifest

# Globals
logger = logging.getLogger(__name__)
_log = logger
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _known_fields(cls, data: dict) -> dict:
    names = {item.name for item in fields(cls)}
    extra = [key for key in data if key not in names]
    if extra:
        logger.debug("Ignoring unknown %s keys: %s", cls.__name__, extra)
    return {key: value for key, value in data.items() if key in names}



@dataclass
class Ledger:
    """Persisted last-known-good session membership for one volume."""

    confirmed_sessions: int
    last_verified: str
    label: str = ""
    archive_id: str | None = None
    volume_index: int | None = None
    session_ranges: list[list[int]] | None = None



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
        XDG_STATE_HOME variable has a value. Otherwise, it uses
        ~/.local/state/oddarchiver, as specified in Assist/TODO.md 22c.
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    directory = Path(xdg) / "oddarchiver" if xdg else Path.home() / ".local" / "state" / "oddarchiver"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"State directory is not owned by this user: {directory}")
    directory.chmod(0o700)
    return directory


def ledger_path(
    label: str, backend_id: str, volume_index: int | None = None,
    archive_id: str | None = None,
) -> Path:
    """
    Input:  label, backend_id - archive label and medium identity
            volume_index - disc index when known
            archive_id - archive identifier when known
    Output: Path to the ledger JSON file
    Details:
        With archive_id, the filename keys on a hash of the archive id
        plus the volume index, so two archives that share a label never
        share a torn-read guard. Without archive_id, the legacy name
        uses the label and a hash of backend_id; those files are only
        read back as migration input for the matching archive.
    """
    if archive_id:
        digest = hashlib.sha256(archive_id.encode("utf-8")).hexdigest()[:12]
        suffix = f"-vol{volume_index}" if volume_index is not None else ""
        return ledger_dir() / f"arch-{digest}{suffix}.ledger.json"
    safe = _SAFE_LABEL_RE.sub("_", label).strip("._") or "archive"
    digest = hashlib.sha256(backend_id.encode("utf-8")).hexdigest()[:12]
    if volume_index is None:
        return ledger_dir() / f"{safe}-{digest}.ledger.json"
    return ledger_dir() / f"{safe}-{digest}-vol{volume_index}.ledger.json"



def _parse_ledger_raw(raw: object, path: Path, label: str) -> Ledger | None:
    """Input: decoded JSON, path, label. Output: Ledger, or None if corrupt."""
    try:
        if not isinstance(raw, dict):
            raise ValueError("ledger is not an object")
        raw = _known_fields(Ledger, raw)
        confirmed = int(raw["confirmed_sessions"])
        last_verified = str(raw["last_verified"])
        if confirmed < 0:
            raise ValueError("confirmed_sessions is negative")
        ranges = None
        if "session_ranges" in raw:
            from oddarchiver.volume import parse_session_ranges
            ranges = parse_session_ranges(raw.get("session_ranges"))
        archive_id = raw.get("archive_id")
        volume_index = raw.get("volume_index")
        if archive_id is not None and not isinstance(archive_id, str):
            raise ValueError("invalid archive_id")
        # scbc ignore[type-equality] Reject bool as a volume index
        if volume_index is not None and (type(volume_index) is not int or volume_index < 0):
            raise ValueError("invalid volume_index")
        return Ledger(
            confirmed_sessions=confirmed,
            last_verified=last_verified,
            label=str(raw.get("label", label)),
            archive_id=archive_id,
            volume_index=volume_index,
            session_ranges=ranges,
        )
    except (KeyError, TypeError, ValueError) as exc:
        _log.warning("Corrupt ledger at %s: %s", path, exc)
        return None


def _read_ledger_file(path: Path, label: str) -> Ledger | None:
    """Input: ledger path and label. Output: Ledger, or None if missing or corrupt."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("Corrupt ledger at %s: %s", path, exc)
        return None
    return _parse_ledger_raw(raw, path, label)


def _migrated_label_ledger(
    label: str, backend_id: str, volume_index: int | None,
    archive_id: str,
) -> Ledger | None:
    """
    Input: archive identity and the legacy label-plus-backend key
    Output: Ledger from a legacy label-keyed file, or None
    Details:
        A legacy file has no archive_id. It counts as this archive only
        when its label matches the archive's remembered label. One-way
        migration: the caller rewrites the row under the archive-id key.
    """
    legacy = _read_ledger_file(ledger_path(label, backend_id), label)
    if legacy is None or legacy.archive_id is not None:
        return None
    from oddarchiver.catalog import remembered_archive_label
    remembered = remembered_archive_label(archive_id)
    if remembered is not None and remembered != label:
        return None
    legacy.archive_id = archive_id
    legacy.volume_index = volume_index
    return legacy


def load_ledger(
    label: str, backend_id: str, volume_index: int | None = None,
    archive_id: str | None = None,
) -> Ledger | None:
    """
    Input:  label, backend_id - archive label and physical medium identity
            volume_index - disc index when known
            archive_id - archive identifier when known
    Output: Ledger, or None when the file is absent or corrupt
    Details:
        A missing or corrupt ledger is only advisory. With archive_id,
        prefer the per-volume archive-id file, then the archive-id file,
        then one legacy label-keyed file whose label matches this
        archive. Without archive_id, keep the legacy read.
    """
    if archive_id:
        if volume_index is not None:
            specific = _read_ledger_file(
                ledger_path(label, backend_id, volume_index, archive_id), label,
            )
            if specific is not None:
                return specific
            general = _read_ledger_file(
                ledger_path(label, backend_id, None, archive_id), label,
            )
            if general is not None:
                return general
        else:
            general = _read_ledger_file(
                ledger_path(label, backend_id, None, archive_id), label,
            )
            if general is not None:
                return general
        return _migrated_label_ledger(label, backend_id, volume_index, archive_id)
    if volume_index is not None:
        specific = _read_ledger_file(ledger_path(label, backend_id, volume_index), label)
        if specific is not None:
            return specific
    return _read_ledger_file(ledger_path(label, backend_id), label)


def _write_ledger_file(path: Path, payload: dict) -> None:
    """Input: destination path and JSON payload. Output: None; atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record_confirmed(
    label: str,
    backend_id: str,
    confirmed_sessions: int,
    *,
    archive_id: str | None = None,
    volume_index: int | None = None,
    session_ranges: list[list[int]] | None = None,
) -> None:
    """
    Input:  label, backend_id, confirmed_sessions - volume identity and count
            archive_id, volume_index, session_ranges - fill membership when known
    Output: None
    Details:
        Write after a post-burn fast verify passes. Never lower an
        existing confirmed count. With archive_id, write the archive-id
        key only: the per-volume file when volume_index is set, and the
        whole-archive file for a later volume-less read. Without
        archive_id, keep the legacy label-plus-backend file. One
        archive's write never touches another archive's count.
    """
    existing = load_ledger(label, backend_id, volume_index, archive_id=archive_id)
    if existing is not None and existing.confirmed_sessions > confirmed_sessions:
        _log.warning(
            "Refusing to lower ledger for %r from %d to %d",
            label, existing.confirmed_sessions, confirmed_sessions,
        )
        confirmed_sessions = existing.confirmed_sessions
        if session_ranges is None:
            session_ranges = existing.session_ranges
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "confirmed_sessions": confirmed_sessions,
        "last_verified": now,
        "label": label,
    }
    if archive_id:
        payload["archive_id"] = archive_id
    if volume_index is not None:
        payload["volume_index"] = volume_index
    if session_ranges:
        payload["session_ranges"] = session_ranges
    if archive_id:
        _write_ledger_file(ledger_path(label, backend_id, None, archive_id), payload)
        if volume_index is not None:
            _write_ledger_file(
                ledger_path(label, backend_id, volume_index, archive_id), payload,
            )
    else:
        _write_ledger_file(ledger_path(label, backend_id), payload)
        if volume_index is not None:
            _write_ledger_file(ledger_path(label, backend_id, volume_index), payload)
    _log.info(
        "Ledger updated for %r: confirmed_sessions=%d at %s",
        label, confirmed_sessions, now,
    )


def _sessions_from_ranges(session_ranges: list[list[int]] | None) -> set[int]:
    """Input: inclusive pairs. Output: session numbers in those pairs."""
    sessions: set[int] = set()
    if not session_ranges:
        return sessions
    for pair in session_ranges:
        sessions.update(range(pair[0], pair[1] + 1))
    return sessions


def _loaded_sessions(manifests: Sequence["Manifest"]) -> set[int] | None:
    """Input: manifests. Output: session numbers, or None when a row has no session."""
    sessions: set[int] = set()
    for manifest in manifests:
        session = getattr(manifest, "session", None)
        # scbc ignore[type-equality] Reject bool as a session number
        if type(session) is not int:
            return None
        if session >= 0:
            sessions.add(session)
    return sessions


def _unreadable_session_count(manifests: Sequence["Manifest"]) -> int:
    """Input: manifests. Output: blank SUSPECT rows that have no session number."""
    count = 0
    for manifest in manifests:
        if getattr(manifest, "suspect", False) and getattr(manifest, "session", 0) < 0:
            count += 1
    return count


def _coverage_from_ranges(
    loaded: set[int],
    expected: set[int],
    label: str,
    recover_scan: bool,
    backend: "BurnBackend | None",
    source: str,
    unreadable: int = 0,
) -> None:
    """Input: loaded and expected session sets. Output: None; raise on a hole in expected."""
    missing = sorted(expected - loaded)
    if unreadable:
        missing = missing[unreadable:]
    if not missing:
        return
    _handle_shortfall(
        len(loaded), len(expected), missing, label, recover_scan, backend,
        source=source, range_check=True,
    )


def _coverage_from_expected(
    manifests: Sequence["Manifest"],
    label: str,
    recover_scan: bool,
    backend: "BurnBackend | None",
    ledger: object,
    expected_set: set[int],
    loaded: set[int],
) -> None:
    """Input: expected session set and loaded sessions. Output: None."""
    source = (
        f"ledger last_verified={ledger.last_verified}"
        if ledger is not None else "volume.json session_ranges"
    )
    _coverage_from_ranges(
        loaded, expected_set, label, recover_scan, backend, source,
        unreadable=_unreadable_session_count(manifests),
    )


def _coverage_from_ledger(
    manifests: Sequence["Manifest"],
    label: str,
    recover_scan: bool,
    disc_session_count: int,
    backend: "BurnBackend | None",
    ledger: object,
) -> None:
    """Input: ledger fallback context. Output: None."""
    readable = len(manifests)
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


def check_session_coverage(
    manifests: Sequence["Manifest"],
    *,
    label: str,
    backend_id: str,
    recover_scan: bool = False,
    disc_session_count: int = 0,
    backend: "BurnBackend | None" = None,
    archive_id: str | None = None,
    volume_index: int | None = None,
    session_ranges: list[list[int]] | None = None,
) -> None:
    """
    Input:  manifests - session manifests already read from the disc
            label, backend_id - ledger key
            recover_scan - log a warning and return instead of raising
            disc_session_count - initialized-or-blank MMC signal
            backend - optional backend, used to name the gap error
            archive_id, volume_index, session_ranges - disc identity
    Output: None
    Details:
        When volume.json or the ledger lists session_ranges, this volume
        must hold those sessions. A hole inside that set is a torn read.
        Sessions that the ranges place elsewhere are not required.
        Without ranges, a shortfall against confirmed_sessions still
        uses the older 0..N-1 count.
    """
    ledger = (
        load_ledger(label, backend_id, volume_index, archive_id=archive_id)
        if label else None
    )
    expected_set = _sessions_from_ranges(session_ranges)
    if not expected_set and ledger is not None:
        expected_set = _sessions_from_ranges(ledger.session_ranges)
    loaded = _loaded_sessions(manifests)
    if expected_set and loaded is not None:
        _coverage_from_expected(
            manifests, label, recover_scan, backend, ledger, expected_set, loaded,
        )
        return
    _coverage_from_ledger(
        manifests, label, recover_scan, disc_session_count, backend, ledger,
    )




def _handle_shortfall(
    readable: int,
    expected: int | None,
    missing: list[int],
    label: str,
    recover_scan: bool,
    backend: "BurnBackend | None",
    source: str,
    range_check: bool = False,
) -> None:
    """
    Input:  readable - the number of manifests the scan read
            expected - known-good session count, or None when the count is unknown
            missing  - gap indexes when expected has a value
            label, recover_scan, backend, source - report context
            range_check - True when missing is a hole inside session_ranges
    Output: None
    Details:
        If the caller sets recover_scan, this function logs a WARNING and
        returns. Otherwise it raises RuntimeError.
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
    if range_check:
        message = (
            f"Read failure for session {missing[0]:03d}: cannot treat as absent; "
            f"this volume should hold session(s) {named} ({source}). "
            f"Readable count: {readable}.{gap_detail} "
            f"This is a read failure, not an empty archive. Retry after "
            f"eject/reinsert, or pass --recover-scan if you have confirmed "
            f"the missing sessions are genuinely gone."
        )
    else:
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



def _missing_gap_description(disc_path: str, exc: FileNotFoundError) -> str:
    """
    Input:  disc_path - missing manifest path on disc
            exc       - missing-path error returned by the backend
    Output: description of the missing session manifest
    """
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


def _describe_gap_error(backend: "BurnBackend", session_idx: int) -> str:
    """
    Input:  backend     - BurnBackend to re-probe
            session_idx - first missing session index
    Output: str - one line that states why that session is unreadable
    Details:
        This function tells apart two errors. The first is the message of
        xorriso, 'Cannot determine attributes of', which means the disc
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
            return _missing_gap_description(disc_path, exc)
        except RuntimeError as exc:
            _log.exception("Gap probe %s exhausted retries", disc_path)
            return (
                f"Underlying failure: read of {disc_path!r} exhausted "
                f"retries ({exc})."
            )
        except (OSError, ValueError) as exc:
            _log.exception("Gap probe %s failed", disc_path)
            last_exc = exc
    if last_exc is not None:
        return f"Underlying failure: {last_exc}."
    return "Underlying failure: session unreadable."


if __name__ == "__main__":
    pass
