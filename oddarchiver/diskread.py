"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Session-ordered disc-read acquisition for restore and verify (TODO 23d).
    SessionSource is the small interface both engines read blobs through:
    open_session(n) returns a local directory holding the content of
    session n, materialized ahead of time in one batched
    backend.extract_paths() call per session instead of one xorriso
    launch per blob. Every ManifestEntry blob path already embeds its
    own session number ("session_NNN/full/<hash>" or
    "session_NNN/deltas/<hash>"), so every implementation here returns
    the same kind of directory: a local mirror of the disc path layout,
    read as `source.open_session(n) / entry.file`.

    Two modes trade local disk usage for I/O shape:
      FullImageSource  (--read-mode full-copy)  extracts every session up
        front into one directory. It needs local free space for the whole
        used capacity of the archive, checked before extracting anything,
        then again against real extracted sizes.
      SequentialSource (--read-mode sequential, default) extracts one
        session at a time into a wipe-and-reuse scratch directory, bounding
        local disk usage to the largest single session regardless of how
        many sessions a restore or verify walks.
    Both extract blobs with extract_paths(), not read_paths().
"""
# Imports
from __future__ import annotations

import abc
import os
import stat
import tempfile
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.session import SPACE_SAFETY_MARGIN

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.manifest import Manifest

logger = logging.getLogger(__name__)
_log = logger
READ_MODES = ("sequential", "full-copy")
DEFAULT_READ_MODE = "sequential"

# Functions


def _valid_session_blobs(m: "Manifest") -> list[str]:
    """
    Input:  m - manifest whose entries reference blob paths
    Output: valid blob paths in entry order
    """
    from oddarchiver.manifest import validate_blob_path

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
    return paths


def _session_blob_paths(manifests: "list[Manifest]") -> dict[int, list[str]]:
    """
    Input:  manifests - Manifest list (any order)
    Output: {session_n: [blob paths referenced by entries of that session]}
    Details:
        Full entries (streamed or not) contribute entry.file. Delta
        entries contribute entry.delta_file. Grouping is by the
        manifest that owns the entry, not by the session number in the
        blob path string. A content-addressed dedup reference can point
        at a blob stored in an earlier session; SequentialSource still
        batches that path with the owning entry's session.

        A path that
        fails validate_blob_path (e.g. a manifest with a traversal
        attempt) stays out of the batch entirely, rather than reaching
        backend.read_paths(). The per-file reconstruction code
        (restore._read_full / _apply_delta_entry) re-validates and
        fails that one file the same way it always has. A batch source
        must never let one bad path abort the read of every other file.
    """
    by_session: dict[int, list[str]] = {}
    for m in manifests:
        by_session[m.session] = _valid_session_blobs(m)
    return by_session


def _materialize(
    backend: "BurnBackend",
    paths: list[str],
    root: Path,
    max_bytes: int | None = None,
) -> int:
    """
    Input:  backend - BurnBackend to batch-read from
            paths   - disc-relative blob paths to fetch
            root    - local directory to write them under, preserving path
            max_bytes - optional cap on total extracted bytes
    Output: total bytes written
    Details:
        One backend.extract_paths() call. Extract straight into root.
        Do not hold blob payloads in a dict of bytes.
    """
    if not paths:
        return 0
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    return backend.extract_paths(paths, root, max_bytes=max_bytes)


class SessionSource(abc.ABC):
    """Shared interface that the session-ordered replay engines of restore and verify read through."""

    @abc.abstractmethod
    def open_session(self, session_n: int) -> Path:
        """
        Input:  session_n - session index to make available locally
        Output: local directory holding the extracted content of that
                session. entry.file / entry.delta_file resolve under
                it unchanged
        """


class FullImageSource(SessionSource):
    """--read-mode full-copy: every session extracted up front into one local directory."""

    def __init__(
        self,
        backend: "BurnBackend",
        manifests: "list[Manifest]",
        workdir: Path,
        max_bytes: int | None = None,
    ) -> None:
        """
        Input:  backend, manifests - archive to extract
                workdir            - local directory to extract into (created if absent)
                max_bytes          - optional cap on real extracted bytes
        Output: N/A (class)
        Details:
            Checks free space against the total used capacity of the
            archive (full_size_bytes for full entries, delta_size_bytes
            for delta entries). This is a worst-case, pre-encryption-
            overhead estimate. It is not the only guard. Extraction
            then counts real bytes and stops at the cap. Raises
            SystemExit(1) if the declared size will not fit.
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
        cap = int(limit)
        if max_bytes is not None:
            cap = min(cap, max_bytes)
        if needed >= cap:
            _log.error(
                "full-copy read mode needs ~%d bytes for the archive's used "
                "capacity; %s has %d bytes free * %.2f safety margin = %d "
                "available. Free up space, or use --read-mode sequential to "
                "bound local usage to one session at a time.",
                needed, self._workdir, free, SPACE_SAFETY_MARGIN, cap,
            )
            raise SystemExit(1)
        by_session = _session_blob_paths(manifests)
        all_paths = [p for paths in by_session.values() for p in paths]
        _materialize(backend, all_paths, self._workdir, max_bytes=cap)

    def open_session(self, session_n: int) -> Path:
        return self._workdir


