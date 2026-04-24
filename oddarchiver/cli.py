"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Argument parsing and command dispatch for oddarchiver CLI.
    Implements: init, sync, restore, history, verify, status subcommands.
"""
# Imports
import argparse
import getpass
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.cache import CacheManager
from oddarchiver.config import resolve_config
from oddarchiver.crypto import make_crypto, NullCrypto
from oddarchiver.disc import ISOBackend, DiscBackend, parse_disc_size
from oddarchiver.log import check_capacity, setup_logging, suspect
from oddarchiver.manifest import (
    Manifest, read_manifest, write_manifest, build_disc_state,
)

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals
COMMANDS = ["init", "sync", "restore", "history", "verify", "status"]
_log = logging.getLogger(__name__)

# Functions


def build_parser() -> argparse.ArgumentParser:
    """
    Input:  None
    Output: argparse.ArgumentParser — fully configured top-level parser
    Details:
        Constructs parser with all six subcommands and their flags.
        Mutual exclusion: --dry-run and --test-iso cannot coexist.
    """
    parser = argparse.ArgumentParser(
        prog="oddarchiver",
        description="Incremental delta-compressed backups to write-once optical media.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    _add_init(sub)
    _add_sync(sub)
    _add_restore(sub)
    _add_history(sub)
    _add_verify(sub)
    _add_status(sub)

    return parser


def _add_init(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub — subparsers action to attach to
    Output: None
    Details:
        init <source> [flags] — full snapshot burn, session 0.
    """
    p = sub.add_parser("init", help="Create session 0 (full snapshot) on disc or ISO.")
    p.add_argument("source", help="Directory to archive.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--label", default="ARCHIVE", metavar="LABEL")
    p.add_argument("--encrypt", choices=["none", "passphrase", "keyfile"], default="none")
    p.add_argument("--key", metavar="PATH", help="Keyfile path (keyfile mode only).")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--disc-size", default=None, dest="disc_size", metavar="SIZE")
    p.add_argument("--prefill", metavar="SIZE")
    _add_dry_iso_mutex(p)


