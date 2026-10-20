"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Local cache for encrypted file versions. A delta entry caches the
    encrypted full result after a verified burn. A cache miss reads
    the encrypted entry blob from disc, which can be a delta. Callers
    must check the decrypted checksum before they use it as file content.
    Cold-cache misses group by origin session.
"""
# Imports
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from oddarchiver.crypto import CryptoBackend
    from oddarchiver.disc import BurnBackend
    from oddarchiver.manifest import Manifest

# Globals
logger = logging.getLogger(__name__)
_log = logger


# Functions


def default_cache_dir() -> Path:
    """
    Input:  None
    Output: ~/.cache/oddarchiver for the current HOME
    Details:
        Reads HOME at each call, not at import. A changed HOME (tests,
        sudo -H) then selects its own cache.
    """
    return Path.home() / ".cache" / "oddarchiver"


def _blob_path(cache_dir: Path, path: str, session: int) -> Path:
    """
    Input: cache_dir, path, and session
    Output: Opaque blob path for this cache and session
    Details: The index holds the original key. The filename never holds it.
    """
    digest = hashlib.sha256(f"{session}:{_key_path(path)}".encode("utf-8")).hexdigest()
    return cache_dir / "blobs" / digest


def _key_path(path: str) -> str:
    """Input: relative file path. Output: slash-normalized cache key."""
    return Path(path).as_posix()


def _manifest_path(cache_dir: Path) -> Path:
    """
    Input:  cache_dir - cache root
    Output: Path to the cache manifest JSON file
    """
    return cache_dir / "cache_manifest.json"


def _secure_dir(path: Path) -> None:
    """Input: directory path. Output: None. Details: Reject links and foreign owners."""
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Unsafe cache directory: {path}")
    path.chmod(0o700)


def _legacy_cache_entry(key: str, entry: object) -> tuple[str, str, tuple[str, ...]] | None:
    """Input: old cache key and entry. Output: validated session, path, and parts, or None."""
    session, sep, path = key.partition(":")
    parts = Path(path).parts
    valid = (
        sep and session.isdecimal() and path and
        not Path(path).is_absolute() and
        ".." not in parts and "." not in parts and
        isinstance(entry, dict) and
        isinstance(entry.get("size"), int) and entry["size"] >= 0
    )
    if not valid:
        return None
    return session, path, parts


def _secure_legacy_blob_parents(blobs: Path, session: str, parts: tuple[str, ...]) -> None:
    """Input: blob root and legacy path components. Output: None; check existing parents."""
    directory = blobs
    for component in (session, *parts[:-1]):
        directory = directory / component
        if directory.exists() or directory.is_symlink():
            _secure_dir(directory)


def _migrate_legacy_blob(
    cache_dir: Path,
    blobs: Path,
    key: str,
    entry: dict,
    session: str,
    path: str,
    parts: tuple[str, ...],
) -> bool | None:
    """Input: old blob and validated key. Output: True if moved, False if present, None if dropped."""
    old = blobs / session / (path + ".blob")
    target = _blob_path(cache_dir, path, int(session))
    _secure_legacy_blob_parents(blobs, session, parts)
    moved = False
    if old.exists() or old.is_symlink():
        info = old.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size != entry["size"]):
            _log.warning("dropping corrupt old cache entry %r", key)
            old.unlink()
            return None
        _secure_dir(blobs)
        target_info = target.lstat() if target.exists() or target.is_symlink() else None
        if target_info is not None and not stat.S_ISREG(target_info.st_mode):
            raise RuntimeError(f"Unsafe cache blob: {target}")
        os.chmod(old, 0o600)
        os.replace(old, target)
        moved = True
    elif not target.is_file() or target.stat().st_size != entry["size"]:
        _log.warning("dropping missing old cache entry %r", key)
        return None
    target.chmod(0o600)
    entry["blob"] = target.name
    return moved


def _migrate(cache_dir: Path) -> None:
    """
    Input: cache root
    Output: None
    Details: Move old named blobs to opaque names. Save the index last.
    """
    if not cache_dir.exists() and not cache_dir.is_symlink():
        return
    # scbc boundary: Validate legacy cache paths and ownership before moving files.
    _secure_dir(cache_dir)
    manifest = _load_manifest(cache_dir)
    blobs = cache_dir / "blobs"
    if blobs.exists() or blobs.is_symlink():
        _secure_dir(blobs)
    moved = dropped = 0
    changed = False
    # scbc ignore[unnecessary-list-call] The loop deletes entries from manifest.
    for key, entry in list(manifest.items()):
        if isinstance(entry, dict) and "blob" in entry:
            continue
        parsed = _legacy_cache_entry(key, entry)
        if parsed is None:
            _log.warning("dropping invalid old cache entry %r", key)
            del manifest[key]
            dropped += 1
            continue
        session, path, parts = parsed
        result = _migrate_legacy_blob(cache_dir, blobs, key, entry, session, path, parts)
        if result is None:
            del manifest[key]
            dropped += 1
            continue
        moved += result
        changed = True
    if moved or dropped or changed:
        _save_manifest(cache_dir, manifest)
    if blobs.is_dir():
        for child in blobs.iterdir():
            if child.name.isdecimal() and child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
    if moved or dropped:
        _log.info("cache migration: moved %d blobs, dropped %d entries", moved, dropped)


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
        if mp.exists() or mp.is_symlink():
            info = mp.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError(f"Unsafe cache index: {mp}")
            mp.chmod(0o600)
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
        Writes through a unique same-directory temp file, fsyncs, then
        os.replace so the final cache_manifest.json is atomic. A unique
        name per call avoids a race on a reused .tmp path. On any
        failure before replace succeeds, the partial temp file is
        unlinked.
    """
    mp = _manifest_path(cache_dir)
    if not cache_dir.exists() and not cache_dir.is_symlink():
        cache_dir.mkdir(parents=True, mode=0o700)
    _secure_dir(cache_dir)
    fd = None
    tmp = None
    replaced = False
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix="cache_manifest.",
            suffix=".tmp",
            dir=str(mp.parent),
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(json.dumps(manifest, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, mp)
        replaced = True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                _log.warning("failed to close temp manifest fd for %s", tmp)
        if tmp is not None and not replaced:
            try:
                tmp.unlink()
            except OSError:
                _log.warning("failed to unlink partial temp manifest %s", tmp)


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


def _prefetch_manifest(
    session: int,
    loaded: dict[int, Manifest],
    backend: BurnBackend,
    crypto: CryptoBackend | None,
) -> Manifest | None:
    """Input: session, loaded manifests, backend, crypto. Output: manifest or None on load failure."""
    manifest = loaded.get(session)
    if manifest is None:
        try:
            manifest = _load_session_manifest_from_disc(session, backend, crypto)
        except RuntimeError:
            _log.exception(
                "prefetch: cannot load origin session %d manifest",
                session,
            )
            return None
        loaded[session] = manifest
    return manifest


def _prefetch_session_blobs(
    manager: CacheManager,
    session: int,
    paths: list[str],
    manifest: Manifest,
    backend: BurnBackend,
) -> None:
    """Input: cache, session, paths, manifest, backend. Output: None; cache available blobs."""
    path_to_blob: dict[str, str] = {}
    for path in paths:
        blob_rel = _blob_rel_for_path(manifest, path)
        if blob_rel:
            path_to_blob[path] = blob_rel
    if not path_to_blob:
        return
    raw = backend.read_paths(list(path_to_blob.values()))
    for path, blob_rel in path_to_blob.items():
        data = raw.get(blob_rel)
        if data is None:
            _log.warning(
                "prefetch: blob %r for %r (session %d) missing on disc",
                blob_rel, path, session,
            )
            continue
        manager.put(path, session, data)


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

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir if cache_dir is not None else default_cache_dir()
        self._migration_done = False

    def _ensure_migrated(self) -> None:
        """Input: None. Output: None. Details: Resume old-layout moves on first use."""
        if not self._migration_done:
            _migrate(self.cache_dir)
            self._migration_done = True

    def get(self, path: str, session: int) -> bytes | None:
        """
        Input:  path    - relative file path
                session - session number the blob belongs to
        Output: encrypted blob bytes, or None on miss
        Details:
            Logs WARNING on a miss. Treats a partially written blob as
            a miss.
        """
        self._ensure_migrated()
        bp = _blob_path(self.cache_dir, path, session)
        manifest = _load_manifest(self.cache_dir)
        key = f"{session}:{_key_path(path)}"

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
            A single put is one batch of size 1.
        """
        self.put_many([(path, session, blob)])

    def put_many(self, entries: Iterable[tuple[str, int, bytes]]) -> None:
        """
        Input: entries - stream of (path, session, blob) tuples
        Output: None
        Details: Write one blob at a time. Commit the index once.
        """
        self._ensure_migrated()
        manifest = _load_manifest(self.cache_dir)
        changed = False
        for path, session, blob in entries:
            bp = _blob_path(self.cache_dir, path, session)
            if not self.cache_dir.exists() and not self.cache_dir.is_symlink():
                self.cache_dir.mkdir(parents=True, mode=0o700)
            _secure_dir(self.cache_dir)
            _secure_dir(bp.parent)
            fd, tmp_name = tempfile.mkstemp(prefix="blob.", dir=str(bp.parent))
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(blob)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, bp)
            finally:
                if tmp.exists():
                    tmp.unlink()
            manifest[f"{session}:{_key_path(path)}"] = {"size": len(blob), "blob": bp.name}
            changed = True
        if changed:
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
        Output: encrypted cached version or encrypted on-disc entry blob
        Details:
            A delta cached after a burn contains the full new version.
            On a miss, the disc read returns entry.file for a full entry
            or entry.delta_file for a delta entry. The fallback also
            caches those raw bytes. Check the decrypted result against
            the recorded file checksum before use. The code raises
            RuntimeError when it cannot find the manifest or entry.
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
            Group misses by origin session. Each read_paths() call reads
            the entry blobs from disc. A delta entry adds its raw delta
            blob to the cache on a miss, not rebuilt file content.
            Callers must check the decrypted prior checksum. A path
            with no matching entry stays uncached. A later fallback
            call raises RuntimeError.
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
            manifest = _prefetch_manifest(session, loaded, backend, crypto)
            if manifest is None:
                continue
            _prefetch_session_blobs(self, session, paths, manifest, backend)


if __name__ == "__main__":
    pass
