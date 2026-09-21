"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Local cache for encrypted blobs that mirror disc content.
    The cache is a performance optimization only. Correctness never
    depends on it. Cold-cache misses (TODO 23f) group by origin
    session. The code extracts the missing blobs of each session in
    one read_paths() call. It uses an already-loaded Manifest list when
    the caller has one.
"""
# Imports
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.disc import BurnBackend
    from oddarchiver.manifest import Manifest

# Globals
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "oddarchiver"
_log = logging.getLogger(__name__)


# Functions


def _blob_path(cache_dir: Path, path: str, session: int) -> Path:
    """
    Input:  cache_dir - cache root
            path      - relative file path
            session   - session number
    Output: Path for the blob file in the cache
    Details:
        Layout: <cache_dir>/blobs/<session>/<path>.blob
    """
    return cache_dir / "blobs" / str(session) / (path + ".blob")


def _manifest_path(cache_dir: Path) -> Path:
    """
    Input:  cache_dir - cache root
    Output: Path to the cache manifest JSON file
    """
    return cache_dir / "cache_manifest.json"


def _load_manifest(cache_dir: Path) -> dict:
    """
    Input:  cache_dir - cache root
    Output: dict of cache manifest entries, empty when absent or corrupt
    Details:
        Returns {} when the file does not exist or the JSON is
        invalid.
    """
    mp = _manifest_path(cache_dir)
    try:
        return json.loads(mp.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        _log.warning("cache manifest %s is not valid JSON; treating as empty", mp)
        return {}


def _save_manifest(cache_dir: Path, manifest: dict) -> None:
    """
    Input:  cache_dir - cache root
            manifest  - dict to persist
    Output: None
    Details:
        Writes through a .tmp file and then calls os.replace, so the
        replace is atomic.
    """
    mp = _manifest_path(cache_dir)
    mp.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, sort_keys=True))
    os.replace(tmp, mp)


def _blob_rel_for_path(manifest: "Manifest", path: str) -> str | None:
    """
    Input:  manifest - already-loaded session Manifest
            path     - relative source path to find
    Output: disc-relative blob path (entry.file or entry.delta_file), or None
    Details:
        The code scans manifest.entries one time. A missing path gives
        None, not an error. The caller decides whether to raise.
    """
    for entry in manifest.entries:
        if entry.path != path:
            continue
        blob_rel = entry.file if entry.type == "full" else entry.delta_file
        return blob_rel or None
    return None


def _load_session_manifest_from_disc(
    session: int,
    backend: "BurnBackend",
    crypto: "CryptoBackend | None",
) -> "Manifest":
    """
    Input:  session - session index whose manifest to load
            backend - BurnBackend to read from
            crypto  - CryptoBackend for an encrypted manifest (None = plaintext)
    Output: Manifest for that session
    Details:
        Tries manifest.enc, then manifest.json, through read_path.
        This matches the single-file fallback that existing callers
        (and tests) already mock. Raises RuntimeError when neither
        file is present.
    """
    from oddarchiver.manifest import read_manifest

    data: bytes | None = None
    suffix = ".json"
    for disc_path in (
        f"session_{session:03d}/manifest.enc",
        f"session_{session:03d}/manifest.json",
    ):
        try:
            data = backend.read_path(disc_path)
            suffix = Path(disc_path).suffix
            break
        except FileNotFoundError:
            _log.warning("cache fallback: %s not present", disc_path)
            continue
        except (OSError, ValueError) as exc:
            _log.warning("cache fallback: %s: %s", disc_path, exc)
            continue
    if data is None:
        raise RuntimeError(
            f"Cannot read session {session} manifest to resolve cache miss"
        )

    with tempfile.TemporaryDirectory(prefix="oddarchiver_cache_fallback_") as tmp:
        tmp_file = Path(tmp) / f"manifest_{session:03d}{suffix}"
        tmp_file.write_bytes(data)
        return read_manifest(tmp_file, crypto=crypto)


def _read_blob_from_disc(
    path: str,
    session: int,
    backend: "BurnBackend",
    crypto: "CryptoBackend | None",
    manifest: "Manifest | None" = None,
) -> bytes:
    """
    Input:  path     - relative file path to resolve
            session  - session that the entry belongs to
            backend  - BurnBackend to read the blob (and maybe the manifest) from
            crypto   - CryptoBackend for an encrypted manifest (None = plaintext)
            manifest - optional already-loaded Manifest for this session.
                       Skips a disc re-extract when present
    Output: raw blob bytes exactly as stored on disc for that entry
    Details:
        Finds the entry for path and reads its actual blob. It reads
        entry.file for a "full" entry and entry.delta_file for a
        "delta" entry. The blob name is a content hash, never a path.
        The code raises RuntimeError when it cannot find the manifest
        or the entry.
    """
    if manifest is None:
        try:
            manifest = _load_session_manifest_from_disc(session, backend, crypto)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Cannot read session {session} manifest to resolve cache miss "
                f"for {path!r}"
            ) from exc

    blob_rel = _blob_rel_for_path(manifest, path)
    if not blob_rel:
        raise RuntimeError(f"No entry for {path!r} in session {session} manifest")
    try:
        return backend.read_path(blob_rel)
    except (OSError, ValueError):
        _log.exception(
            "Failed to read blob %r for %r (session %d)",
            blob_rel, path, session,
        )
        raise RuntimeError(
            f"No entry for {path!r} in session {session} manifest"
        ) from None


class CacheManager:
    """
    Input:  cache_dir - root directory for the cache (default ~/.cache/oddarchiver)
    Output: N/A (class)
    Details:
        The code keys blobs by (session, path). get() treats a missing or
        partial blob as a miss. get_with_fallback() reads from the
        backend on a miss. prefetch_origin_blobs() batch-fills misses,
        grouped by origin session.
    """

    def __init__(self, cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
        self.cache_dir = cache_dir

    def get(self, path: str, session: int) -> bytes | None:
        """
        Input:  path    - relative file path
                session - session number the blob belongs to
        Output: encrypted blob bytes, or None on miss
        Details:
            Logs WARNING on a miss. Treats a partially written blob as
            a miss.
        """
        bp = _blob_path(self.cache_dir, path, session)
        manifest = _load_manifest(self.cache_dir)
        key = f"{session}:{path}"

        if key not in manifest:
            _log.warning("cache miss: %s session %d (not in manifest)", path, session)
            return None

        try:
            data = bp.read_bytes()
        except FileNotFoundError:
            _log.warning("cache miss: %s session %d (blob file missing)", path, session)
            return None

        # Treat a partial write as a miss. Check the expected size.
        expected = manifest[key]["size"]
        if len(data) != expected:
            _log.warning(
                "cache miss: %s session %d (partial write: got %d expected %d)",
                path, session, len(data), expected,
            )
            return None

        return data

    def put(self, path: str, session: int, blob: bytes) -> None:
        """
        Input:  path    - relative file path
                session - session number
                blob    - encrypted blob bytes
        Output: None
        Details:
            Writes the blob atomically and updates the cache manifest.
        """
        bp = _blob_path(self.cache_dir, path, session)
        bp.parent.mkdir(parents=True, exist_ok=True)

        tmp = bp.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, bp)

        manifest = _load_manifest(self.cache_dir)
        manifest[f"{session}:{path}"] = {"size": len(blob)}
        _save_manifest(self.cache_dir, manifest)

    def get_with_fallback(
        self,
        path: str,
        session: int,
        backend: "BurnBackend",
        crypto: "CryptoBackend | None" = None,
        manifests: "list[Manifest] | None" = None,
    ) -> bytes:
        """
        Input:  path      - relative file path
                session   - session number
                backend   - BurnBackend for disc read on cache miss
                crypto    - CryptoBackend to decrypt an encrypted manifest
                            (None for a plaintext manifest)
                manifests - optional already-loaded Manifest list (23b/23f).
                            Used to resolve the blob path without re-extracting
        Output: encrypted blob bytes, exactly as stored on disc for that entry
        Details:
            Returns the cached blob when present. On a miss, the code
            resolves the real entry for path. It uses the in-memory
            manifest when the caller passes one. Otherwise it reads
            the manifest of that session one time. Then it reads that
            actual entry blob (entry.file for "full", entry.delta_file
            for "delta") from the backend. Content-hash names under
            full/ or deltas/ identify the blobs. The plaintext path
            never appears in a blob name. A caller cannot guess the
            path. The code raises RuntimeError when it cannot find the
            manifest or the entry for path.
        """
        cached = self.get(path, session)
        if cached is not None:
            return cached

        manifest = None
        if manifests:
            for item in manifests:
                if item.session == session:
                    manifest = item
                    break
        blob = _read_blob_from_disc(path, session, backend, crypto, manifest=manifest)
        self.put(path, session, blob)
        return blob

    def prefetch_origin_blobs(
        self,
        needed: list[tuple[str, int]],
        backend: "BurnBackend",
        manifests: "list[Manifest] | None" = None,
        crypto: "CryptoBackend | None" = None,
    ) -> None:
        """
        Input:  needed    - (path, origin_session) pairs to keep cached
                backend   - BurnBackend for batched blob reads
                manifests - optional already-loaded Manifest list. Origin
                            sessions present here are not re-extracted
                crypto    - CryptoBackend for a session whose manifest is
                            not in manifests and that the code must load
                            from disc
        Output: None (cache populated for every resolvable miss)
        Details:
            Groups cache misses by origin session, in ascending order.
            It pulls the missing blobs of each origin session in one
            backend.read_paths() call. An origin session already in
            manifests is not re-read from disc. The code loads any
            other origin session one time and reuses it for every miss
            that points at it. A path with no matching entry stays
            uncached. A later get_with_fallback then raises the same
            RuntimeError it always has.
        """
        by_session: dict[int, list[str]] = {}
        for path, session in needed:
            if self.get(path, session) is not None:
                continue
            by_session.setdefault(session, []).append(path)
        if not by_session:
            return

        loaded: dict[int, Manifest] = {}
        if manifests:
            for item in manifests:
                loaded[item.session] = item

        for session in sorted(by_session):
            paths = by_session[session]
            manifest = loaded.get(session)
            if manifest is None:
                try:
                    manifest = _load_session_manifest_from_disc(
                        session, backend, crypto,
                    )
                except RuntimeError:
                    _log.exception(
                        "prefetch: cannot load origin session %d manifest",
                        session,
                    )
                    continue
                loaded[session] = manifest
            path_to_blob: dict[str, str] = {}
            for path in paths:
                blob_rel = _blob_rel_for_path(manifest, path)
                if blob_rel:
                    path_to_blob[path] = blob_rel
            if not path_to_blob:
                continue
            raw = backend.read_paths(list(path_to_blob.values()))
            for path, blob_rel in path_to_blob.items():
                data = raw.get(blob_rel)
                if data is None:
                    _log.warning(
                        "prefetch: blob %r for %r (session %d) missing on disc",
                        blob_rel, path, session,
                    )
                    continue
                self.put(path, session, data)


if __name__ == "__main__":
    pass
