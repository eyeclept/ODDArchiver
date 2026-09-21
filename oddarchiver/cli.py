"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Argument parsing and command dispatch for oddarchiver CLI.
    Implements: init, sync, restore, history, ls, browse, verify, status.
"""
# Imports
import argparse
import getpass
import json
import logging
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.cache import CacheManager
from oddarchiver.config import resolve_config
from oddarchiver.crypto import make_crypto, NullCrypto
from oddarchiver.disc import (
    ISOBackend, DiscBackend, parse_disc_size, BurnInterrupted,
    check_volume_identity,
)
from oddarchiver.log import check_capacity, setup_logging, suspect
from oddarchiver.diskread import DEFAULT_READ_MODE, READ_MODES
from oddarchiver.manifest import (
    Manifest, TreeEntry, read_manifest, write_manifest, build_disc_state,
    build_disc_origin, build_disc_tree, load_session_manifests,
    session_control_paths,
)

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals
COMMANDS = ["init", "sync", "restore", "history", "ls", "browse", "verify", "status"]
_log = logging.getLogger(__name__)
_BROWSE_HELP = (
    "Commands:\n"
    "  N       open directory N, or toggle file N in the selection\n"
    "  t N     toggle entry N (file or directory prefix) in the selection\n"
    "  ..      go up one directory\n"
    "  s       show the current selection\n"
    "  r       restore the selected paths (prompts for a destination)\n"
    "  q       quit without restoring\n"
    "  h       show this help"
)

# Functions


def build_parser() -> argparse.ArgumentParser:
    """
    Input:  None
    Output: argparse.ArgumentParser - fully configured top-level parser
    Details:
        Builds the parser with all subcommands and their flags.
        --dry-run and --test-iso may appear together.
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
    _add_ls(sub)
    _add_browse(sub)
    _add_verify(sub)
    _add_status(sub)

    return parser