class SequentialSource(SessionSource):
    """--read-mode sequential (default): blobs of one session on local disk at a time."""

    def __init__(
        self,
        backend: "BurnBackend",
        manifests: "list[Manifest]",
        workdir: Path,
        max_bytes: int | None = None,
    ) -> None:
        """
        Input:  backend, manifests - archive to extract from, on demand
                workdir            - scratch directory, wiped and reused per session
                max_bytes          - optional cap on real extracted bytes
        Output: N/A (class)
        """
        self._backend = backend
        self._workdir = Path(workdir)
        self._by_session = _session_blob_paths(manifests)
        self._current: int | None = None
        self._max_bytes = max_bytes
        self._written = 0

    def open_session(self, session_n: int) -> Path:
        """
        Input:  session_n - session to make locally available
        Output: workdir, now holding only the blobs of session_n
        Details:
            A no-op when session_n is already the open session, so a
            caller touching several files within one session pays one
            batched read, not one per file. Any other session wipes the
            directory and re-extracts, bounding local disk to the blob
            volume of one session regardless of how many sessions a
            restore or verify walks in total.
        """
        if session_n == self._current:
            return self._workdir
        if self._workdir.exists() or self._workdir.is_symlink():
            info = self._workdir.lstat()
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid()):
                raise RuntimeError(f"Unsafe restore work directory: {self._workdir}")
            shutil.rmtree(self._workdir)
        self._workdir.mkdir(parents=True, exist_ok=True)
        remaining = None
        if self._max_bytes is not None:
            remaining = self._max_bytes - self._written
        written = _materialize(
            self._backend, self._by_session.get(session_n, []),
            self._workdir, max_bytes=remaining,
        )
        self._written += written
        self._current = session_n
        return self._workdir


def make_session_source(
    backend: "BurnBackend",
    manifests: "list[Manifest]",
    workdir: Path,
    mode: str = DEFAULT_READ_MODE,
    max_bytes: int | None = None,
) -> SessionSource:
    """
    Input:  backend, manifests - archive to read from
            workdir            - local scratch/extraction root
            mode               - "sequential" (default) or "full-copy"
            max_bytes          - optional cap on real extracted bytes
    Output: SessionSource implementation matching mode
    Details:
        Raises ValueError for an unrecognized mode.
    """
    if mode == "sequential":
        return SequentialSource(backend, manifests, workdir, max_bytes=max_bytes)
    if mode == "full-copy":
        return FullImageSource(backend, manifests, workdir, max_bytes=max_bytes)
    raise ValueError(f"Unknown --read-mode {mode!r}; expected one of {READ_MODES}")


if __name__ == "__main__":
    pass