def _add_sync(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub — subparsers action
    Output: None
    Details:
        sync <source> — incremental session burn.
    """
    p = sub.add_parser("sync", help="Burn an incremental session.")
    p.add_argument("source", help="Directory to sync.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--no-cache", action="store_true", dest="no_cache")
    p.add_argument("--disc-size", default=None, dest="disc_size", metavar="SIZE")
    p.add_argument("--prefill", metavar="SIZE")
    _add_dry_iso_mutex(p)


def _add_restore(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub — subparsers action
    Output: None
    Details:
        restore <dest> — reconstruct source directory from disc/ISO.
    """
    p = sub.add_parser("restore", help="Reconstruct source from disc or ISO.")
    p.add_argument("dest", help="Destination directory.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--session", type=int, metavar="N")
    p.add_argument("--force", action="store_true")


def _add_history(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub — subparsers action
    Output: None
    Details:
        history — print session table from disc/ISO manifests.
    """
    p = sub.add_parser("history", help="List all sessions on disc or ISO.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")


def _add_verify(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub — subparsers action
    Output: None
    Details:
        verify — integrity check at specified depth.
    """
    p = sub.add_parser("verify", help="Check integrity of disc or ISO.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--level", choices=["fast", "checksum", "full"], default="fast")


def _add_status(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub — subparsers action
    Output: None
    Details:
        status — print disc/ISO state and any SUSPECT entries.
    """
    p = sub.add_parser("status", help="Show disc/ISO state and warnings.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")


def _add_dry_iso_mutex(_parser: argparse.ArgumentParser) -> None:
    """
    Input:  _parser — unused; kept for signature compatibility
    Output: None
    Details:
        --dry-run and --test-iso are intentionally allowed to coexist.
        --test-iso selects the ISO backend; --dry-run skips the actual write.
    """


def dispatch(args: argparse.Namespace) -> int:
    """
    Input:  args — parsed namespace from build_parser()
    Output: int — exit code (0 success, 1 error)
    Details:
        Validates cross-flag constraints then routes to the correct handler.
        Returns exit code; does not call sys.exit() directly.
    """
    # Apply config file defaults where CLI flags were not explicitly set (None)
    cfg = resolve_config(args)
    if getattr(args, "device", None) is None:
        args.device = cfg.device
    if getattr(args, "disc_size", None) is None:
        args.disc_size = cfg.disc_size
    setup_logging(cfg.log_file)

    handlers = {
        "init": _run_init,
        "sync": _run_sync,
        "restore": _run_restore,
        "history": _run_history,
        "verify": _run_verify,
        "status": _run_status,
    }
    handler = handlers.get(args.command)
    if handler is None:
        print(f"error: unknown command {args.command!r}", file=sys.stderr)
        return 1

    try:
        return handler(args)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _make_backend(args: argparse.Namespace) -> "BurnBackend":
    """
    Input:  args — parsed namespace with test_iso and disc_size attributes
    Output: BurnBackend — ISOBackend if --test-iso set, else DiscBackend
    """
    if getattr(args, "test_iso", None):
        size_str = getattr(args, "disc_size", "25gb")
        disc_size = parse_disc_size(size_str)
        return ISOBackend(Path(args.test_iso), disc_size=disc_size)
    return DiscBackend(getattr(args, "device", "/dev/sr0"))


def _read_disc_manifests(backend: "BurnBackend") -> list[Manifest]:
    """
    Input:  backend — BurnBackend to read from
    Output: list of Manifest objects in ascending session order (may be empty)
    Details:
        Reads all session manifests; skips unreadable sessions without error.
    """
    disc_info = backend.mediainfo()
    manifests: list[Manifest] = []
    with tempfile.TemporaryDirectory(prefix="oddarchiver_cli_") as tmp:
        tmp_path = Path(tmp)
        for s in range(disc_info.session_count):
            disc_path = f"session_{s:03d}/manifest.json"
            try:
                data = backend.read_path(disc_path)
            except OSError:
                continue
            tmp_file = tmp_path / f"manifest_{s:03d}.json"
            tmp_file.write_bytes(data)
            manifests.append(read_manifest(tmp_file))
    return manifests


def _crypto_for_disc(backend: "BurnBackend") -> "CryptoBackend":
    """
    Input:  backend — BurnBackend with at least one session
    Output: CryptoBackend matching the encryption mode stored in session 0 manifest
    Details:
        Reads encryption.mode from the first available manifest.
        "none" → NullCrypto; "passphrase" → PassphraseCrypto via env var;
        "keyfile" → raises NotImplementedError (key path not available post-init).
    """
    manifests = _read_disc_manifests(backend)
    if not manifests:
        return NullCrypto()
    enc = manifests[0].encryption
    mode = enc.get("mode", "none")
    if mode == "none" or not mode:
        return make_crypto("none")
    if mode == "passphrase":
        passphrase = os.environ.get("ODDARCHIVER_PASSPHRASE", "")
        if not passphrase:
            passphrase = getpass.getpass("Passphrase: ")
        return make_crypto("passphrase", passphrase=passphrase.encode())
    if mode == "keyfile":
        raise NotImplementedError(
            "keyfile mode requires --key on non-init commands (not yet wired)"
        )
    raise ValueError(f"Unknown encryption mode on disc: {mode!r}")


def _make_init_crypto(args: argparse.Namespace) -> "CryptoBackend":
    """
    Input:  args — parsed init namespace with --encrypt and optional --key
    Output: CryptoBackend for the requested mode
    Details:
        For passphrase: reads ODDARCHIVER_PASSPHRASE env var or prompts.
        For keyfile: reads the file at args.key.
    """
    mode = getattr(args, "encrypt", "none")
    if mode == "none":
        return make_crypto("none")
    if mode == "passphrase":
        passphrase = os.environ.get("ODDARCHIVER_PASSPHRASE", "")
        if not passphrase:
            passphrase = getpass.getpass("Passphrase: ")
        return make_crypto("passphrase", passphrase=passphrase.encode())
    if mode == "keyfile":
        key_path = getattr(args, "key", None)
        if not key_path:
            print("error: --key is required for keyfile mode.", file=sys.stderr)
            raise SystemExit(1)
        key_bytes = Path(key_path).read_bytes()
        return make_crypto("keyfile", key=key_bytes)
    raise ValueError(f"Unknown mode: {mode!r}")


def _encryption_block(crypto: "CryptoBackend") -> dict:
    """
    Input:  crypto — CryptoBackend instance
    Output: dict — encryption metadata to embed in manifest
    Details:
        Records mode for subsequent commands to reconstruct crypto.
    """
    from oddarchiver.crypto import NullCrypto, PassphraseCrypto, KeyfileCrypto
    if isinstance(crypto, NullCrypto):
        return {"mode": "none"}
    if isinstance(crypto, PassphraseCrypto):
        return {"mode": "passphrase", "cipher": "chacha20-poly1305", "kdf": "argon2id"}
    if isinstance(crypto, KeyfileCrypto):
        return {"mode": "keyfile", "cipher": "chacha20-poly1305"}
    return {"mode": "none"}


def _patch_manifest(staging: Path, session_n: int, label: str, encryption: dict) -> Manifest:
    """
    Input:  staging    — staging root directory
            session_n  — session index
            label      — disc label to apply
            encryption — encryption block dict
    Output: updated Manifest (also written to disc atomically)
    Details:
        Reads the manifest written by build_staging, sets label and encryption,
        then rewrites it atomically so the burnt manifest is complete.
    """
    session_dir = staging / f"session_{session_n:03d}"
    manifest = read_manifest(session_dir / "manifest.json")
    manifest.label = label
    manifest.encryption = encryption
    write_manifest(session_dir, manifest)
    return manifest


def _update_cache(
    cache: CacheManager,
    staging: Path,
    session_n: int,
    entries: list,
) -> None:
    """
    Input:  cache     — CacheManager to update
            staging   — staging directory root
            session_n — session that was just burned
            entries   — ManifestEntry list from the session manifest
    Output: None
    Details:
        Copies each encrypted blob from staging into the cache after a
        successful burn. Cache is updated only after burn succeeds.
    """
    for entry in entries:
        blob_rel = entry.file if entry.type == "full" else entry.delta_file
        if not blob_rel:
            continue
        blob_path = staging / blob_rel
        if blob_path.exists():
            cache.put(entry.path, session_n, blob_path.read_bytes())


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


def _run_dry_run(args: argparse.Namespace, is_init: bool = False) -> int:
    """
    Input:  args    — parsed namespace with source and backend flags
            is_init — True when called from _run_init (no prior disc state)
    Output: exit code (0 always unless disc read fails)
    Details:
        Scans source, diffs, computes delta sizes for changed files, prints
        rsync-style report.  Space overage is reported but never causes exit 1.
        No burn, no cache update, no manifest written to disc.
    """
    import hashlib
    from oddarchiver.delta import delta_or_full
    from oddarchiver.session import SPACE_SAFETY_MARGIN

    backend = _make_backend(args)
    disc_info = backend.mediainfo()

    if is_init:
        if disc_info.session_count > 0:
            print("warning: disc already initialized; skipping init.", file=sys.stderr)
            return 0
        disc_state: dict[str, str] = {}
        label = getattr(args, "label", "ARCHIVE")
        session_n = 0
        crypto = _make_init_crypto(args)
    else:
        if disc_info.session_count == 0:
            print("error: disc not initialized; run 'init' first.", file=sys.stderr)
            return 1
        manifests = _read_disc_manifests(backend)
        disc_state = build_disc_state(manifests)
        label = manifests[0].label if manifests else "ARCHIVE"
        session_n = disc_info.session_count
        crypto = _crypto_for_disc(backend)

    source = Path(args.source)
    source_files = sorted(f for f in source.rglob("*") if f.is_file())
    current_state = {
        str(f.relative_to(source)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in source_files
    }
    total_source_bytes = sum(f.stat().st_size for f in source_files)

    changed = {p for p in current_state if p in disc_state and current_state[p] != disc_state[p]}
    new_files = {p for p in current_state if p not in disc_state}
    unchanged_count = len(current_state) - len(changed) - len(new_files)

    print("DRY RUN -- no disc will be written")
    print()
    print(
        f"Scanning source: {source} "
        f"({len(current_state)} files, {_fmt_bytes(total_source_bytes)})"
    )
    print(f"Reading disc state: {label}, session {disc_info.session_count}")
    print()

    if not changed and not new_files:
        print("No changes detected.")
        return 0

    print("Changes detected:")
    cache = CacheManager()
    total_session_bytes = 0
    entry_count = 0

    for rel_path in sorted(changed):
        abs_path = source / rel_path
        full_size = abs_path.stat().st_size
        try:
            encrypted_old = cache.get_with_fallback(rel_path, session_n - 1, backend)
            old_bytes = crypto.decrypt(encrypted_old)
            kind, blob = delta_or_full(old_bytes, abs_path)
            blob_size = len(blob)
            if kind == "delta":
                reduction = 100.0 * (1.0 - blob_size / full_size) if full_size else 0.0
                print(
                    f"  [delta]  {rel_path:<40} "
                    f"{_fmt_bytes(full_size)} \u2192 {_fmt_bytes(blob_size)} delta "
                    f"({reduction:.1f}% reduction)"
                )
            else:
                pct = 100.0 * blob_size / full_size if full_size else 0.0
                print(
                    f"  [full]   {rel_path:<40} "
                    f"{_fmt_bytes(full_size)}  "
                    f"(delta {pct:.0f}% of full -- storing full)"
                )
        except Exception:  # noqa: BLE001
            blob_size = full_size
            print(f"  [full]   {rel_path:<40} {_fmt_bytes(full_size)}  (changed)")
        total_session_bytes += blob_size
        entry_count += 1

    for rel_path in sorted(new_files):
        abs_path = source / rel_path
        file_size = abs_path.stat().st_size
        print(f"  [full]   {rel_path:<40} {_fmt_bytes(file_size)}  (new file)")
        total_session_bytes += file_size
        entry_count += 1

    print()
    print(f"  {entry_count} files to write, {unchanged_count} unchanged")
    print()
    print(f"Session size:        {_fmt_bytes(total_session_bytes)}")
    print(f"Disc remaining:      {_fmt_bytes(disc_info.remaining_bytes)}")

    limit = disc_info.remaining_bytes * SPACE_SAFETY_MARGIN
    if disc_info.remaining_bytes > 0 and total_session_bytes < limit:
        pct = 100.0 * total_session_bytes / disc_info.remaining_bytes
        print(f"Space check:         OK (session is {pct:.2f}% of remaining)")
    elif disc_info.remaining_bytes > 0:
        pct = 100.0 * total_session_bytes / disc_info.remaining_bytes
        print(
            f"Space check:         OVERAGE "
            f"({_fmt_bytes(total_session_bytes)} is {pct:.1f}% of remaining -- would not fit)"
        )
    else:
        print("Space check:         OVERAGE (disc full)")

    print()
    print(f"Would burn as: session_{session_n:03d} on {label}")
    print("No disc written (dry run).")

    return 0


def _run_init(args: argparse.Namespace) -> int:
    """
    Input:  args — parsed init namespace
    Output: exit code
    Details:
        Builds session 0: scan source, build staging, burn, post-burn fast
        verify, update cache.  Exits 0 with warning if already initialized.
    """
    if getattr(args, "dry_run", False):
        return _run_dry_run(args, is_init=True)

    from oddarchiver import session as session_mod
    from oddarchiver import verify as verify_mod

    backend = _make_backend(args)
    disc_info = backend.mediainfo()

    if disc_info.session_count > 0:
        print("warning: disc already initialized; skipping init.", file=sys.stderr)
        return 0

    if getattr(args, "prefill", None):
        if isinstance(backend, ISOBackend):
            backend.prefill(parse_disc_size(args.prefill))

    crypto = _make_init_crypto(args)
    enc_block = _encryption_block(crypto)
    cache = CacheManager()

    staging = session_mod.build_staging(
        session_n=0,
        source=Path(args.source),
        disc_state={},
        backend=backend,
        cache=cache,
        crypto=crypto,
    )
    try:
        manifest = _patch_manifest(staging, 0, args.label, enc_block)
        backend.init(staging, args.label, expected_session_count=0)
        try:
            verify_mod.verify(backend, crypto, level="fast")
        except SystemExit:
            print("error: post-burn verify failed; cache not updated.", file=sys.stderr)
            return 1
        _update_cache(cache, staging, 0, manifest.entries)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return 0


def _run_sync(args: argparse.Namespace) -> int:
    """
    Input:  args — parsed sync namespace
    Output: exit code (0 even when no changes)
    Details:
        Reads disc state, diffs source, exits 0 silently on no changes.
        Otherwise: build staging, burn, verify, update cache.
    """
    if getattr(args, "dry_run", False):
        return _run_dry_run(args, is_init=False)

    from oddarchiver import session as session_mod
    from oddarchiver import verify as verify_mod

    backend = _make_backend(args)
    disc_info = backend.mediainfo()

    if disc_info.session_count == 0:
        print("error: disc not initialized; run 'init' first.", file=sys.stderr)
        return 1

    if getattr(args, "prefill", None):
        if isinstance(backend, ISOBackend):
            backend.prefill(parse_disc_size(args.prefill))

    manifests = _read_disc_manifests(backend)
    disc_state = build_disc_state(manifests)
    crypto = _crypto_for_disc(backend)
    enc_block = _encryption_block(crypto)
    label = manifests[0].label if manifests else "ARCHIVE"

    session_n = disc_info.session_count
    source = Path(args.source)

    # step 3-4: scan and diff to detect no-change early
    import hashlib

    def _sha256_file(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    current_state = {
        str(f.relative_to(source)): _sha256_file(f)
        for f in sorted(source.rglob("*"))
        if f.is_file()
    }
    changed = {p for p in current_state if p in disc_state and current_state[p] != disc_state[p]}
    new_files = {p for p in current_state if p not in disc_state}
    deleted_files = [p for p in disc_state if p not in current_state]

    if not changed and not new_files and not deleted_files:
        return 0  # silent exit on no change

    cache = CacheManager()
    staging = session_mod.build_staging(
        session_n=session_n,
        source=source,
        disc_state=disc_state,
        backend=backend,
        cache=cache,
        crypto=crypto,
    )
    try:
        manifest = _patch_manifest(staging, session_n, label, enc_block)
        backend.append(staging, label, expected_session_count=session_n)
        try:
            verify_mod.verify(backend, crypto, level="fast")
        except SystemExit:
            print("error: post-burn verify failed; cache not updated.", file=sys.stderr)
            return 1
        _update_cache(cache, staging, session_n, manifest.entries)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return 0


def _run_restore(args: argparse.Namespace) -> int:
    """
    Input:  args — parsed restore namespace
    Output: exit code (0 success, 1 on any failure)
    Details:
        Calls restore.restore() and translates failure_count to exit code.
    """
    from oddarchiver.restore import restore

    backend = _make_backend(args)
    crypto = _crypto_for_disc(backend)

    _, failures = restore(
        dest=Path(args.dest),
        backend=backend,
        crypto=crypto,
        session=getattr(args, "session", None),
        force=getattr(args, "force", False),
    )
    return 1 if failures else 0


def _run_history(args: argparse.Namespace) -> int:
    """
    Input:  args — parsed history namespace
    Output: exit code
    Details:
        Reads all manifests; prints one row per session.
        Columns: session, timestamp, files, total_bytes, encryption mode.
    """
    backend = _make_backend(args)
    manifests = _read_disc_manifests(backend)

    if not manifests:
        print("No sessions found.")
        return 0

    header = f"{'Session':<8} {'Timestamp':<22} {'Files':>6} {'Size':>10} Encryption"
    print(header)
    print("-" * len(header))

    for m in manifests:
        total_bytes = sum(e.full_size_bytes for e in m.entries)
        enc_mode = m.encryption.get("mode", "none") if m.encryption else "none"
        suspect_marker = " [SUSPECT]" if m.suspect else ""
        print(
            f"{m.session:03d}     {m.timestamp:<22} {len(m.entries):>6}"
            f" {_fmt_bytes(total_bytes):>10} {enc_mode}{suspect_marker}"
        )

    return 0


def _run_verify(args: argparse.Namespace) -> int:
    """
    Input:  args — parsed verify namespace
    Output: exit code (verify() raises SystemExit(1) on failure)
    Details:
        Reads disc crypto then calls verify.verify(); catches SystemExit.
    """
    from oddarchiver import verify as verify_mod

    backend = _make_backend(args)
    crypto = _crypto_for_disc(backend)

    try:
        verify_mod.verify(backend, crypto, level=args.level)
        return 0
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1


def _run_status(args: argparse.Namespace) -> int:
    """
    Input:  args — parsed status namespace
    Output: exit code
    Details:
        Prints disc label, session count, used/remaining space, capacity
        warnings, and any SUSPECT manifests.
    """
    backend = _make_backend(args)
    disc_info = backend.mediainfo()

    used_pct = (
        100 * disc_info.used_bytes / (disc_info.used_bytes + disc_info.remaining_bytes)
        if (disc_info.used_bytes + disc_info.remaining_bytes) > 0
        else 0
    )

    print(f"Label:     {disc_info.label or '(none)'}")
    print(f"Sessions:  {disc_info.session_count}")
    print(f"Used:      {_fmt_bytes(disc_info.used_bytes)} ({used_pct:.0f}%)")
    print(f"Remaining: {_fmt_bytes(disc_info.remaining_bytes)}")

    check_capacity(used_pct, disc_info.remaining_bytes, _log)

    manifests = _read_disc_manifests(backend)
    suspects = [m for m in manifests if m.suspect]
    if suspects:
        print(f"\nSUSPECT sessions ({len(suspects)}):")
        for m in suspects:
            suspect(_log, "session_%03d: manifest checksum mismatch", m.session)
            print(f"  session_{m.session:03d}: manifest checksum mismatch")

    return 0


def main() -> None:
    """
    Input:  None (reads sys.argv)
    Output: None
    Details:
        Entry point called by __main__.py and the installed console script.
        Exits with code returned by dispatch().
    """
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(dispatch(args))


if __name__ == "__main__":
    main()
