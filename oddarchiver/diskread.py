"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Session-ordered disc-read acquisition for restore and verify (TODO 23d).
    SessionSource is the small interface both engines read blobs through:
    open_session(n) returns a local directory holding session n's content,
    materialized ahead of time in one batched backend.read_paths() call per
    session instead of one xorriso launch per blob. Every ManifestEntry blob
    path already embeds its own session number ("session_NNN/full/<hash>" or
    "session_NNN/deltas/<hash>"), so every implementation here returns the
    same kind of directory: a local mirror of the disc's own path layout,
    read as `source.open_session(n) / entry.file`.

    Two modes trade local disk usage for I/O shape:
      FullImageSource  (--read-mode full-copy)  extracts every session up
        front into one directory; needs local free space for the archive's
        whole used capacity, checked before extracting anything.
      SequentialSource (--read-mode sequential, default) extracts one
        session at a time into a wipe-and-reuse scratch directory, bounding
        local disk usage to the largest single session regardless of how
        many sessions a restore or verify walks.
    Both are backend-agnostic: ISOBackend's read_paths() is already a plain
    local copy, so the same code exercises DiscBackend and ISOBackend alike.
"""
# Imports
from __future__ import annotations

import abc
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.session import SPACE_SAFETY_MARGIN

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.manifest import Manifest

# Globals
_log = logging.getLogger(__name__)
READ_MODES = ("sequential", "full-copy")
DEFAULT_READ_MODE = "sequential"

# Functions


def _session_blob_paths(manifests: "list[Manifest]") -> dict[int, list[str]]:
    """
    Input:  manifests - Manifest list (any order)
    Output: {session_n: [blob paths referenced by that session's entries]}
    Details:
        Full entries (streamed or not) contribute entry.file; delta
        entries contribute entry.delta_file. A blob path is always
        under its own session's own "session_NNN/" prefix (see
        session.py's _write_chunk), so grouping by manifest.session is
        exact -- no entry ever points at another session's blob. A path
        that fails validate_blob_path (e.g. a manifest with a
        traversal attempt) is left out of the batch entirely rather
        than handed to backend.read_paths() -- the per-file
        reconstruction code (restore._read_full / _apply_delta_entry)
        re-validates and fails that one file the same way it always
        has; a batch source must never let one bad path abort every
        other file's read.
    """
    from oddarchiver.manifest import validate_blob_path

    by_session: dict[int, list[str]] = {}
    for m in manifests:
        paths = []
        for entry in m.entries:
            blob = entry.file if entry.type == "full" else entry.delta_file
            if not blob:
                continue
            try:
                validate_blob_path(blob)
            except ValueError:
                _log.warning(
                    "Excluding unrecognized blob path %r (session %d) from "
                    "batched read", blob, m.session,
                )
                continue
            paths.append(blob)
        by_session[m.session] = paths
    return by_session


def _materialize(backend: "BurnBackend", paths: list[str], root: Path) -> None:
    """
    Input:  backend - BurnBackend to batch-read from
            paths   - disc-relative blob paths to fetch
            root    - local directory to write them under, preserving path
    Output: None (writes files under root)
    Details:
        One backend.read_paths() call regardless of how many paths are
        requested -- the whole point of session-ordered acquisition. A
        path missing on the disc is silently absent locally too; a
        reader built on top of root raises FileNotFoundError for it,
        same as a direct backend.read_path() miss would.
    """
    if not paths:
        return
    raw = backend.read_paths(paths)
    for path, data in raw.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


class SessionSource(abc.ABC):
    """Shared interface restore's and verify's session-ordered replay engines read through."""

    @abc.abstractmethod
    def open_session(self, session_n: int) -> Path:
        """
        Input:  session_n - session index to make available locally
        Output: local directory holding that session's extracted content;
                entry.file / entry.delta_file resolve under it unchanged
        """


class FullImageSource(SessionSource):
    """--read-mode full-copy: every session extracted up front into one local directory."""

    def __init__(
        self,
        backend: "BurnBackend",
        manifests: "list[Manifest]",
        workdir: Path,
    ) -> None:
        """
        Input:  backend, manifests - archive to extract
                workdir            - local directory to extract into (created if absent)
        Output: N/A (class)
        Details:
            Checks free space against the archive's total used capacity
            (full_size_bytes for full entries, delta_size_bytes for delta
            entries -- a worst-case, pre-encryption-overhead estimate,
            same convention as session.py's own staging space check)
            before extracting anything, so a shortfall is reported up
            front instead of after a partial, wasted extraction. Raises
            SystemExit(1) naming the shortfall, matching the existing
            session.py space-check pattern, if the archive will not fit.
        """
        self._workdir = Path(workdir)
        self._workdir.mkdir(parents=True, exist_ok=True)
        needed = sum(
            entry.full_size_bytes if entry.type == "full" else entry.delta_size_bytes
            for m in manifests
            for entry in m.entries
        )
        free = shutil.disk_usage(self._workdir).free
        limit = free * SPACE_SAFETY_MARGIN
        if needed >= limit:
            _log.error(
                "full-copy read mode needs ~%d bytes for the archive's used "
                "capacity; %s has %d bytes free * %.2f safety margin = %d "
                "available. Free up space, or use --read-mode sequential to "
                "bound local usage to one session at a time.",
                needed, self._workdir, free, SPACE_SAFETY_MARGIN, int(limit),
            )
            raise SystemExit(1)
        by_session = _session_blob_paths(manifests)
        all_paths = [p for paths in by_session.values() for p in paths]
        _materialize(backend, all_paths, self._workdir)

    def open_session(self, session_n: int) -> Path:
        return self._workdir


class SequentialSource(SessionSource):
    """--read-mode sequential (default): one session's blobs on local disk at a time."""

    def __init__(
        self,
        backend: "BurnBackend",
        manifests: "list[Manifest]",
        workdir: Path,
    ) -> None:
        """
        Input:  backend, manifests - archive to extract from, on demand
                workdir            - scratch directory, wiped and reused per session
        Output: N/A (class)
        """
        self._backend = backend
        self._workdir = Path(workdir)
        self._by_session = _session_blob_paths(manifests)
        self._current: int | None = None

    def open_session(self, session_n: int) -> Path:
        """
        Input:  session_n - session to make locally available
        Output: workdir, now holding only session_n's blobs
        Details:
            A no-op when session_n is already the open session, so a
            caller touching several files within one session pays one
            batched read, not one per file. Any other session wipes and
            re-extracts, bounding local disk to one session's worth of
            blobs regardless of how many sessions a restore or verify
            walks in total.
        """
        if session_n == self._current:
            return self._workdir
        if self._workdir.exists():
            shutil.rmtree(self._workdir)
        self._workdir.mkdir(parents=True, exist_ok=True)
        _materialize(self._backend, self._by_session.get(session_n, []), self._workdir)
        self._current = session_n
        return self._workdir


def make_session_source(
    backend: "BurnBackend",
    manifests: "list[Manifest]",
    workdir: Path,
    mode: str = DEFAULT_READ_MODE,
) -> SessionSource:
    """
    Input:  backend, manifests - archive to read from
            workdir            - local scratch/extraction root
            mode               - "sequential" (default) or "full-copy"
    Output: SessionSource implementation matching mode
    Details:
        Raises ValueError for an unrecognized mode.
    """
    if mode == "sequential":
        return SequentialSource(backend, manifests, workdir)
    if mode == "full-copy":
        return FullImageSource(backend, manifests, workdir)
    raise ValueError(f"Unknown --read-mode {mode!r}; expected one of {READ_MODES}")

if __name__ == "__main__":
    pass