def _add_init(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action to attach to
    Output: None
    Details:
        init <source> [flags] - full snapshot burn, session 0.
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
    p.add_argument("--mirror", metavar="PATH", help="Second drive or ISO to mirror each session to.")
    p.add_argument(
        "--recover-scan", action="store_true", dest="recover_scan",
        help="Bypass last-known-good ledger check; treat readable sessions as authoritative.",
    )
    _add_slow(p)
    _add_dry_iso_mutex(p)


def _add_sync(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action
    Output: None
    Details:
        sync <source> - incremental session burn.
    """
    p = sub.add_parser("sync", help="Burn an incremental session.")
    p.add_argument("source", help="Directory to sync.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--no-cache", action="store_true", dest="no_cache")
    p.add_argument("--disc-size", default=None, dest="disc_size", metavar="SIZE")
    p.add_argument("--prefill", metavar="SIZE")
    p.add_argument("--mirror", metavar="PATH", help="Second drive or ISO to mirror each session to.")
    p.add_argument("--key", metavar="PATH", help="Keyfile path (keyfile mode only).")
    p.add_argument(
        "--recover-scan", action="store_true", dest="recover_scan",
        help="Bypass last-known-good ledger check; treat readable sessions as authoritative.",
    )
    _add_slow(p)
    _add_dry_iso_mutex(p)


def _add_slow(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a burn command
    Output: None
    Details:
        Adds --slow. Post-burn verify then uses checksum level instead of fast.
    """
    p.add_argument(
        "--slow",
        action="store_true",
        dest="slow",
        help="Checksum every blob after the burn. Slower on a large archive.",
    )


def _add_read_mode(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a disc-read command
    Output: None
    Details:
        Adds --read-mode sequential|full-copy, defaulting to sequential.
        Used by restore and verify, the commands that extract session blobs.
    """
    p.add_argument(
        "--read-mode",
        choices=list(READ_MODES),
        default=DEFAULT_READ_MODE,
        dest="read_mode",
        help=(
            "How to extract session blobs from the disc. "
            "'sequential' (default) holds one session on local disk at a time; "
            "'full-copy' extracts every session up front."
        ),
    )


def _add_restore(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action
    Output: None
    Details:
        restore <dest> - reconstruct source directory from disc/ISO.
        --rescue-key recovers the primary secret from the enc_mode.json blob.
    """
    p = sub.add_parser("restore", help="Reconstruct source from disc or ISO.")
    p.add_argument("dest", help="Destination directory.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--session", type=int, metavar="N")
    p.add_argument("--force", action="store_true")
    p.add_argument("--key", metavar="PATH", help="Keyfile path (keyfile mode only).")
    p.add_argument(
        "--rescue-key",
        nargs="?",
        const="",
        default=None,
        dest="rescue_key",
        metavar="CODE",
        help=(
            "Recover using the rescue key printed at init. "
            "Omit CODE to prompt."
        ),
    )
    p.add_argument(
        "--path",
        action="append",
        dest="paths",
        metavar="REL_PATH",
        help="Restore this relative path or directory prefix. Repeatable.",
    )
    p.add_argument(
        "--skip-volume",
        type=int,
        action="append",
        dest="skip_volumes",
        metavar="VOLUME_INDEX",
        help="Mark this volume index as permanently unavailable. Repeatable. Activates recovery mode.",
    )
    _add_read_mode(p)


def _add_history(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action
    Output: None
    Details:
        history - print session table from disc/ISO manifests.
    """
    p = sub.add_parser("history", help="List all sessions on disc or ISO.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--key", metavar="PATH", help="Keyfile path (keyfile mode only).")



def _add_ls(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action
    Output: None
    Details:
        ls - list archive paths from disc/ISO manifests. No blob reads.
    """
    p = sub.add_parser("ls", help="List archive contents from disc or ISO manifests.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--key", metavar="PATH", help="Keyfile path (keyfile mode only).")
    p.add_argument("--session", type=int, metavar="N")
    p.add_argument(
        "--path",
        default=None,
        metavar="PREFIX",
        help="List only this relative path or directory prefix.",
    )
    p.add_argument(
        "--show-deleted",
        action="store_true",
        dest="show_deleted",
        help="Include tombstoned paths still physically on the disc.",
    )


def _add_browse(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action
    Output: None
    Details:
        browse - interactive numbered listing that feeds restore --path.
    """
    p = sub.add_parser(
        "browse",
        help="Interactively select archive paths and restore the set.",
    )
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--key", metavar="PATH", help="Keyfile path (keyfile mode only).")
    p.add_argument(
        "--show-deleted",
        action="store_true",
        dest="show_deleted",
        help="Include tombstoned paths still physically on the disc.",
    )


def _add_verify(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action
    Output: None
    Details:
        verify - integrity check at specified depth.
    """
    p = sub.add_parser("verify", help="Check integrity of disc or ISO.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--level", choices=["fast", "checksum", "full"], default="fast")
    p.add_argument("--key", metavar="PATH", help="Keyfile path (keyfile mode only).")
    _add_read_mode(p)


def _add_status(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action
    Output: None
    Details:
        status - print disc/ISO state and any SUSPECT entries.
    """
    p = sub.add_parser("status", help="Show disc/ISO state and warnings.")
    p.add_argument("--device", default=None, metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--key", metavar="PATH", help="Keyfile path (keyfile mode only).")
    p.add_argument(
        "--recover-scan", action="store_true", dest="recover_scan",
        help="Bypass last-known-good ledger check; treat readable sessions as authoritative.",
    )


def _add_dry_iso_mutex(_parser: argparse.ArgumentParser) -> None:
    """
    Input:  _parser - unused. Kept for signature compatibility
    Output: None
    Details:
        --dry-run and --test-iso are intentionally allowed to coexist.
        --test-iso selects the ISO backend. --dry-run skips the actual write.
    """


def dispatch(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed namespace from build_parser()
    Output: int - exit code (0 success, 1 error)
    Details:
        Applies config defaults, sets up logging, then routes to the handler.
        Returns the exit code. Does not call sys.exit().
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
        "ls": _run_ls,
        "browse": _run_browse,
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
        _log.warning("Command interrupted by user.")
        print("\nInterrupted.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        _log.error("Unhandled error in %s: %s", args.command, exc, exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _make_backend(args: argparse.Namespace) -> "BurnBackend":
    """
    Input:  args - parsed namespace with test_iso and disc_size attributes
    Output: BurnBackend - ISOBackend if --test-iso set, else DiscBackend
    """
    if getattr(args, "test_iso", None):
        size_str = getattr(args, "disc_size", "25gb")
        disc_size = parse_disc_size(size_str)
        return ISOBackend(Path(args.test_iso), disc_size=disc_size)
    return DiscBackend(getattr(args, "device", "/dev/sr0"))


def _make_mirror_backend(args: argparse.Namespace) -> "BurnBackend | None":
    """
    Input:  args - parsed namespace with optional --mirror and backend flags
    Output: BurnBackend for the mirror drive, or None if --mirror not given
    Details:
        Matches the primary backend type: ISOBackend when the user sets
        --test-iso, DiscBackend otherwise.
    """
    mirror = getattr(args, "mirror", None)
    if not mirror:
        return None
    if getattr(args, "test_iso", None):
        size_str = getattr(args, "disc_size", None) or "25gb"
        disc_size = parse_disc_size(size_str)
        return ISOBackend(Path(mirror), disc_size=disc_size)
    return DiscBackend(mirror)


def _backend_id(backend: "BurnBackend") -> str:
    """
    Input:  backend - BurnBackend instance
    Output: str - canonical identifier (ISO path or device path)
    """
    if isinstance(backend, ISOBackend):
        return str(backend.iso_path)
    if isinstance(backend, DiscBackend):
        return backend.device
    return "unknown"


def _read_disc_manifests(
    backend: "BurnBackend",
    crypto: "CryptoBackend | None" = None,
) -> list[Manifest]:
    """
    Input:  backend - BurnBackend to read from
            crypto  - CryptoBackend for encrypted manifests (None means plaintext only)
    Output: list of Manifest objects in ascending session order (may be empty)
    Details:
        Delegates to manifest.load_session_manifests(). Kept as a named
        helper so call sites stay one statement assigning list[Manifest].
        Marker-aware: for a disc whose VolumeMarker.first_session is not 0
        (a later volume of a multi-volume archive), probing starts
        there, not at session 0. Otherwise its own coverage check
        would see session 0 "missing" on a medium that never had one.
    """
    from oddarchiver.volume import read_volume_marker
    marker = read_volume_marker(backend)
    return load_session_manifests(
        backend, crypto, start_session=marker.first_session if marker else 0,
    )


def _check_loaded_manifests(
    manifests: list[Manifest],
    *,
    backend: "BurnBackend",
    args: argparse.Namespace,
    disc_session_count: int,
    command: str,
) -> None:
    """
    Input:  manifests          - already-loaded session list
            backend            - BurnBackend source of the list
            args               - parsed CLI namespace (recover_scan, label)
            disc_session_count - mediainfo session_count
            command            - caller name for log context
    Output: None
    Details:
        Runs the 22a/22c ledger coverage check against the loaded list.
        Does not rescan manifests. Raises RuntimeError on a shortfall
        unless the user passes --recover-scan.
    """
    from oddarchiver.ledger import check_session_coverage

    label = ""
    if manifests:
        label = manifests[0].label
    if not label:
        label = getattr(args, "label", "") or ""
    try:
        check_session_coverage(
            manifests,
            label=label,
            backend_id=_backend_id(backend),
            recover_scan=getattr(args, "recover_scan", False),
            disc_session_count=disc_session_count,
            backend=backend,
        )
    except RuntimeError:
        _log.exception("%s: manifest coverage short of last-known-good", command)
        raise

def _crypto_for_disc(
    backend: "BurnBackend",
    key_path: str | None = None,
    rescue_key: str | None = None,
    raw: dict[str, bytes] | None = None,
    first_session: int | None = None,
) -> "CryptoBackend":
    """
    Input:  backend       - BurnBackend with at least one session
            key_path      - path to keyfile (required when disc is keyfile-encrypted)
            rescue_key    - printable rescue code, or "" to prompt via getpass
            raw           - optional already-fetched control-file bytes
            first_session - optional VolumeMarker.first_session. Avoids a
                            second marker read when the caller already has it
    Output: CryptoBackend matching the encryption mode stored on disc
    Details:
        Reads session_{first:03d}/enc_mode.json (and falls back to the
        plaintext manifest.json of that session) from the shared control-file
        batch produced by session_control_paths(), not a separate two-path
        read_paths() of its own. first is the VolumeMarker.first_session
        of this disc (0 for volume 0 and every legacy/single-volume disc
        with no marker). Hardcoding session_000
        here silently misdetected every volume after the first as
        unencrypted, since that path never exists on the physical disc
        of a later volume. Falls back to the plaintext manifest.json
        encryption field on discs that predate encrypted-manifest
        support. When rescue_key is not None, unwraps the
        rescue blob of enc_mode.json via scrypt and builds the backend
        from the recovered secret.
    """
    from oddarchiver.volume import read_volume_marker
    if first_session is None:
        marker = read_volume_marker(backend)
        first = marker.first_session if marker else 0
    else:
        first = first_session
    enc_mode_path = f"session_{first:03d}/enc_mode.json"
    manifest_path = f"session_{first:03d}/manifest.json"
    mode = "none"
    enc_mode_data: dict = {}
    files = raw if raw is not None else backend.read_paths(session_control_paths())
    enc_mode_bytes = files.get(enc_mode_path)
    if enc_mode_bytes is not None:
        try:
            parsed = json.loads(enc_mode_bytes)
            if isinstance(parsed, dict):
                enc_mode_data = parsed
                mode = parsed.get("mode", "none") or "none"
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
            _log.warning("Cannot parse %s: %s", enc_mode_path, exc)
            mode = "none"
    else:
        manifest_bytes = files.get(manifest_path)
        if manifest_bytes is not None:
            try:
                raw = json.loads(manifest_bytes)
                if isinstance(raw, dict):
                    mode = raw.get("encryption", {}).get("mode", "none") or "none"
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                _log.warning(
                    "Cannot parse %s for encryption mode: %s",
                    manifest_path, exc,
                )
        else:
            _log.warning(
                "No %s or %s on disc; assuming encryption mode none",
                enc_mode_path, manifest_path,
            )

    if rescue_key is not None:
        if rescue_key == "":
            rescue_key = getpass.getpass("Rescue key: ")
        rescue_block = enc_mode_data.get("rescue")
        if not isinstance(rescue_block, dict) or "wrapped" not in rescue_block:
            raise ValueError(
                "this archive has no rescue key; it predates this feature"
            )
        from oddarchiver.crypto import (
            KeyfileCrypto,
            PassphraseCrypto,
            unwrap_rescue_secret,
        )
        secret = unwrap_rescue_secret(rescue_key, rescue_block)
        if mode == "passphrase":
            return PassphraseCrypto(passphrase=secret)
        if mode == "keyfile":
            return KeyfileCrypto.from_kek(secret)
        raise ValueError(
            "this archive has no rescue key; it predates this feature"
        )

    if mode == "none":
        return make_crypto("none")
    if mode == "passphrase":
        passphrase = os.environ.get("ODDARCHIVER_PASSPHRASE", "")
        if not passphrase:
            passphrase = getpass.getpass("Passphrase: ")
        return make_crypto("passphrase", passphrase=passphrase.encode())
    if mode == "keyfile":
        if not key_path:
            print("error: --key is required for keyfile-encrypted disc.", file=sys.stderr)
            raise SystemExit(1)
        return make_crypto("keyfile", keyfile_path=key_path)
    raise ValueError(f"Unknown encryption mode on disc: {mode!r}")


def _crypto_and_manifests(
    backend: "BurnBackend",
    key_path: str | None = None,
    rescue_key: str | None = None,
) -> tuple["CryptoBackend", list[Manifest], dict[str, bytes]]:
    """
    Input:  backend, key_path, rescue_key - same as _crypto_for_disc
    Output: (crypto, manifests, raw_control_files)
    Details:
        One backend.read_paths(session_control_paths()) serves both
        encryption-mode detection and the session manifest scan. Callers
        that previously did _crypto_for_disc then _read_disc_manifests
        should use this instead so enc_mode.json is not fetched twice.
    """
    from oddarchiver.volume import read_volume_marker
    marker = read_volume_marker(backend)
    first = marker.first_session if marker else 0
    raw = backend.read_paths(session_control_paths())
    crypto = _crypto_for_disc(
        backend, key_path=key_path, rescue_key=rescue_key,
        raw=raw, first_session=first,
    )
    manifests = load_session_manifests(
        backend, crypto, start_session=first, raw=raw,
    )
    return crypto, manifests, raw


def _make_init_crypto(args: argparse.Namespace) -> "CryptoBackend":
    """
    Input:  args - parsed init namespace with --encrypt and optional --key
    Output: CryptoBackend for the requested mode
    Details:
        For passphrase: reads ODDARCHIVER_PASSPHRASE env var or prompts twice
        (confirmation loop) to catch typos before any write.
        For keyfile: reads the file at args.key.
        For passphrase and keyfile, generates a rescue code, wraps the
        persistent secret with scrypt, and hangs _rescue_block/_rescue_code
        on the returned backend for write_manifest and the init printout.
    """
    from oddarchiver.crypto import (
        KeyfileCrypto,
        PassphraseCrypto,
        generate_rescue_code,
        wrap_rescue_secret,
    )

    mode = getattr(args, "encrypt", "none")
    if mode == "none":
        return make_crypto("none")
    if mode == "passphrase":
        passphrase = os.environ.get("ODDARCHIVER_PASSPHRASE", "")
        if not passphrase:
            while True:
                passphrase = getpass.getpass("Passphrase: ")
                confirm = getpass.getpass("Confirm passphrase: ")
                if passphrase == confirm:
                    break
                print("Passphrases do not match. Try again.", file=sys.stderr)
        crypto: "CryptoBackend" = make_crypto(
            "passphrase", passphrase=passphrase.encode()
        )
    elif mode == "keyfile":
        key_path = getattr(args, "key", None)
        if not key_path:
            print("error: --key is required for keyfile mode.", file=sys.stderr)
            raise SystemExit(1)
        crypto = make_crypto("keyfile", keyfile_path=key_path)
    else:
        raise ValueError(f"Unknown mode: {mode!r}")

    code, raw = generate_rescue_code()
    if isinstance(crypto, PassphraseCrypto):
        secret = crypto._passphrase
    elif isinstance(crypto, KeyfileCrypto):
        secret = crypto._kek
    else:
        return crypto
    crypto._rescue_block = wrap_rescue_secret(raw, secret)
    crypto._rescue_code = code
    return crypto


def _encryption_block(crypto: "CryptoBackend") -> dict:
    """
    Input:  crypto - CryptoBackend instance
    Output: dict - encryption metadata to embed in manifest
    Details:
        Records mode for subsequent commands to reconstruct crypto.
        Copies crypto._rescue_block into "rescue" when present so
        write_manifest can put it in session_000/enc_mode.json.
    """
    from oddarchiver.crypto import NullCrypto, PassphraseCrypto, KeyfileCrypto
    if isinstance(crypto, NullCrypto):
        block: dict = {"mode": "none"}
    elif isinstance(crypto, PassphraseCrypto):
        block = {"mode": "passphrase", "cipher": "chacha20-poly1305", "kdf": "argon2id"}
    elif isinstance(crypto, KeyfileCrypto):
        block = {"mode": "keyfile", "cipher": "chacha20-poly1305"}
    else:
        block = {"mode": "none"}
    rescue = getattr(crypto, "_rescue_block", None)
    if rescue:
        block["rescue"] = rescue
    return block


def _patch_manifest(
    staging: Path,
    session_n: int,
    label: str,
    encryption: dict,
    drives: list[str] | None = None,
    crypto: "CryptoBackend | None" = None,
) -> Manifest:
    """
    Input:  staging    - staging root directory
            session_n  - session index
            label      - disc label to apply
            encryption - encryption block dict
            drives     - list of drive/ISO identifiers that will hold this session
            crypto     - if set (and non-null), rewrite manifest encrypted
    Output: updated Manifest (also written to staging atomically)
    Details:
        Reads the provisional plaintext manifest written by build_staging.
        Sets label, encryption, and drives, then rewrites the file atomically
        in the staging session directory.
        If crypto is non-null, the output is manifest.enc plus enc_mode.json
        and write_manifest removes the provisional manifest.json.
    """
    session_dir = staging / f"session_{session_n:03d}"
    manifest = read_manifest(session_dir / "manifest.json", crypto=None)
    manifest.label = label
    manifest.encryption = encryption
    if drives is not None:
        manifest.drives = drives
    write_manifest(session_dir, manifest, crypto=crypto)
    return manifest


def _update_cache(
    cache: CacheManager,
    staging: Path,
    session_n: int,
    entries: list,
) -> None:
    """
    Input:  cache     - CacheManager to update
            staging   - staging directory root
            session_n - session this call just burned
            entries   - ManifestEntry list from the session manifest
    Output: None
    Details:
        Copies each encrypted blob from staging into the cache after a
        successful burn. The cache updates only after burn succeeds.
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
    Input:  n - byte count
    Output: human-readable string (e.g. "1.2 GiB")
    """
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TiB"


def _path_matches(rel_path: str, prefix: str) -> bool:
    """
    Input:  rel_path - archive-relative path
            prefix   - exact path or directory prefix
    Output: True if rel_path equals prefix or is under prefix/
    """
    return rel_path == prefix or rel_path.startswith(prefix.rstrip("/") + "/")


def _deleted_marker(entry: TreeEntry) -> str:
    """
    Input:  entry - tree row
    Output: trailing deleted marker, or empty string
    """
    if not entry.deleted:
        return ""
    sess = entry.deleted_session
    if sess is None:
        sess = entry.origin_session
    return f" [DELETED at session {sess:03d}]"


def _direct_children(
    tree: list[TreeEntry],
    current: str,
    show_deleted: bool,
) -> list[tuple[str, bool, TreeEntry | None]]:
    """
    Input:  tree         - full disc tree
            current      - current directory prefix (empty at root)
            show_deleted - include tombstoned entries
    Output: (name, is_dir, file_entry_or_None) sorted by name
    Details:
        Direct children of current only. Deeper paths collapse to a
        directory name.
    """
    files: dict[str, TreeEntry] = {}
    dirs: set[str] = set()
    prefix = current.rstrip("/")
    for entry in tree:
        if entry.deleted and not show_deleted:
            continue
        rel = entry.path
        if prefix:
            if rel == prefix:
                continue
            if not rel.startswith(prefix + "/"):
                continue
            rest = rel[len(prefix) + 1 :]
        else:
            rest = rel
        if not rest:
            continue
        name, sep, more = rest.partition("/")
        if sep:
            dirs.add(name)
        else:
            files[name] = entry
    names = sorted(set(dirs) | set(files))
    rows: list[tuple[str, bool, TreeEntry | None]] = []
    for name in names:
        if name in dirs:
            rows.append((name, True, None))
        else:
            rows.append((name, False, files[name]))
    return rows


def _run_dry_run(args: argparse.Namespace, is_init: bool = False) -> int:
    """
    Input:  args    - parsed namespace with source and backend flags
            is_init - True when called from _run_init (no prior disc state)
    Output: exit code 0, or 1 if a non-init run finds an uninitialized disc
    Details:
        Scans the source, diffs it, and estimates delta sizes for changed files.
        Prints an rsync-style report. The report shows a space overage
        but does not cause exit 1. Does not burn and does not write a new session.
        Fallback reads of prior blobs may fill the local cache.

        Hashing progress shows inline. The code prints the file list after.
    """
    import hashlib
    from oddarchiver.delta import process_files
    from oddarchiver.session import SPACE_SAFETY_MARGIN, _print_bar

    print("DRY RUN -- no disc will be written")
    print()

    backend = _make_backend(args)
    disc_info = backend.mediainfo()

    if is_init:
        if disc_info.session_count > 0:
            print("warning: disc already initialized; skipping init.", file=sys.stderr)
            return 0
        disc_state: dict[str, str] = {}
        disc_origin: dict[str, int] = {}
        label = getattr(args, "label", "ARCHIVE")
        session_n = 0
        crypto = _make_init_crypto(args)
    else:
        if disc_info.session_count == 0:
            print("error: disc not initialized; run 'init' first.", file=sys.stderr)
            return 1
        crypto, manifests, _raw = _crypto_and_manifests(
            backend, key_path=getattr(args, "key", None),
        )
        disc_state = build_disc_state(manifests)
        disc_origin = build_disc_origin(manifests)
        label = manifests[0].label if manifests else "ARCHIVE"
        from oddarchiver.volume import read_volume_marker
        marker = read_volume_marker(backend)
        # Match _run_sync: marker.first_session plus manifest count, not
        # dvd+rw-mediainfo session count. BD-R SRM+POW reports
        # session_count=1 no matter how many appends happened, because
        # growisofs adds tracks inside one MMC session. Reading the disc
        # count here made dry-run announce a session number that was
        # already used. A missing marker treats first_session as 0.
        session_n = (marker.first_session if marker else 0) + len(manifests)

    source = Path(args.source)

    # Collect paths first so we know the total for the bar.
    print(f"Scanning {source} ...", flush=True)
    source_files = sorted(f for f in source.rglob("*") if f.is_file())
    total_files = len(source_files)
    print(f"  {total_files} file(s) found. Hashing...", flush=True)

    current_state: dict[str, str] = {}
    total_source_bytes = 0
    for i, f in enumerate(source_files, 1):
        rel = str(f.relative_to(source))
        data = f.read_bytes()
        current_state[rel] = hashlib.sha256(data).hexdigest()
        total_source_bytes += len(data)
        _print_bar(i, total_files, suffix=rel)
    if total_files:
        print()

    changed = {p for p in current_state if p in disc_state and current_state[p] != disc_state[p]}
    new_files = {p for p in current_state if p not in disc_state}
    unchanged_count = len(current_state) - len(changed) - len(new_files)

    print(f"  Disc state: {label}, next session {session_n}")
    print(f"  {len(new_files)} new, {len(changed)} changed, {unchanged_count} unchanged")
    print()

    if not changed and not new_files:
        print("No changes detected.")
        return 0

    change_count = len(changed) + len(new_files)
    print(
        f"Changes ({change_count} files, {_fmt_bytes(total_source_bytes)} total source):"
        f"  (note: real run adds encryption + staging overhead)"
    )

    cache = CacheManager()
    total_session_bytes = 0
    entry_count = 0

    dry_manifests = manifests if not is_init else None
    needed = [
        (rel_path, disc_origin.get(rel_path, session_n - 1))
        for rel_path in sorted(changed)
    ]
    if needed:
        cache.prefetch_origin_blobs(
            needed, backend, manifests=dry_manifests, crypto=crypto,
        )

    delta_jobs: list[tuple[bytes, Path]] = []
    delta_paths: list[str] = []
    failed_paths: set[str] = set()
    for rel_path in sorted(changed):
        abs_path = source / rel_path
        try:
            encrypted_old = cache.get_with_fallback(
                rel_path, disc_origin.get(rel_path, session_n - 1),
                backend, crypto=crypto, manifests=dry_manifests,
            )
            old_bytes = crypto.decrypt(encrypted_old)
            delta_jobs.append((old_bytes, abs_path))
            delta_paths.append(rel_path)
        except Exception:
            _log.exception(
                "dry-run: could not read prior blob for %s; treating as full",
                rel_path,
            )
            failed_paths.add(rel_path)

    delta_results: dict[str, tuple[str, bytes]] = {}
    if delta_jobs:
        for rel_path, result in zip(delta_paths, process_files(delta_jobs)):
            delta_results[rel_path] = result

    for rel_path in sorted(changed):
        abs_path = source / rel_path
        full_size = abs_path.stat().st_size
        if rel_path in delta_results:
            kind, blob = delta_results[rel_path]
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
        else:
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
    print(f"Session size (unencrypted): {_fmt_bytes(total_session_bytes)}")
    print(f"Disc remaining:             {_fmt_bytes(disc_info.remaining_bytes)}")

    limit = disc_info.remaining_bytes * SPACE_SAFETY_MARGIN
    if disc_info.remaining_bytes > 0 and total_session_bytes < limit:
        pct = 100.0 * total_session_bytes / disc_info.remaining_bytes
        print(f"Space check:                OK (session is {pct:.2f}% of remaining)")
    elif disc_info.remaining_bytes > 0:
        pct = 100.0 * total_session_bytes / disc_info.remaining_bytes
        print(
            f"Space check:                OVERAGE "
            f"({_fmt_bytes(total_session_bytes)} is {pct:.1f}% of remaining -- would not fit)"
        )
    else:
        print("Space check:                OVERAGE (disc full)")

    print()
    print(f"Would burn as: session_{session_n:03d} on {label}")
    print("No disc written (dry run).")

    return 0


def _volume_iso_path(original: Path, volume_index: int) -> Path:
    """
    Input:  original     - original --test-iso path (volume 0)
            volume_index - index of the new volume (k >= 1)
    Output: Path with .vol{k} inserted before the .iso suffix
    Details:
        archive.iso volume 1 -> archive.vol1.iso
        archive.iso volume 2 -> archive.vol2.iso
        Always derived from the original path, never from a previous .volN
        path. A name without an .iso suffix gets .vol{k} appended.
    """
    name = original.name
    if name.lower().endswith(".iso"):
        stem = name[:-4]
        suffix = name[-4:]
        return original.with_name(f"{stem}.vol{volume_index}{suffix}")
    return original.with_name(f"{name}.vol{volume_index}")


def _swap_after_full_volume(
    backend: "BurnBackend",
    label: str,
    next_volume_index: int,
    iso_origin: Path | None,
    iso_disc_size: int | None,
) -> "BurnBackend | None":
    """
    Input:  backend           - backend that just finished a volume
            label             - archive label used in the prompt
            next_volume_index - volume index of the medium to insert (k+1)
            iso_origin        - original --test-iso path, or None for a drive
            iso_disc_size     - disc_size of the original ISOBackend, or None
    Output: backend pointing at the next blank medium, or None on abort
    Details:
        ISOBackend skips the interactive prompt and opens original.vol{k}.iso
        with the same disc_size. DiscBackend prompts on stdin. 'q' (or EOF)
        returns None so already-committed chunks stay durable. After this
        function selects the medium, mediainfo() must report session_count == 0 or
        this raises RuntimeError.
    """
    info = backend.mediainfo()
    used = _fmt_bytes(info.used_bytes)
    total = _fmt_bytes(info.used_bytes + info.remaining_bytes)
    if iso_origin is not None:
        next_path = _volume_iso_path(iso_origin, next_volume_index)
        _log.info(
            "Volume %d is full (%s/%s); auto-continuing on %s for volume %d",
            next_volume_index - 1, used, total, next_path, next_volume_index,
        )
        print(
            f"Disc {label} is full ({used}/{total}). "
            f"Continuing on {next_path} for volume {next_volume_index}.",
            flush=True,
        )
        next_backend: "BurnBackend" = ISOBackend(next_path, disc_size=iso_disc_size)
    else:
        prompt = (
            f"Disc {label} is full ({used}/{total}). "
            f"Insert a new disc for volume {next_volume_index} and press Enter "
            f"(or 'q' to abort; already-burned data is durable and this sync is resumable)."
        )
        try:
            reply = input(prompt)
        except EOFError:
            _log.warning("disc-swap: EOF on input; aborting remaining volumes")
            return None
        if reply.strip().lower() == "q":
            _log.info(
                "disc-swap: user aborted after volume %d",
                next_volume_index - 1,
            )
            return None
        next_backend = backend
    new_info = next_backend.mediainfo()
    if new_info.session_count != 0:
        raise RuntimeError(
            f"Newly inserted disc is not blank "
            f"(session_count={new_info.session_count}); "
            "refusing to overwrite an existing volume"
        )
    return next_backend


def _burn_verify_commit(
    command: str,
    staging: Path,
    session_n: int,
    backend: "BurnBackend",
    label: str,
    enc_block: dict,
    drives: list[str],
    crypto: "CryptoBackend",
    cache: CacheManager,
    verify_mod: object,
    use_init: bool,
    expected_session_count: int,
    mirror_backend: "BurnBackend | None",
    marker_first_session: int,
    expected_archive_id: str | None = None,
    expected_volume_index: int | None = None,
    slow: bool = False,
) -> int:
    """
    Input:  command                - "init" or "sync" for log prefixes
            staging                - staging root for this chunk
            session_n              - global session number of this chunk
            backend                - backend to burn this chunk to
            label                  - archive label written into the manifest
            enc_block              - encryption block for the manifest
            drives                 - drive ids recorded on the manifest
            crypto                 - CryptoBackend used for the burn
            cache                  - CacheManager updated after verify
            verify_mod             - oddarchiver.verify module
            use_init               - True to call backend.init, else append
            expected_session_count - archive session count at run start
            mirror_backend         - optional mirror, first chunk only
            marker_first_session   - first_session of the marker of this volume
            expected_archive_id    - archive_id to continue, or None to skip
            expected_volume_index  - volume_index to continue, or None
            slow                   - True to post-burn verify at checksum level
    Output: 0 on success, 1 on burn/verify/mirror failure
    Details:
        When the caller sets expected_archive_id, re-reads volume.json immediately
        before init/append. That is the 31d check: _guard() already does
        mediainfo(), and this marker read runs only on the continue path.
        Blank-disc init skips it. init/append call signatures stay
        unchanged so existing test wrappers keep working. Then patches
        the staging manifest, burns, optionally mirrors, and runs
        post-burn verify. Default level is fast (manifest only).
        --slow sets checksum, which reads every blob from the medium.
        Then records the per-volume ledger count and updates the cache.

        On BurnInterrupted, runs the same verify level then returns 1.
        Unless verify passes, this function does not update the cache
        and ledger.
    """
    from oddarchiver.ledger import record_confirmed

    level = "checksum" if slow else "fast"
    if slow:
        interrupt_msg = "Verifying disc (checksum) after interrupted burn..."
        start_msg = "Verifying burn (checksum)..."
        ok_msg = "  Verify OK (checksum)."
    else:
        interrupt_msg = "Verifying disc (fast, manifest only) after interrupted burn..."
        start_msg = "Verifying burn (fast, manifest only)..."
        ok_msg = "  Verify OK (manifest only)."

    manifest = _patch_manifest(
        staging, session_n, label, enc_block, drives=drives, crypto=crypto,
    )
    print(f"Burning session {session_n:03d} to {_backend_id(backend)} ...", flush=True)
    _log.info("%s: burning session %03d to %s", command, session_n, _backend_id(backend))
    if expected_archive_id is not None:
        check_volume_identity(
            backend, expected_archive_id, expected_volume_index,
        )
    try:
        if use_init:
            backend.init(staging, label, expected_session_count=expected_session_count)
        else:
            backend.append(staging, label, expected_session_count=expected_session_count)
    except BurnInterrupted as exc:
        _log.exception("%s: burn interrupted for session %03d", command, session_n)
        print(
            f"error: burn interrupted for session {session_n:03d}: {exc}",
            file=sys.stderr,
        )
        try:
            print(interrupt_msg, flush=True)
            verify_mod.verify(backend, crypto, level=level)
        except SystemExit:
            _log.exception("%s: post-interrupt %s verify reported failures", command, level)
        except Exception:
            _log.exception("%s: post-interrupt %s verify raised", command, level)
        return 1
    _log.info("%s: burn complete", command)
    print("  Burn complete.", flush=True)
    if mirror_backend is not None:
        try:
            print(f"Burning mirror to {_backend_id(mirror_backend)} ...", flush=True)
            _log.info("%s: burning mirror to %s", command, _backend_id(mirror_backend))
            if use_init:
                mirror_backend.init(staging, label, expected_session_count=0)
            else:
                # Same start-of-run archive count as the primary. Mirror
                # mediainfo().session_count is stuck at 1 on BD-R SRM+POW.
                mirror_backend.append(
                    staging, label,
                    expected_session_count=expected_session_count,
                )
            _log.info("%s: mirror burn complete", command)
            print("  Mirror burn complete.", flush=True)
        except Exception as exc:
            _log.exception("Mirror burn failed for session %d", session_n)
            print(f"error: mirror burn failed: {exc}", file=sys.stderr)
            return 1
    try:
        print(start_msg, flush=True)
        verify_mod.verify(backend, crypto, level=level, manifests=[manifest])
        _log.info("%s: post-burn verify OK", command)
        print(ok_msg, flush=True)
    except SystemExit:
        _log.error("%s: post-burn verify FAILED; cache not updated", command)
        print("error: post-burn verify failed; cache not updated.", file=sys.stderr)
        return 1
    local_count = (session_n - marker_first_session) + 1
    record_confirmed(label, _backend_id(backend), local_count)
    print("Updating cache...", flush=True)
    _update_cache(cache, staging, session_n, manifest.entries)
    _log.info(
        "%s: session %03d complete - %d file(s) archived",
        command, session_n, len(manifest.entries),
    )
    return 0


def _burn_volume_chunks(
    chunks: list[tuple[Path, int, bool]],
    backend: "BurnBackend",
    *,
    command: str,
    label: str,
    enc_block: dict,
    crypto: "CryptoBackend",
    cache: CacheManager,
    verify_mod: object,
    mirror_backend: "BurnBackend | None",
    archive_id: str,
    base_volume_index: int,
    chunk0_first_session: int,
    write_chunk0_marker: bool,
    chunk0_use_init: bool,
    chunk0_expected_session_count: int,
    expected_archive_id: str | None = None,
    expected_volume_index: int | None = None,
    slow: bool = False,
) -> tuple[int, "BurnBackend"]:
    """
    Input:  chunks                       - (staging, session_n, is_new_volume)
            backend                      - backend for chunk 0 (current medium)
            command                      - "init" or "sync"
            label                        - archive label
            enc_block                    - encryption block
            crypto                       - CryptoBackend
            cache                        - CacheManager
            verify_mod                   - oddarchiver.verify module
            mirror_backend               - optional mirror. Forbidden with 2+ chunks
            archive_id                   - id written onto new VolumeMarkers
            base_volume_index            - volume_index of the current disc
            chunk0_first_session         - first_session of the current volume
            write_chunk0_marker          - True for a fresh init
            chunk0_use_init              - True to init (not append) chunk 0
            chunk0_expected_session_count - archive session count for chunk 0
            expected_archive_id          - identity to require on append, or None
            expected_volume_index        - volume_index to require, or None
            slow                         - True to post-burn verify at checksum
    Output: (exit code, current backend) - 0 for success and the backend
            the last chunk actually burned to (unchanged unless a
            disc-capacity swap happened). A caller running more
            staging-space groups after this call must keep using the
            returned backend, not its own original reference, or it
            queries a medium this call already swapped away from
    Details:
        Burns every chunk, swapping media between them. ISOBackend auto-opens
        the next .vol{k}.iso. DiscBackend prompts. 'q' stops the loop with
        already-committed chunks left intact. This function removes every
        staging dir in finally. Multi-volume plus --mirror raises RuntimeError.

        Append of an existing volume passes expected identity into
        _burn_verify_commit. Blank-disc init (use_init) does not.
    """
    from oddarchiver.volume import (
        VolumeMarker, read_volume_baseline, write_volume_marker,
        write_volume_baseline,
    )

    iso_origin: Path | None = None
    iso_disc_size: int | None = None
    if isinstance(backend, ISOBackend):
        iso_origin = backend.iso_path
        iso_disc_size = backend.disc_size

    previous_volume_label = ""
    new_volumes_swapped = 0
    pending_baseline: tuple[dict[str, str], dict[str, int]] | None = None
    try:
        if len(chunks) > 1 and mirror_backend is not None:
            raise RuntimeError(
                "multi-volume disc-swap combined with --mirror is not yet supported"
            )
        for i, (staging, session_n, is_new_volume) in enumerate(chunks):
            if i == 0 and write_chunk0_marker:
                write_volume_marker(
                    staging,
                    VolumeMarker(
                        archive_id=archive_id,
                        volume_index=base_volume_index,
                        first_session=chunk0_first_session,
                        created_at=datetime.now(timezone.utc).isoformat(),
                        previous_volume_label="",
                    ),
                )
            elif is_new_volume:
                vol_index = base_volume_index + new_volumes_swapped
                write_volume_marker(
                    staging,
                    VolumeMarker(
                        archive_id=archive_id,
                        volume_index=vol_index,
                        first_session=session_n,
                        created_at=datetime.now(timezone.utc).isoformat(),
                        previous_volume_label=previous_volume_label,
                    ),
                )
                if pending_baseline is not None:
                    write_volume_baseline(
                        staging, pending_baseline[0], pending_baseline[1],
                    )

            if is_new_volume:
                marker_first_session = session_n
                use_init = True
                expected = 0
                chunk_mirror = None
                burn_archive_id = None
                burn_volume_index = None
            else:
                marker_first_session = chunk0_first_session
                use_init = chunk0_use_init
                expected = chunk0_expected_session_count
                chunk_mirror = mirror_backend
                if use_init:
                    burn_archive_id = None
                    burn_volume_index = None
                else:
                    burn_archive_id = expected_archive_id
                    burn_volume_index = expected_volume_index

            drives = [_backend_id(backend)]
            if chunk_mirror is not None:
                drives.append(_backend_id(chunk_mirror))

            rc = _burn_verify_commit(
                command,
                staging,
                session_n,
                backend,
                label,
                enc_block,
                drives,
                crypto,
                cache,
                verify_mod,
                use_init,
                expected,
                chunk_mirror,
                marker_first_session,
                expected_archive_id=burn_archive_id,
                expected_volume_index=burn_volume_index,
                slow=slow,
            )
            if rc != 0:
                return rc, backend

            if i < len(chunks) - 1:
                info = backend.mediainfo()
                previous_volume_label = info.label or label
                old_seed = read_volume_baseline(backend) or ({}, {})
                old_manifests = _read_disc_manifests(backend, crypto=crypto)
                pending_baseline = (
                    build_disc_state(old_manifests, seed=old_seed[0]),
                    build_disc_origin(old_manifests, seed=old_seed[1]),
                )
                next_index = base_volume_index + new_volumes_swapped + 1
                swapped = _swap_after_full_volume(
                    backend,
                    label,
                    next_index,
                    iso_origin,
                    iso_disc_size,
                )
                if swapped is None:
                    return 1, backend
                backend = swapped
                new_volumes_swapped += 1
        return 0, backend
    finally:
        for staging, _session_n, _is_new in chunks:
            shutil.rmtree(staging, ignore_errors=True)


def _mask_diff_to_group(
    current_state: dict[str, str],
    disc_state: dict[str, str],
    group_paths: set[str],
    group_index: int,
) -> dict[str, str]:
    """
    Input:  current_state - {path: sha256} for every file in source right now
            disc_state    - real, unmasked disc state from before this
                            init/sync run started
            group_paths   - new/changed paths assigned to this pass
            group_index   - 0 for the first automatic pass, 1+ for later ones
    Output: a disc_state view scoped to the changeset of this pass
    Details:
        build_staging diffs current_state against whichever disc_state it
        receives. Passing the real disc_state to every pass would make each
        later pass re-see the files of every earlier pass as new or changed
        again. The mask fakes a path outside group_paths as already matching
        current_state, hiding it from this pass. A path inside group_paths
        keeps its true prior value, or stays absent if the file is
        genuinely new. A deleted path (present in disc_state, absent from
        current_state) keeps its true value only on group 0. Every later
        pass omits it entirely, so the deletion lands in exactly one
        session manifest. No later pass replays it or drops it.
    """
    masked: dict[str, str] = {}
    for path in current_state:
        if path in group_paths:
            if path in disc_state:
                masked[path] = disc_state[path]
            # else: genuinely new, leave absent so build_staging stages it
        else:
            masked[path] = current_state[path]
    if group_index == 0:
        for path, checksum in disc_state.items():
            if path not in current_state:
                masked[path] = checksum
    return masked


def _run_init(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed init namespace
    Output: exit code
    Details:
        Scans the source up front, then partitions the new-file set against
        the local staging budget (session.partition_by_staging_budget). One
        group is the common case and behaves exactly as a single build_staging
        call always has. Two or more groups burn as automatic sequential
        sessions on one volume, session 0 through the parameters of init and
        every later group as an ordinary sync-style append, with no user
        interaction. A changeset that also spans discs within one
        group still burns each chunk and then swaps media (ISOBackend auto-opens
        .vol{k}.iso). That disc-capacity split is orthogonal and unchanged.
        Every fresh init writes a VolumeMarker on chunk 0 of group 0 even
        when nothing splits. Exits 0 with a warning if already initialized.

        After a successful encrypted init, prints the rescue code once
        to stdout. The code is never logged or written to disc.
    """
    if getattr(args, "dry_run", False):
        return _run_dry_run(args, is_init=True)

    from oddarchiver import session as session_mod
    from oddarchiver import verify as verify_mod

    backend = _make_backend(args)
    _log.info("init: source=%s label=%s device=%s",
              args.source, getattr(args, "label", "ARCHIVE"), _backend_id(backend))
    disc_info = backend.mediainfo()

    if disc_info.session_count > 0:
        _log.warning("init: disc already initialized (%s); skipping.", _backend_id(backend))
        print("warning: disc already initialized; skipping init.", file=sys.stderr)
        return 0

    from oddarchiver.ledger import load_ledger
    init_label = getattr(args, "label", "ARCHIVE")
    ledger = load_ledger(init_label, _backend_id(backend))
    if ledger is not None and ledger.confirmed_sessions > 0:
        if not getattr(args, "recover_scan", False):
            msg = (
                f"init: ledger records {ledger.confirmed_sessions} confirmed "
                f"session(s) for {init_label!r} but the disc looks empty. "
                "This is a read failure, not a new archive. Retry after "
                "eject/reinsert, or pass --recover-scan if you have confirmed "
                "the disc is genuinely blank."
            )
            _log.error("%s", msg)
            raise RuntimeError(msg)
        _log.warning(
            "recover-scan: proceeding with init despite ledger "
            "confirmed_sessions=%d for %r",
            ledger.confirmed_sessions, init_label,
        )

    mirror_backend = _make_mirror_backend(args)

    if getattr(args, "prefill", None):
        if isinstance(backend, ISOBackend):
            backend.prefill(parse_disc_size(args.prefill))

    crypto = _make_init_crypto(args)
    enc_block = _encryption_block(crypto)
    cache = CacheManager()
    source = Path(args.source)

    # Scan source with progress bar, mirroring the scan block of _run_sync:
    # this pass needs a full path set and sizes before any staging directory
    # exists, so it cannot delegate the scan to build_staging as before.
    import hashlib
    from oddarchiver.session import _print_bar

    print(f"Scanning {source} ...", flush=True)
    source_files = sorted(f for f in source.rglob("*") if f.is_file())
    total_files = len(source_files)
    print(f"  {total_files} file(s) found. Hashing...", flush=True)
    current_state: dict[str, str] = {}
    for i, sf in enumerate(source_files, 1):
        rel = str(sf.relative_to(source))
        current_state[rel] = hashlib.sha256(sf.read_bytes()).hexdigest()
        _print_bar(i, total_files, suffix=rel)
    if total_files:
        print()

    new_files = set(current_state)
    print(f"  {len(new_files)} new, 0 changed, 0 deleted, 0 unchanged", flush=True)

    disc_state: dict[str, str] = {}
    disc_origin: dict[str, int] = {}

    groups = session_mod.partition_by_staging_budget(new_files, source)
    if len(groups) > 1:
        total_needed = sum((source / p).stat().st_size for p in new_files)
        staging_free = shutil.disk_usage(session_mod._default_staging_root()).free
        print(
            f"Source needs ~{_fmt_bytes(total_needed)}; staging has "
            f"~{_fmt_bytes(staging_free)} free. Splitting into {len(groups)} "
            "automatic passes.",
            flush=True,
        )

    archive_id = uuid.uuid4().hex
    base_volume_index = 0
    chunk0_first_session = 0
    session_n = 0
    rc = 0
    for group_index, group_paths in enumerate(groups):
        masked_disc_state = _mask_diff_to_group(
            current_state, disc_state, set(group_paths), group_index,
        )
        chunks = session_mod.build_staging(
            session_n=session_n,
            source=source,
            disc_state=masked_disc_state,
            disc_origin=disc_origin,
            backend=backend,
            cache=cache,
            crypto=crypto,
            _current_state=current_state,
        )
        if group_index == 0:
            write_chunk0_marker = True
            chunk0_use_init = True
            chunk0_expected_session_count = 0
            group_expected_id = None
            group_expected_vol = None
        else:
            write_chunk0_marker = False
            chunk0_use_init = False
            # session_n is first_session + manifests, not MMC. BD-R
            # SRM+POW pins mediainfo().session_count at 1 after the
            # first burn.
            chunk0_expected_session_count = session_n
            group_expected_id = archive_id
            group_expected_vol = None

        rc, backend = _burn_volume_chunks(
            chunks,
            backend,
            command="init",
            label=init_label,
            enc_block=enc_block,
            crypto=crypto,
            cache=cache,
            verify_mod=verify_mod,
            mirror_backend=mirror_backend,
            archive_id=archive_id,
            base_volume_index=base_volume_index,
            chunk0_first_session=chunk0_first_session,
            write_chunk0_marker=write_chunk0_marker,
            chunk0_use_init=chunk0_use_init,
            chunk0_expected_session_count=chunk0_expected_session_count,
            expected_archive_id=group_expected_id,
            expected_volume_index=group_expected_vol,
            slow=getattr(args, "slow", False),
        )
        if rc != 0:
            return rc
        session_n += len(chunks)

    rescue_code = getattr(crypto, "_rescue_code", None)
    if rc == 0 and rescue_code:
        print(
            "\n"
            "RESCUE KEY -- store this offline. It is never written to the\n"
            "archive, this machine, or any log. If you lose the primary secret\n"
            "and this key, the archive is unrecoverable.\n"
            f"\n{rescue_code}\n",
            flush=True,
        )
    return rc


def _run_sync(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed sync namespace
    Output: exit code (0 even when no changes)
    Details:
        Reads disc state and diffs the source. On no changes, prints
        "All files up to date." and returns 0.
        Otherwise partitions the changeset against the local staging budget
        (session.partition_by_staging_budget). One group is the common case
        and behaves exactly as a single build_staging call always has. Two
        or more groups burn as automatic sequential sessions on one volume
        with no user interaction. The run appends every group, including
        group 0, with the sync parameters of this run.

        A changeset that also spans discs within one group still burns
        each chunk, then swaps media, verifies, and updates the cache. That
        disc-capacity split is orthogonal and unchanged.
        New volumes get a VolumeMarker before init.
    """
    if getattr(args, "dry_run", False):
        return _run_dry_run(args, is_init=False)

    from oddarchiver import session as session_mod
    from oddarchiver import verify as verify_mod

    backend = _make_backend(args)
    disc_info = backend.mediainfo()

    if disc_info.session_count == 0:
        _log.error("sync: disc not initialized (%s)", _backend_id(backend))
        print("error: disc not initialized; run 'init' first.", file=sys.stderr)
        return 1

    _log.info("sync: source=%s session=%d device=%s",
              args.source, disc_info.session_count, _backend_id(backend))

    mirror_backend = _make_mirror_backend(args)

    if getattr(args, "prefill", None):
        if isinstance(backend, ISOBackend):
            backend.prefill(parse_disc_size(args.prefill))

    from oddarchiver.volume import read_volume_marker, read_volume_baseline
    crypto, manifests, _raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )
    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=disc_info.session_count,
        command="sync",
    )
    baseline = read_volume_baseline(backend)
    disc_state = build_disc_state(
        manifests, seed=baseline[0] if baseline else None,
    )
    disc_origin = build_disc_origin(
        manifests, seed=baseline[1] if baseline else None,
    )
    enc_block = _encryption_block(crypto)
    label = manifests[0].label if manifests else "ARCHIVE"

    marker = read_volume_marker(backend)
    # Use marker.first_session + manifest count, not dvd+rw-mediainfo
    # session count. BD-R SRM+POW always reports session_count=1
    # regardless of how many appends occurred. A missing marker
    # (legacy/single-volume) treats first_session as 0.
    session_n = (marker.first_session if marker else 0) + len(manifests)
    source = Path(args.source)

    # Scan source with progress bar, compute diff, always show summary.
    import hashlib
    from oddarchiver.session import _print_bar

    print(f"Scanning {source} ...", flush=True)
    source_files = sorted(f for f in source.rglob("*") if f.is_file())
    total_files = len(source_files)
    print(f"  {total_files} file(s) found. Hashing...", flush=True)
    current_state: dict[str, str] = {}
    for i, sf in enumerate(source_files, 1):
        rel = str(sf.relative_to(source))
        current_state[rel] = hashlib.sha256(sf.read_bytes()).hexdigest()
        _print_bar(i, total_files, suffix=rel)
    if total_files:
        print()

    changed = {p for p in current_state if p in disc_state and current_state[p] != disc_state[p]}
    new_files = {p for p in current_state if p not in disc_state}
    deleted_files = [p for p in disc_state if p not in current_state]
    unchanged_count = len(current_state) - len(changed) - len(new_files)

    print(
        f"  {len(new_files)} new, {len(changed)} changed, "
        f"{len(deleted_files)} deleted, {unchanged_count} unchanged",
        flush=True,
    )

    if not changed and not new_files and not deleted_files:
        _log.info("sync: no changes detected; nothing to burn")
        print("All files up to date.", flush=True)
        return 0

    cache = CacheManager()
    changeset = new_files | changed
    groups = session_mod.partition_by_staging_budget(changeset, source)
    if len(groups) > 1:
        total_needed = sum((source / p).stat().st_size for p in changeset)
        staging_free = shutil.disk_usage(session_mod._default_staging_root()).free
        print(
            f"Source needs ~{_fmt_bytes(total_needed)}; staging has "
            f"~{_fmt_bytes(staging_free)} free. Splitting into {len(groups)} "
            "automatic passes.",
            flush=True,
        )

    archive_id = marker.archive_id if marker else uuid.uuid4().hex
    base_volume_index = marker.volume_index if marker else 0
    chunk0_first_session = marker.first_session if marker else 0
    expected_archive_id = marker.archive_id if marker else None
    expected_volume_index = marker.volume_index if marker else None
    rc = 0
    for group_index, group_paths in enumerate(groups):
        masked_disc_state = _mask_diff_to_group(
            current_state, disc_state, set(group_paths), group_index,
        )
        chunks = session_mod.build_staging(
            session_n=session_n,
            source=source,
            disc_state=masked_disc_state,
            disc_origin=disc_origin,
            backend=backend,
            cache=cache,
            crypto=crypto,
            _current_state=current_state,
            manifests=manifests,
        )
        # session_n is first_session + len(manifests), the same quantity
        # _guard re-reads. MMC session_count is not: BD-R SRM+POW
        # reports 1 after every append.
        chunk0_expected_session_count = session_n
        rc, backend = _burn_volume_chunks(
            chunks,
            backend,
            command="sync",
            label=label,
            enc_block=enc_block,
            crypto=crypto,
            cache=cache,
            verify_mod=verify_mod,
            mirror_backend=mirror_backend,
            archive_id=archive_id,
            base_volume_index=base_volume_index,
            chunk0_first_session=chunk0_first_session,
            write_chunk0_marker=False,
            chunk0_use_init=False,
            chunk0_expected_session_count=chunk0_expected_session_count,
            expected_archive_id=expected_archive_id,
            expected_volume_index=expected_volume_index,
            slow=getattr(args, "slow", False),
        )
        if rc != 0:
            return rc
        session_n += len(chunks)
    return rc


def _format_needed_volume(needed: object) -> str:
    """
    Input:  needed - NeededVolume-like object from restore()
    Output: CLI message telling the user which earlier volume to insert
    """
    volume_index = getattr(needed, "volume_index", "?")
    label = getattr(needed, "label", "") or ""
    first_session = getattr(needed, "first_session", None)
    last_session = getattr(needed, "last_session", None)
    extra = f"label {label}" if label else ""
    if (
        first_session is not None
        and last_session is not None
        and last_session >= first_session
    ):
        sess = f"sessions {first_session}-{last_session}"
        extra = f"{extra}, {sess}" if extra else sess
    detail = f" ({extra})" if extra else ""
    return (
        f"Restore needs volume {volume_index}{detail} next. "
        f"Insert it and re-run 'oddarchiver restore'."
    )


def _run_restore(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed restore namespace
    Output: exit code (0 success, 1 on any failure)
    Details:
        Calls restore.restore() and translates failure_count to exit code.
        The function prints a needed earlier volume and does not treat it
        as a failure.
        Recovery-mode stale/missing files print as a per-file listing and
        do not change the exit code.
        --rescue-key unwraps the enc_mode.json blob and skips the primary
        passphrase prompt or --key requirement.
    """
    from oddarchiver.restore import restore

    backend = _make_backend(args)
    crypto, manifests, _raw = _crypto_and_manifests(
        backend,
        key_path=getattr(args, "key", None),
        rescue_key=getattr(args, "rescue_key", None),
    )
    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=backend.mediainfo().session_count,
        command="restore",
    )

    result = restore(
        dest=Path(args.dest),
        backend=backend,
        crypto=crypto,
        session=getattr(args, "session", None),
        force=getattr(args, "force", False),
        paths=getattr(args, "paths", None),
        skip_volumes=getattr(args, "skip_volumes", None),
        read_mode=getattr(args, "read_mode", DEFAULT_READ_MODE),
        manifests=manifests,
    )
    if result.stale or result.missing:
        if result.stale:
            print("  stale:", flush=True)
            for rel_path, session_n in result.stale:
                print(f"    {rel_path}  (session {session_n})", flush=True)
        if result.missing:
            print("  missing:", flush=True)
            for rel_path in result.missing:
                print(f"    {rel_path}", flush=True)
    if result.needed is not None:
        print(_format_needed_volume(result.needed), flush=True)
    return 1 if result.failures else 0


def _run_history(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed history namespace
    Output: exit code
    Details:
        Reads all manifests. Prints one row per session.
        Columns: session, timestamp, files, total_bytes, encryption mode.
    """
    backend = _make_backend(args)
    crypto, manifests, _raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )
    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=backend.mediainfo().session_count,
        command="history",
    )

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


def _run_ls(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed ls namespace
    Output: exit code
    Details:
        Loads manifests the same way as history. Prints a path table.
        No blob reads. --path is a listing scope filter, not repeatable.
    """
    backend = _make_backend(args)
    crypto, manifests, _raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )
    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=backend.mediainfo().session_count,
        command="ls",
    )

    if not manifests:
        print("No sessions found.")
        return 0

    entries = build_disc_tree(manifests, at_session=getattr(args, "session", None))
    prefix = getattr(args, "path", None)
    if prefix:
        entries = [e for e in entries if _path_matches(e.path, prefix)]
    if not getattr(args, "show_deleted", False):
        entries = [e for e in entries if not e.deleted]

    header = (
        f"{'Path':<40} {'Size':>10} {'Origin Session':<14} {'Last Modified':<22}"
    )
    print(header)
    print("-" * len(header))
    for entry in entries:
        print(
            f"{entry.path:<40} {_fmt_bytes(entry.size):>10} "
            f"{entry.origin_session:03d}           {entry.last_modified:<22}"
            f"{_deleted_marker(entry)}"
        )
    return 0


def _run_browse(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed browse namespace
    Output: exit code
    Details:
        Builds the live disc tree once. Numbered listing of direct
        children. A number opens a directory or toggles a file. t N
        toggles any entry. r restores the selected set via restore().
    """
    from oddarchiver.restore import restore

    backend = _make_backend(args)
    crypto, manifests, _raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )
    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=backend.mediainfo().session_count,
        command="browse",
    )
    if not manifests:
        print("No sessions found.")
        return 0

    show_deleted = getattr(args, "show_deleted", False)
    tree = build_disc_tree(manifests)
    current = ""
    selected: set[str] = set()
    print(_BROWSE_HELP)

    while True:
        children = _direct_children(tree, current, show_deleted)
        location = f"/{current}" if current else "/"
        print()
        print(f"Browsing {location}  ({len(selected)} selected)")
        if not children:
            print("  (empty)")
        for i, (name, is_dir, entry) in enumerate(children, start=1):
            child_path = f"{current}/{name}" if current else name
            mark = "*" if child_path in selected else " "
            if is_dir:
                print(f"  {mark}{i:>3}  {name}/")
            else:
                extra = _deleted_marker(entry) if entry is not None else ""
                size = _fmt_bytes(entry.size) if entry is not None else ""
                print(f"  {mark}{i:>3}  {name:<32} {size:>10}{extra}")
        prompt = f"{location}> "
        try:
            raw = input(prompt)
        except EOFError:
            _log.warning("browse: EOF on input; quitting")
            print()
            return 0
        raw = raw.strip()
        if not raw:
            continue
        if raw in ("q", "quit"):
            return 0
        if raw in ("h", "help", "?"):
            print(_BROWSE_HELP)
            continue
        if raw == "..":
            if not current:
                print("Already at archive root.")
            elif "/" in current:
                current = current.rsplit("/", 1)[0]
            else:
                current = ""
            continue
        if raw == "s":
            if not selected:
                print("Selection is empty.")
            else:
                print("Selected:")
                for p in sorted(selected):
                    print(f"  {p}")
            continue
        if raw == "r":
            if not selected:
                print("Nothing selected. Use a file number or t N to mark paths.")
                continue
            try:
                dest_s = input("Restore to directory: ").strip()
            except EOFError:
                _log.warning("browse: EOF on restore destination; quitting")
                print()
                return 0
            if not dest_s:
                print("No destination given.")
                continue
            _, failures = restore(
                dest=Path(dest_s),
                backend=backend,
                crypto=crypto,
                paths=sorted(selected),
                manifests=manifests,
            )
            return 1 if failures else 0
        parts = raw.split()
        toggle = False
        idx_s = raw
        if parts[0] in ("t", "m") and len(parts) == 2:
            toggle = True
            idx_s = parts[1]
        if not idx_s.isdigit():
            print(f"Unknown command {raw!r}. Enter h for help.")
            continue
        idx = int(idx_s)
        if idx < 1 or idx > len(children):
            print("No entry with that number.")
            continue
        name, is_dir, _entry = children[idx - 1]
        child_path = f"{current}/{name}" if current else name
        if toggle or not is_dir:
            if child_path in selected:
                selected.discard(child_path)
                print(f"  unselected {child_path}")
            else:
                selected.add(child_path)
                print(f"  selected {child_path}")
        else:
            current = child_path


def _run_verify(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed verify namespace
    Output: exit code (verify() raises SystemExit(1) on failure)
    Details:
        Reads disc crypto then calls verify.verify(). Catches SystemExit.
    """
    from oddarchiver import verify as verify_mod

    backend = _make_backend(args)
    crypto, manifests, raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )

    try:
        verify_mod.verify(
            backend, crypto, level=args.level,
            manifests=manifests, skip_disc_io=False, control_raw=raw,
            read_mode=getattr(args, "read_mode", DEFAULT_READ_MODE),
        )
        return 0
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1


def _mirror_health(
    manifests: list[Manifest], primary_id: str
) -> list[tuple[int, str, bool]]:
    """
    Input:  manifests  - all session manifests
            primary_id - identifier of the primary backend (excluded from results)
    Output: list of (session_n, mirror_drive, accessible) for sessions with mirrors
    Details:
        For ISO paths (not starting with /dev/): checks file existence.
        For device paths: reports True without probing (cannot mount inline).
    """
    results = []
    for m in manifests:
        if len(m.drives) < 2:
            continue
        for drive in m.drives:
            if drive == primary_id:
                continue
            if drive.startswith("/dev/"):
                accessible = True  # cannot probe device inline
            else:
                accessible = Path(drive).exists()
            results.append((m.session, drive, accessible))
    return results


def _run_status(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed status namespace
    Output: exit code
    Details:
        Prints disc label, session count, used/remaining space, capacity
        warnings, and any SUSPECT manifests.
    """
    backend = _make_backend(args)
    crypto, manifests, _raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )
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

    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=disc_info.session_count,
        command="status",
    )
    from oddarchiver.verify import detect_incomplete_session
    incomplete = detect_incomplete_session(
        backend, {m.session if m.session >= 0 else i: m for i, m in enumerate(manifests)},
    )
    if incomplete is not None:
        print(
            f"\nINCOMPLETE trailing session {incomplete:03d} "
            "(interrupted burn, not corruption)."
        )
        print(
            "Do not eject; run 'oddarchiver verify --level checksum' "
            "before another sync."
        )
    suspects = [m for m in manifests if m.suspect]
    if suspects:
        print(f"\nSUSPECT sessions ({len(suspects)}):")
        for m in suspects:
            suspect(_log, "session_%03d: manifest checksum mismatch", m.session)
            print(f"  session_{m.session:03d}: manifest checksum mismatch")

    mirror_rows = _mirror_health(manifests, _backend_id(backend))
    if mirror_rows:
        print("\nMirror health:")
        for session_n, drive, accessible in mirror_rows:
            status_str = "OK" if accessible else "MISSING"
            if not accessible:
                _log.error("session_%03d: mirror %s is MISSING", session_n, drive)
            print(f"  session_{session_n:03d}: {drive} [{status_str}]")

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
