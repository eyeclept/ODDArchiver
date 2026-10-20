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
import contextlib
import getpass
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from oddarchiver.cache import CacheManager
from oddarchiver.config import resolve_config
from oddarchiver.crypto import make_crypto, NullCrypto, blob_aad, is_bound_ciphertext
from oddarchiver.disc import (
    ISOBackend, DiscBackend, parse_disc_size, BurnInterrupted,
    check_volume_identity,
)
from oddarchiver.log import _fmt_bytes, check_capacity, setup_logging, suspect
from oddarchiver.diskread import DEFAULT_READ_MODE, READ_MODES
from oddarchiver.manifest import (
    Manifest, TreeEntry, read_manifest, write_manifest, build_disc_state,
    build_disc_origin, build_disc_tree, load_session_manifests,
    session_control_paths,
)
from oddarchiver.catalog import (
    archive_memory_path, build_archive_catalog, catalog_tree, disc_name,
    highest_seen_volume, remember_discs, remember_encryption_mode,
    remember_inserted_disc, remembered_encryption_mode, safe_display,
)
from oddarchiver.volume import BaselineReadError

if TYPE_CHECKING:
    from oddarchiver.disc import BurnBackend
    from oddarchiver.crypto import CryptoBackend

# Globals
COMMANDS = ["init", "sync", "restore", "history", "ls", "browse", "verify", "status"]
logger = logging.getLogger(__name__)
_log = logger
_MARKER_UNSET = object()
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
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run 'oddarchiver COMMAND -h' for the options of one command.\n"
            "\n"
            "Defaults: --device and --disc-size fall back to device and disc_size in\n"
            "~/.config/oddarchiver/config.toml, then to /dev/sr0 and 25gb.\n"
            "Environment: ODDARCHIVER_PASSPHRASE supplies the passphrase for\n"
            "passphrase-encrypted archives. Without it, the command prompts.\n"
            "Exit status: 0 on success, 1 on error."
        ),
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
    _add_device(p)
    p.add_argument(
        "--label", default="ARCHIVE", metavar="LABEL",
        help="Archive label. Part of the ledger key. Default: ARCHIVE.",
    )
    p.add_argument(
        "--encrypt", choices=["none", "passphrase", "keyfile"], default="none",
        help=(
            "Encryption mode. 'passphrase' reads ODDARCHIVER_PASSPHRASE or "
            "prompts twice. 'keyfile' needs --key. Both print a rescue key once. "
            "Default: none."
        ),
    )
    p.add_argument(
        "--key", metavar="PATH",
        help="Keyfile path. Required with --encrypt keyfile.",
    )
    _add_test_iso(p)
    _add_dry_run(p)
    _add_disc_size(p)
    _add_prefill(p)
    _add_mirror(p)
    _add_recover_scan(p)
    _add_slow(p)
    _add_skip_large(p)
    _add_embed_software(p)
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
    _add_device(p)
    _add_test_iso(p)
    _add_dry_run(p)
    p.add_argument(
        "--no-cache", action="store_true", dest="no_cache",
        help=(
            "Read every prior blob from the disc, not from the local cache. "
            "The cache still gets the new session after a verified burn."
        ),
    )
    _add_disc_size(p)
    _add_prefill(p)
    _add_mirror(p)
    _add_key(p)
    _add_recover_scan(p)
    _add_slow(p)
    _add_skip_large(p)
    _add_embed_software(p)
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


def _add_skip_large(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a burn command
    Output: None
    Details:
        Adds --skip-large. See _apply_skip_large for the rule it triggers.
    """
    p.add_argument(
        "--skip-large",
        action="store_true",
        dest="skip_large",
        help=(
            "Leave out any new or changed file that does not fit on one disc, "
            "or that is bigger than the local staging space, and list it. "
            "Without this flag such a file is split across volumes, or the "
            "run stops with an error. A skipped changed file keeps its "
            "previous version on the archive; the next run tries it again."
        ),
    )


def _add_embed_software(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a burn command
    Output: None
    Details:
        Adds --embed-software {minimal,all}. Default is off. Writes a
        plain oddarchiver-software/ directory onto the disc.
    """
    p.add_argument(
        "--embed-software",
        choices=["minimal", "all"],
        default=None,
        dest="embed_software",
        help=(
            "Write a plain copy of the oddarchiver source onto the disc as "
            "oddarchiver-software/. 'minimal' is the package needed to restore "
            "and verify. 'all' is the full repo plus vendored argon2-cffi and "
            "cryptography. Off by default."
        ),
    )


def _add_read_mode(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a disc-read command
    Output: None
    Details:
        Adds --read-mode sequential|full-copy. All-disc restore selects
        a mode from free space when no mode is given.
    """
    p.add_argument(
        "--read-mode",
        choices=list(READ_MODES),
        default=None,
        dest="read_mode",
        help=(
            "How to extract session blobs from the disc. "
            "For --all-disks restore, the default depends on free space. "
            "For one disc and verify, the default is 'sequential'. "
            "'full-copy' extracts every session up front."
        ),
    )


def _add_device(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a command that reads or writes media
    Output: None
    Details:
        Adds --device. The default stays None so dispatch() can apply config.
    """
    p.add_argument(
        "--device", default=None, metavar="DEV",
        help="Optical drive device node. Default: device from config.toml, else /dev/sr0.",
    )


def _add_test_iso(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a command that reads or writes media
    Output: None
    Details:
        Adds --test-iso. A set path selects ISOBackend instead of DiscBackend.
        Restore and other read commands require that PATH exists. init creates it.
    """
    p.add_argument(
        "--test-iso", metavar="PATH", dest="test_iso",
        help=(
            "Use the ISO image at PATH instead of an optical drive. "
            "Overrides --device. PATH must exist except for init."
        ),
    )


def _add_key(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a command that opens an existing archive
    Output: None
    Details:
        Adds --key for archives that use keyfile encryption.
    """
    p.add_argument(
        "--key", metavar="PATH",
        help="Keyfile path. Required when the archive uses keyfile encryption.",
    )


def _add_dry_run(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a burn command
    Output: None
    Details:
        Adds --dry-run. It may be combined with --test-iso.
    """
    p.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help=(
            "Scan the source and print the planned changes and size estimate. "
            "Writes no session. Combine with --test-iso to plan against an ISO."
        ),
    )


def _add_disc_size(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a burn command
    Output: None
    Details:
        Adds --disc-size. Only the ISO backend reads it.
    """
    p.add_argument(
        "--disc-size", default=None, dest="disc_size", metavar="SIZE",
        help=(
            "Capacity of the ISO image. Used only with --test-iso. "
            "SIZE is a number with a unit (500mb, 25gb, 93gib), a raw byte count, "
            "or a preset: cd, dvd-sl, dvd-dl, bd-r, bd-dl, bdxl. "
            "Default: disc_size from config.toml, else 25gb. "
            "A real drive reports its own capacity."
        ),
    )


def _add_prefill(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a burn command
    Output: None
    Details:
        Adds --prefill. Only the ISO backend applies it.
    """
    p.add_argument(
        "--prefill", metavar="SIZE",
        help=(
            "Mark SIZE bytes of the ISO as already used, to test capacity "
            "warnings. SIZE uses the same format as --disc-size. "
            "Applies only with --test-iso."
        ),
    )


def _add_mirror(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a burn command
    Output: None
    Details:
        Adds --mirror. The burn code rejects it for multi-chunk runs.
    """
    p.add_argument(
        "--mirror", metavar="PATH",
        help=(
            "Second drive device, or ISO path with --test-iso. Each session is "
            "also burned there. Not supported when the run needs a disc swap."
        ),
    )


def _add_recover_scan(p: argparse.ArgumentParser) -> None:
    """
    Input:  p - argparse parser for a command that checks the session ledger
    Output: None
    Details:
        Adds --recover-scan.
    """
    p.add_argument(
        "--recover-scan", action="store_true", dest="recover_scan",
        help=(
            "Bypass the last-known-good ledger check and treat the readable "
            "sessions as authoritative. Logs a warning instead of failing "
            "when sessions are missing."
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
    _add_device(p)
    _add_test_iso(p)
    p.add_argument(
        "--session", type=int, metavar="N",
        help="Restore the state as of session N. Default: latest session.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Rewrite destination files even when they already match the target checksum.",
    )
    _add_key(p)
    class _RescueKeyFlag(argparse.Action):
        """Require a destination before the rescue-key flag."""

        def __call__(self, parser, namespace, values, option_string=None):
            if getattr(namespace, "dest", None) is None:
                raise argparse.ArgumentError(
                    self, "put the destination before --rescue-key"
                )
            setattr(namespace, self.dest, "")

    p.add_argument(
        "--rescue-key",
        action=_RescueKeyFlag,
        nargs=0,
        default=None,
        dest="rescue_key",
        help=(
            "Recover with the rescue key printed at init. "
            "Put the destination first, then enter the key at the prompt "
            "or pipe it into stdin."
        ),
    )
    selection = p.add_mutually_exclusive_group()
    selection.add_argument(
        "--path", action="append", dest="paths", metavar="REL_PATH",
        help="Restore this relative path or directory prefix. Repeatable.",
    )
    selection.add_argument(
        "--all-files", action="store_true",
        help="Restore every live file.",
    )
    p.add_argument(
        "--all-disks", action="store_true",
        help="Use the archive catalog and request discs with selected files.",
    )
    p.add_argument(
        "--skip-volume",
        type=int,
        action="append",
        dest="skip_volumes",
        metavar="VOLUME_INDEX",
        help="Mark a disc as missing. Repeat to mark more discs.",
    )
    p.add_argument(
        "--unskip-volume", type=int, action="append", dest="unskip_volumes",
        metavar="VOLUME_INDEX",
        help="Clear a saved missing-disc mark. Repeatable.",
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
    _add_device(p)
    _add_test_iso(p)
    _add_key(p)



def _add_ls(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action
    Output: None
    Details:
        ls - list archive paths from disc/ISO manifests. No blob reads.
    """
    p = sub.add_parser("ls", help="List archive contents from disc or ISO manifests.")
    _add_device(p)
    _add_test_iso(p)
    _add_key(p)
    p.add_argument(
        "--session", type=int, metavar="N",
        help="List the state as of session N. Default: latest session.",
    )
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
    p.add_argument(
        "--all-disks", action="store_true",
        help="List the archive from this disc and its earlier disc records.",
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
    _add_device(p)
    _add_test_iso(p)
    _add_key(p)
    p.add_argument(
        "--all-disks", action="store_true",
        help="Browse the archive catalog and request discs for a restore.",
    )
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
    _add_device(p)
    _add_test_iso(p)
    p.add_argument(
        "--level", choices=["fast", "checksum", "full"], default="fast",
        help=(
            "Check depth. 'fast' checks manifest checksums and reads no blob "
            "bytes. 'checksum' also reads and hashes every blob. "
            "'full' also rebuilds each file's latest state and compares it to "
            "the recorded checksum. Default: fast."
        ),
    )
    _add_key(p)
    _add_read_mode(p)


def _add_status(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub - subparsers action
    Output: None
    Details:
        status - print disc/ISO state and any SUSPECT entries.
    """
    p = sub.add_parser("status", help="Show disc/ISO state and warnings.")
    _add_device(p)
    _add_test_iso(p)
    _add_key(p)
    _add_recover_scan(p)


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
        _log.warning("Command exited with code %s", exc.code)
        return int(exc.code) if exc.code is not None else 1
    except KeyboardInterrupt:
        _log.warning("Command interrupted by user.")
        print("\nInterrupted.", file=sys.stderr)
        return 1
    except ValueError as exc:
        # Expected refusals (wrong key, unsafe keyfile, refused disc) use
        # ValueError. The log file keeps the traceback. The terminal shows
        # only the error line.
        _log.warning("%s refused: %s", args.command, exc, exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        _log.exception("Unhandled error in %s", args.command)
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
    marker: object = _MARKER_UNSET,
) -> list[Manifest]:
    """
    Input:  backend - BurnBackend to read from
            crypto  - CryptoBackend for encrypted manifests (None means plaintext only)
            marker  - already-read VolumeMarker, or unset to read it here
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
    if marker is _MARKER_UNSET:
        marker = read_volume_marker(backend)
    return load_session_manifests(
        backend, crypto,
        start_session=marker.first_session if marker else 0,
        marker=marker,
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

def _user_supplied_secret(
    key_path: str | None, rescue_key: str | None,
) -> bool:
    """
    Input:  key_path, rescue_key - CLI key flags
    Output: True when the user supplied --key, --rescue-key, or a passphrase
    """
    if key_path:
        return True
    if rescue_key is not None:
        return True
    if os.environ.get("ODDARCHIVER_PASSPHRASE"):
        return True
    return False


def _enc_metadata_names(files: dict[str, bytes]) -> list[str]:
    """
    Input:  files - control-file bytes from one read_paths batch
    Output: names of encrypted metadata files present on the disc
    """
    names = []
    for name in files:
        if name.endswith("manifest.enc") or name == "volume_baseline.enc":
            names.append(name)
    return names


def _crypto_mode_name(crypto: "CryptoBackend") -> str | None:
    """
    Input:  crypto - backend built for this disc
    Output: passphrase or keyfile, or None for plaintext
    """
    from oddarchiver.crypto import KeyfileCrypto, PassphraseCrypto

    if isinstance(crypto, PassphraseCrypto):
        return "passphrase"
    if isinstance(crypto, KeyfileCrypto):
        return "keyfile"
    return None


def _require_iso_exists(backend: "BurnBackend") -> None:
    """
    Input:  backend - disc or ISO backend
    Output: None
    Details: Exit 1 when an ISO path is set and the file is missing.
    """
    if isinstance(backend, ISOBackend) and not backend.iso_path.exists():
        print(
            f"error: ISO image {backend.iso_path} does not exist.",
            file=sys.stderr,
        )
        raise SystemExit(1)


class _ControlFiles(dict):
    """Control-file bytes from one read_paths batch, plus the parsed marker."""

    def __init__(self, files: dict[str, bytes], marker: object) -> None:
        super().__init__(files)
        self.marker = marker


def _disc_mode(
    files: dict[str, bytes], enc_mode_path: str, key_path: str | None,
) -> tuple[str, dict]:
    """
    Input:  files - control-file bytes; enc_mode_path - session mode path
            key_path - optional keyfile path
    Output: encryption mode and parsed mode metadata
    """
    mode = "none"
    enc_mode_data: dict = {}
    enc_mode_bytes = files.get(enc_mode_path)
    if enc_mode_bytes is not None:
        try:
            parsed = json.loads(enc_mode_bytes.decode("utf-8"))
            if not isinstance(parsed, dict) or not isinstance(parsed.get("mode"), str):
                raise ValueError("expected an object with a mode")
            if parsed["mode"] not in ("none", "passphrase", "keyfile"):
                raise ValueError(f"unsupported mode {parsed['mode']!r}")
            enc_mode_data = parsed
            mode = parsed["mode"]
        except (ValueError, UnicodeDecodeError) as exc:
            _log.warning("Cannot parse %s: %s", enc_mode_path, exc)
            raise ValueError(f"Cannot read {enc_mode_path} on this disc: {exc}") from exc
    else:
        enc_names = _enc_metadata_names(files)
        from oddarchiver.volume import parse_volume_marker_bytes
        marker = parse_volume_marker_bytes(files.get("volume.json"))
        remembered = remembered_encryption_mode(marker.archive_id) if marker else None
        if enc_names:
            mode = remembered if remembered is not None else (
                "keyfile" if key_path else "unknown"
            )
        elif remembered is not None:
            mode = remembered
    return mode, enc_mode_data


def _refuse_plaintext_secret(
    mode: str, files: dict[str, bytes], manifest_path: str,
    key_path: str | None, rescue_key: str | None,
) -> None:
    """
    Input:  mode - disc encryption mode; files - control-file bytes
            manifest_path - session manifest path; key_path, rescue_key - CLI keys
    Output: None, or refusal for inconsistent plaintext metadata or supplied keys
    """
    if mode == "none":
        enc_names = _enc_metadata_names(files)
        if enc_names:
            raise ValueError(
                "This disc reports no encryption but it holds encrypted "
                "metadata. Refuse this disc."
            )
        if _user_supplied_secret(key_path, rescue_key):
            named = f" The disc has {manifest_path}." if manifest_path in files else ""
            if rescue_key is not None:
                raise ValueError(
                    "This archive has no rescue key. It predates this "
                    "feature. You supplied a key but this disc reports no "
                    f"encryption. Refuse this disc.{named}"
                )
            raise ValueError(
                f"This disc reports no encryption. You supplied a key. "
                f"Refuse this disc.{named}"
            )


def _rescue_disc_crypto(mode: str, enc_mode_data: dict) -> "CryptoBackend":
    """
    Input:  mode - disc encryption mode; enc_mode_data - parsed mode metadata
    Output: backend unlocked with a rescue code read from terminal or stdin
    """
    rescue_block = enc_mode_data.get("rescue")
    if not isinstance(rescue_block, dict) or "wrapped" not in rescue_block:
        raise ValueError(
            "this archive has no rescue key; it predates this feature"
        )
    if sys.stdin.isatty():
        rescue_key = getpass.getpass("Rescue key: ")
    else:
        rescue_key = sys.stdin.readline().rstrip("\r\n")
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


def _ordinary_disc_crypto(mode: str, key_path: str | None) -> "CryptoBackend":
    """
    Input:  mode - disc encryption mode; key_path - optional keyfile path
    Output: backend for the stored encryption mode
    """
    if mode == "none":
        return make_crypto("none")
    if mode == "unknown":
        passphrase = os.environ.get("ODDARCHIVER_PASSPHRASE", "")
        if passphrase:
            return make_crypto("passphrase", passphrase=passphrase.encode())
        print("error: --key is required for this encrypted disc.", file=sys.stderr)
        raise SystemExit(1)
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
        Reads the first session's validated enc_mode.json from the shared
        control batch. When that file is absent, encrypted metadata and
        local mode memory establish the mode. It never trusts a plaintext
        manifest for mode detection. Refuses mode none when a secret was
        supplied or when encrypted metadata exists.
    """
    from oddarchiver.volume import read_volume_marker
    if first_session is None:
        marker = read_volume_marker(backend)
        first = marker.first_session if marker else 0
    else:
        first = first_session
    enc_mode_path = f"session_{first:03d}/enc_mode.json"
    manifest_path = f"session_{first:03d}/manifest.json"
    if raw is not None:
        files = raw
    else:
        paths = list(session_control_paths(backend))
        paths.extend(("volume_baseline.enc", "volume_baseline.json", "volume.json"))
        files = backend.read_paths(paths)
    mode, enc_mode_data = _disc_mode(files, enc_mode_path, key_path)

    _refuse_plaintext_secret(mode, files, manifest_path, key_path, rescue_key)

    if rescue_key is not None:
        return _rescue_disc_crypto(mode, enc_mode_data)

    return _ordinary_disc_crypto(mode, key_path)


def _check_remembered_disc_mode(
    marker: object, remembered: str | None, mode_name: str,
) -> None:
    """
    Input:  marker - parsed volume marker; remembered - saved mode;
            mode_name - reported mode
    Output: None; refuses a disc that disagrees with local mode memory
    """
    if remembered is not None and remembered != mode_name:
        path = archive_memory_path(marker.archive_id)
        if mode_name == "none":
            raise ValueError(
                f"Local disc record {path} says this archive is encrypted. "
                f"This disc reports no encryption. Move {path} aside if you "
                "trust this disc."
            )
        raise ValueError(
            f"Local disc record {path} says this archive uses {remembered} "
            f"encryption. This disc reports {mode_name}. Move {path} aside "
            "if you trust this disc."
        )


def _confirm_plaintext_disc(
    backend: "BurnBackend", marker: object, mode_name: str,
    remembered: str | None, key_path: str | None, rescue_key: str | None,
) -> None:
    """
    Input:  backend - disc backend; marker - parsed marker;
            mode_name, remembered - reported and saved modes;
            key_path, rescue_key - optional supplied secrets
    Output: None; confirms and records an unrecognized plaintext disc
    """
    if (
        marker is not None and mode_name == "none" and remembered is None
        and not _user_supplied_secret(key_path, rescue_key)
    ):
        label = safe_display(backend.mediainfo().label)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            message = (
                f"error: disc {label} reports no encryption and this host "
                "has no saved mode for the archive. Run the same command "
                "once from a terminal to confirm the disc is unencrypted."
            )
            _log.warning("%s", message)
            print(message, file=sys.stderr)
            raise SystemExit(1)
        warning = (
            f"Warning: disc {label} reports no encryption and this host "
            "has no saved mode for the archive."
        )
        _log.warning("%s", warning)
        print(warning, file=sys.stderr)
        print(
            f"This disc has no encryption. Is archive {label} a "
            "plaintext archive? [y/N]",
            end=" ", flush=True,
        )
        if sys.stdin.readline().strip().lower() != "y":
            raise SystemExit(1)
        remember_encryption_mode(marker.archive_id, "none")


def _remember_verified_encryption(
    marker: object, mode_name: str, manifests: list[Manifest],
) -> None:
    """
    Input:  marker - parsed volume marker; mode_name - reported mode;
            manifests - loaded session manifests
    Output: None; records encrypted mode only for a trusted manifest
    """
    if (
        marker is not None and mode_name != "none"
        and any(not m.suspect for m in manifests)
    ):
        remember_encryption_mode(marker.archive_id, mode_name)


def _crypto_and_manifests(
    backend: "BurnBackend",
    key_path: str | None = None,
    rescue_key: str | None = None,
    marker: object = _MARKER_UNSET,
) -> tuple["CryptoBackend", list[Manifest], dict[str, bytes]]:
    """
    Input:  backend, key_path, rescue_key - same as _crypto_for_disc
            marker - already-read VolumeMarker, or unset to parse it here
    Output: (crypto, manifests, raw_control_files)
    Details:
        One backend.read_paths() of the session control files, both
        baseline names, and volume.json serves encryption-mode detection,
        the marker parse, and the session manifest scan. The parsed marker
        is raw.marker. A missing ISO image exits 1. A mode that differs
        from the local disc record is refused. A plaintext disc with no
        saved mode and no secret requires one terminal confirmation. Without
        a terminal, the command stops before it reads the manifests.
    """
    from oddarchiver.volume import parse_volume_marker_bytes

    _require_iso_exists(backend)
    paths = list(session_control_paths(backend))
    paths.extend(("volume_baseline.enc", "volume_baseline.json"))
    if "volume.json" not in paths:
        paths.append("volume.json")
    raw_files = backend.read_paths(paths)
    if marker is _MARKER_UNSET:
        marker = parse_volume_marker_bytes(raw_files.get("volume.json"))
    first = marker.first_session if marker else 0
    crypto = _crypto_for_disc(
        backend, key_path=key_path, rescue_key=rescue_key,
        raw=raw_files, first_session=first,
    )
    remembered = remembered_encryption_mode(marker.archive_id) if marker is not None else None
    mode_name = _crypto_mode_name(crypto) or "none"
    _check_remembered_disc_mode(marker, remembered, mode_name)
    _confirm_plaintext_disc(backend, marker, mode_name, remembered, key_path, rescue_key)
    manifests = load_session_manifests(
        backend, crypto, start_session=first, raw=raw_files, marker=marker,
    )
    _remember_verified_encryption(marker, mode_name, manifests)
    return crypto, manifests, _ControlFiles(raw_files, marker)


def _init_passphrase() -> bytes:
    """
    Input:  None
    Output: passphrase bytes from the environment or confirmed prompt
    """
    passphrase = os.environ.get("ODDARCHIVER_PASSPHRASE", "")
    if not passphrase:
        while True:
            passphrase = getpass.getpass("Passphrase: ")
            confirm = getpass.getpass("Confirm passphrase: ")
            if passphrase == confirm:
                break
            print("Passphrases do not match. Try again.", file=sys.stderr)
    return passphrase.encode()


def _attach_rescue_code(crypto: "CryptoBackend") -> "CryptoBackend":
    """
    Input:  crypto - encrypted backend for an init session
    Output: crypto with its rescue secret attached, or unchanged subtype
    """
    from oddarchiver.crypto import KeyfileCrypto, PassphraseCrypto, generate_rescue_code, wrap_rescue_secret

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

    mode = getattr(args, "encrypt", "none")
    if mode == "none":
        return make_crypto("none")
    if mode == "passphrase":
        crypto: "CryptoBackend" = make_crypto(
            "passphrase", passphrase=_init_passphrase()
        )
    elif mode == "keyfile":
        key_path = getattr(args, "key", None)
        if not key_path:
            print("error: --key is required for keyfile mode.", file=sys.stderr)
            raise SystemExit(1)
        crypto = make_crypto("keyfile", keyfile_path=key_path)
    else:
        raise ValueError(f"Unknown mode: {mode!r}")

    return _attach_rescue_code(crypto)


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
            crypto     - backend used to write and read the manifest
    Output: updated Manifest (also written to staging atomically)
    Details:
        Reads the staged manifest in its original format. Sets the label,
        encryption, and drives, then writes the manifest atomically.
        Encrypted staging stays encrypted during this update.
    """
    from oddarchiver.crypto import NullCrypto

    session_dir = staging / f"session_{session_n:03d}"
    encrypted = crypto is not None and not isinstance(crypto, NullCrypto)
    name = "manifest.enc" if encrypted else "manifest.json"
    manifest = read_manifest(session_dir / name, crypto=crypto, slot=session_n)
    manifest.label = label
    manifest.encryption = encryption
    if drives is not None:
        manifest.drives = drives
    baseline_path = staging / "volume_baseline.enc"
    if not baseline_path.exists():
        baseline_path = staging / "volume_baseline.json"
    if baseline_path.exists():
        import hashlib
        manifest.baseline_checksum = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    write_manifest(session_dir, manifest, crypto=crypto)
    return manifest


def _verified_delta_payload(
    entry: object, sidecar: Path, session_n: int, crypto: "CryptoBackend",
) -> tuple[str, int, bytes] | None:
    """
    Input:  entry, sidecar, session_n, crypto - delta cache content and verifier
    Output: encrypted full-content payload, or None when unavailable or invalid
    """
    import hashlib

    full_path = sidecar / entry.result_checksum
    if not full_path.is_file():
        _log.warning("cache full content missing for %s in session %d", entry.path, session_n)
        return None
    encrypted_full = full_path.read_bytes()
    if hashlib.sha256(crypto.decrypt(encrypted_full)).hexdigest() != entry.result_checksum:
        _log.warning(
            "cache full content checksum did not match for %s in session %d",
            entry.path, session_n,
        )
        return None
    return entry.path, session_n, encrypted_full


def _full_cache_payload(entry: object, staging: Path, session_n: int) -> tuple[str, int, bytes] | None:
    """
    Input:  entry, staging, session_n - staged full blob source
    Output: encrypted cache payload, or None when no blob is available
    """
    blob_rel = entry.file
    if not blob_rel:
        return None
    blob_path = staging / blob_rel
    if blob_path.exists():
        return entry.path, session_n, blob_path.read_bytes()
    return None


def _cache_payloads(
    entries: list, sidecar: Path, staging: Path, session_n: int, crypto: "CryptoBackend",
) -> Iterator[tuple[str, int, bytes]]:
    """
    Input:  entries, sidecar, staging, session_n, crypto - staged cache sources
    Output: verified encrypted cache entries one at a time
    """
    for entry in entries:
        if entry.type == "delta":
            payload = _verified_delta_payload(entry, sidecar, session_n, crypto)
            if payload is not None:
                yield payload
            continue
        payload = _full_cache_payload(entry, staging, session_n)
        if payload is not None:
            yield payload


def _update_cache(
    cache: CacheManager,
    staging: Path,
    session_n: int,
    entries: list,
    crypto: "CryptoBackend",
) -> None:
    """
    Input:  cache, staging, session_n, entries, and archive crypto
    Output: None
    Details:
        Cache full entries from their burned blobs. Cache delta entries
        from the encrypted full-content copy made during staging.
        Skip a delta when its copied content fails the result checksum.
    """
    sidecar = staging.with_name(staging.name + ".cache_full")
    cache.put_many(_cache_payloads(entries, sidecar, staging, session_n, crypto))


@contextlib.contextmanager
def _prior_blob_cache(cache: CacheManager, no_cache: bool) -> Iterator[CacheManager]:
    """
    Input:  cache    - the local cache
            no_cache - True when the user gave --no-cache
    Output: context manager that yields the cache that staging reads prior blobs from
    Details:
        Without --no-cache, yields the local cache. With --no-cache, yields
        an empty private cache in a new 0700 directory beside the local
        cache. Every prior blob then comes from the disc, still in one
        batched read per session, and no disc blob enters the local
        cache. The private directory is removed on exit.
    """
    if not no_cache:
        yield cache
        return
    from oddarchiver.session import remove_tree

    parent = cache.cache_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix="oddarchiver_nocache_", dir=parent))
    _log.info("--no-cache: prior blobs come from the disc via %s", private)
    try:
        yield CacheManager(cache_dir=private)
    finally:
        remove_tree(private, "--no-cache read directory")




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


def _child_under_prefix(entry: TreeEntry, prefix: str, show_deleted: bool) -> str | None:
    """
    Input:  entry, prefix, show_deleted - tree row and directory selection
    Output: relative path below prefix, or None if excluded
    """
    if entry.deleted and not show_deleted:
        return None
    rel = entry.path
    if prefix:
        if rel == prefix:
            return None
        if not rel.startswith(prefix + "/"):
            return None
        rest = rel[len(prefix) + 1 :]
    else:
        rest = rel
    if not rest:
        return None
    return rest


def _child_rows(
    files: dict[str, TreeEntry], dirs: set[str],
) -> list[tuple[str, bool, TreeEntry | None]]:
    """
    Input:  files, dirs - direct child entries and collapsed directories
    Output: child rows sorted by name, with directories taking precedence
    """
    names = sorted(set(dirs) | set(files))
    rows: list[tuple[str, bool, TreeEntry | None]] = []
    for name in names:
        rows.append((name, True, None) if name in dirs else (name, False, files[name]))
    return rows


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
        rest = _child_under_prefix(entry, prefix, show_deleted)
        if rest is None:
            continue
        name, sep, more = rest.partition("/")
        if sep:
            dirs.add(name)
        else:
            files[name] = entry
    return _child_rows(files, dirs)


def _apply_skip_large(
    args: argparse.Namespace,
    paths: set[str],
    source: Path,
    current_state: dict[str, str],
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    disc_state: dict[str, str],
    disc_origin: dict[str, int],
    manifests: list | None = None,
) -> set[str]:
    """
    Input:  args          - parsed namespace; reads skip_large
            paths         - new and changed source-relative paths
            source, current_state, backend, crypto, disc_state, disc_origin,
            manifests     - run context for session.find_unfittable_paths
    Output: paths minus every unfittable file; paths itself when --skip-large is off
    Details:
        Prints and logs each skipped file with its reason. A skipped path
        never reaches staging, so build_staging sees it as unchanged: a
        changed file keeps its old version and is not tombstoned.
    """
    if not getattr(args, "skip_large", False):
        return paths
    from oddarchiver import session as session_mod

    skipped = session_mod.find_unfittable_paths(
        paths, source, current_state, backend, crypto,
        disc_state, disc_origin, manifests,
    )
    if skipped:
        print(f"Skipping {len(skipped)} file(s) that do not fit (--skip-large):", flush=True)
        for rel_path, reason in skipped.items():
            print(f"  {rel_path}: {reason}", flush=True)
            _log.warning("skip-large: %s: %s", rel_path, reason)
    return paths - skipped.keys()


def _dry_run_disc_state(
    args: argparse.Namespace,
    backend: "BurnBackend",
    disc_info: object,
    is_init: bool,
) -> tuple[dict[str, str], dict[str, int], list[Manifest] | None, str, int, "CryptoBackend"] | None:
    """
    Input:  args, backend, disc_info - dry-run configuration and media state
            is_init - whether this is an initialization
    Output: disc state, origins, manifests, label, next session, and crypto;
            None when the disc cannot be used for this operation
    """
    if is_init:
        if disc_info.session_count > 0:
            print("warning: disc already initialized; skipping init.", file=sys.stderr)
            return None
        disc_state: dict[str, str] = {}
        disc_origin: dict[str, int] = {}
        manifests = None
        label = getattr(args, "label", "ARCHIVE")
        session_n = 0
        crypto = _make_init_crypto(args)
    else:
        if disc_info.session_count == 0:
            print("error: disc not initialized; run 'init' first.", file=sys.stderr)
            return None
        crypto, manifests, raw = _crypto_and_manifests(
            backend, key_path=getattr(args, "key", None),
        )
        marker = raw.marker
        if manifests and all(m.suspect for m in manifests):
            print(
                "error: Cannot decrypt the session manifest. Check --key.",
                file=sys.stderr,
            )
            return None
        disc_state = build_disc_state(manifests)
        disc_origin = build_disc_origin(manifests)
        label = manifests[0].label if manifests else "ARCHIVE"
        # Match _run_sync: marker.first_session plus manifest count, not
        # dvd+rw-mediainfo session count. BD-R SRM+POW reports
        # session_count=1 no matter how many appends happened, because
        # growisofs adds tracks inside one MMC session. Reading the disc
        # count here made dry-run announce a session number that was
        # already used. A missing marker treats first_session as 0.
        session_n = (marker.first_session if marker else 0) + len(manifests)
    return disc_state, disc_origin, manifests, label, session_n, crypto


def _scan_dry_source(source: Path) -> tuple[dict[str, str], int]:
    """
    Input:  source - directory to scan for dry-run files
    Output: source-relative SHA-256 hashes and total source bytes
    """
    import hashlib
    from oddarchiver.session import _print_bar

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
    return current_state, total_source_bytes


def _dry_prior_jobs(
    changed: set[str],
    source: Path,
    disc_origin: dict[str, int],
    session_n: int,
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    dry_manifests: list[Manifest] | None,
    disc_state: dict[str, str],
    no_cache: bool,
) -> tuple[list[tuple[bytes, Path]], list[str]] | None:
    """
    Input:  changed, source, disc_origin, session_n - changed paths and prior sessions
            backend, crypto, dry_manifests, disc_state, no_cache - prior blob context
    Output: eligible delta jobs and paths, or None on a prior-blob read failure
    """
    import hashlib

    needed = [
        (rel_path, disc_origin.get(rel_path, session_n - 1))
        for rel_path in sorted(changed)
    ]
    delta_jobs: list[tuple[bytes, Path]] = []
    delta_paths: list[str] = []
    with _prior_blob_cache(CacheManager(), no_cache) as cache:
        if needed:
            try:
                cache.prefetch_origin_blobs(
                    needed, backend, manifests=dry_manifests, crypto=crypto,
                )
            except Exception as exc:
                _log.warning("dry-run: cannot read prior blobs: %s", exc)
                print(f"error: Cannot read a prior blob: {exc}", file=sys.stderr)
                return None

        for rel_path in sorted(changed):
            abs_path = source / rel_path
            try:
                encrypted_old = cache.get_with_fallback(
                    rel_path, disc_origin.get(rel_path, session_n - 1),
                    backend, crypto=crypto, manifests=dry_manifests,
                )
                prior_entry = next(
                    (entry for m in dry_manifests or []
                     if m.session == disc_origin.get(rel_path, session_n - 1)
                     for entry in m.entries if entry.path == rel_path),
                    None,
                )
                if is_bound_ciphertext(encrypted_old) and prior_entry is None:
                    raise ValueError(f"Cannot resolve authenticated blob for {rel_path!r}.")
                old_bytes = crypto.decrypt(
                    encrypted_old,
                    aad=blob_aad(prior_entry) if is_bound_ciphertext(encrypted_old) else None,
                )
                if hashlib.sha256(old_bytes).hexdigest() != disc_state[rel_path]:
                    _log.warning(
                        "cached prior version did not match for %s. Dry run uses a full blob",
                        rel_path,
                    )
                    continue
                delta_jobs.append((old_bytes, abs_path))
                delta_paths.append(rel_path)
            except Exception as exc:
                _log.warning("dry-run: cannot read prior blob for %s: %s", rel_path, exc)
                print(
                    f"error: Cannot read the prior blob for {safe_display(rel_path)}: {exc}",
                    file=sys.stderr,
                )
                return None
    return delta_jobs, delta_paths


def _print_dry_changed(
    changed: set[str],
    source: Path,
    delta_results: dict[str, tuple[str, bytes]],
) -> tuple[int, int]:
    """
    Input:  changed, source, delta_results - changed paths and their computed deltas
    Output: total bytes and number of changed files to write
    """
    total_session_bytes = 0
    entry_count = 0
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
    return total_session_bytes, entry_count


def _print_dry_space(total_session_bytes: int, remaining_bytes: int) -> None:
    """
    Input:  total_session_bytes - estimated bytes in this session
            remaining_bytes - bytes remaining on the disc
    Output: prints the space-check result
    """
    from oddarchiver.session import SPACE_SAFETY_MARGIN

    limit = remaining_bytes * SPACE_SAFETY_MARGIN
    if remaining_bytes > 0 and total_session_bytes < limit:
        pct = 100.0 * total_session_bytes / remaining_bytes
        print(f"Space check:                OK (session is {pct:.2f}% of remaining)")
    elif remaining_bytes > 0:
        pct = 100.0 * total_session_bytes / remaining_bytes
        print(
            f"Space check:                OVERAGE "
            f"({_fmt_bytes(total_session_bytes)} is {pct:.1f}% of remaining -- would not fit)"
        )
    else:
        print("Space check:                OVERAGE (disc full)")


def _dry_run_changes(
    args: argparse.Namespace,
    source: Path,
    current_state: dict[str, str],
    disc_state: dict[str, str],
    disc_origin: dict[str, int],
    backend: BurnBackend,
    crypto: CryptoBackend,
    manifests: list[Manifest] | None,
) -> tuple[set[str], set[str], int]:
    """
    Input:  args, source, current_state, disc_state - files to compare
            disc_origin, backend, crypto, manifests - skip-large context
    Output: changed and new paths, plus the pre-filter unchanged count
    """
    changed = {p for p in current_state if p in disc_state and current_state[p] != disc_state[p]}
    new_files = {p for p in current_state if p not in disc_state}
    unchanged_count = len(current_state) - len(changed) - len(new_files)

    keep = _apply_skip_large(
        args, new_files | changed, source, current_state, backend, crypto,
        disc_state, disc_origin, manifests,
    )
    changed &= keep
    new_files &= keep
    return changed, new_files, unchanged_count


def _dry_new_files(new_files: set[str], source: Path) -> tuple[int, int]:
    """
    Input:  new_files, source - new source-relative paths and their root
    Output: total bytes and number of new files to write
    """
    total_bytes = 0
    entry_count = 0
    for rel_path in sorted(new_files):
        abs_path = source / rel_path
        file_size = abs_path.stat().st_size
        print(f"  [full]   {rel_path:<40} {_fmt_bytes(file_size)}  (new file)")
        total_bytes += file_size
        entry_count += 1
    return total_bytes, entry_count


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
    from oddarchiver.delta import process_files

    print("DRY RUN -- no disc will be written")
    print()

    backend = _make_backend(args)
    disc_info = backend.mediainfo()

    state = _dry_run_disc_state(args, backend, disc_info, is_init)
    if state is None:
        return 0 if is_init else 1
    disc_state, disc_origin, manifests, label, session_n, crypto = state

    source = Path(args.source)

    current_state, total_source_bytes = _scan_dry_source(source)

    changed, new_files, unchanged_count = _dry_run_changes(
        args, source, current_state, disc_state, disc_origin, backend, crypto, manifests,
    )

    print(f"  Disc state: {safe_display(label)}, next session {session_n}")
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

    dry_manifests = manifests if not is_init else None
    jobs = _dry_prior_jobs(
        changed, source, disc_origin, session_n, backend, crypto,
        dry_manifests, disc_state, getattr(args, "no_cache", False),
    )
    if jobs is None:
        return 1
    delta_jobs, delta_paths = jobs

    delta_results: dict[str, tuple[str, bytes]] = dict(zip(delta_paths, process_files(delta_jobs))) if delta_jobs else {}

    total_session_bytes, entry_count = _print_dry_changed(changed, source, delta_results)

    new_bytes, new_count = _dry_new_files(new_files, source)
    total_session_bytes += new_bytes
    entry_count += new_count

    print()
    print(f"  {entry_count} files to write, {unchanged_count} unchanged")
    print()
    print(f"Session size (unencrypted): {_fmt_bytes(total_session_bytes)}")
    print(f"Disc remaining:             {_fmt_bytes(disc_info.remaining_bytes)}")

    _print_dry_space(total_session_bytes, disc_info.remaining_bytes)

    print()
    print(f"Would burn as: session_{session_n:03d} on {safe_display(label)}")
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
            f"Disc {safe_display(label)} is full ({used}/{total}). "
            f"Continuing on {next_path} for volume {next_volume_index}.",
            flush=True,
        )
        next_backend: "BurnBackend" = ISOBackend(next_path, disc_size=iso_disc_size)
    else:
        prompt = (
            f"Disc {safe_display(label)} is full ({used}/{total}). "
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
    next_backend.invalidate_session_list()
    new_info = next_backend.mediainfo()
    if new_info.session_count != 0:
        raise RuntimeError(
            f"Newly inserted disc is not blank "
            f"(session_count={new_info.session_count}); "
            "refusing to overwrite an existing volume"
        )
    return next_backend


def _burn_primary(
    command: str, staging: Path, session_n: int, backend: "BurnBackend",
    label: str, crypto: "CryptoBackend", verify_mod: object, use_init: bool,
    expected_session_count: int, level: str, interrupt_msg: str,
) -> bool:
    """
    Input:  command, staging, session_n, backend, label, crypto, verify_mod,
            use_init, expected_session_count, level, interrupt_msg - primary burn context
    Output: True on burn success; False after an interrupted burn
    """
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
        return False
    return True


def _burn_mirror(
    command: str, staging: Path, session_n: int, mirror_backend: "BurnBackend",
    label: str, use_init: bool, expected_session_count: int,
) -> bool:
    """
    Input:  command, staging, session_n, mirror_backend, label, use_init,
            expected_session_count - mirror burn context
    Output: True on mirror success; False on burn failure
    """
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
        return False
    return True


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
        A cache-update OSError is logged and does not fail the run.

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
    if not _burn_primary(
        command, staging, session_n, backend, label, crypto, verify_mod,
        use_init, expected_session_count, level, interrupt_msg,
    ):
        return 1
    _log.info("%s: burn complete", command)
    print("  Burn complete.", flush=True)
    if mirror_backend is not None and not _burn_mirror(
        command, staging, session_n, mirror_backend, label, use_init,
        expected_session_count,
    ):
        return 1
    try:
        print(start_msg, flush=True)
        verify_mod.verify(backend, crypto, level=level, manifests=[manifest])
        _log.info("%s: post-burn verify OK", command)
        print(ok_msg, flush=True)
    except SystemExit:
        _log.exception("%s: post-burn verify FAILED; cache not updated", command)
        print("error: post-burn verify failed; cache not updated.", file=sys.stderr)
        return 1
    local_count = (session_n - marker_first_session) + 1
    record_confirmed(label, _backend_id(backend), local_count)
    print("Updating cache...", flush=True)
    try:
        _update_cache(cache, staging, session_n, manifest.entries, crypto)
    except OSError:
        _log.exception(
            "cache update failed after successful burn and verify for session %d",
            session_n,
        )
    _log.info(
        "%s: session %03d complete - %d file(s) archived",
        command, session_n, len(manifest.entries),
    )
    return 0


def _print_rescue_code_once(crypto: "CryptoBackend") -> None:
    """
    Input:  crypto - init crypto backend that may carry _rescue_code
    Output: None (prints to stdout)
    Details:
        Prints the rescue banner after the first verified init burn, then
        clears crypto._rescue_code so the code prints only once. Never
        logs the code.
    """
    rescue_code = getattr(crypto, "_rescue_code", None)
    if rescue_code:
        print(
            "\n"
            "RESCUE KEY -- store this offline. It is never written to the\n"
            "archive, this machine, or any log. If you lose the primary secret\n"
            "and this key, the archive is unrecoverable.\n"
            f"\n{rescue_code}\n",
            flush=True,
        )
        crypto._rescue_code = None


def _write_chunk_marker(
    staging: Path, session_n: int, i: int, is_new_volume: bool,
    write_chunk0_marker: bool, archive_id: str, base_volume_index: int,
    new_volumes_swapped: int, chunk0_first_session: int,
    previous_volume_label: str, pending_baseline: object | None,
    crypto: "CryptoBackend",
) -> None:
    """
    Input:  staging and session_n - chunk location and archive session
            i and is_new_volume - chunk index and volume state
            write_chunk0_marker - whether to write the first marker
            archive_id and base_volume_index - volume identity
            new_volumes_swapped - number of completed swaps
            chunk0_first_session - first session on the initial volume
            previous_volume_label - label from the previous volume
            pending_baseline - baseline to write on a new volume
            crypto - backend for the baseline
    Output: None; writes a marker and optional baseline to staging
    """
    from oddarchiver.volume import VolumeMarker, write_volume_baseline, write_volume_marker

    if i == 0 and write_chunk0_marker:
        write_volume_marker(
            staging,
            VolumeMarker(
                archive_id=archive_id,
                volume_index=base_volume_index,
                first_session=chunk0_first_session,
                created_at=datetime.now(timezone.utc).isoformat(),
                previous_volume_label="",
                format=2,
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
                format=2,
            ),
        )
        if pending_baseline is not None:
            write_volume_baseline(
                staging, pending_baseline, crypto,
                marker=VolumeMarker(
                    archive_id=archive_id, volume_index=vol_index,
                    first_session=session_n, created_at="",
                    previous_volume_label=previous_volume_label,
                ),
            )


def _chunk_burn_options(
    is_new_volume: bool, session_n: int, chunk0_first_session: int,
    chunk0_use_init: bool, chunk0_expected_session_count: int,
    mirror_backend: "BurnBackend | None", expected_archive_id: str | None,
    expected_volume_index: int | None,
) -> tuple[int, bool, int, "BurnBackend | None", str | None, int | None]:
    """
    Input:  is_new_volume and session_n - current chunk's volume and session
            chunk0_first_session, chunk0_use_init, chunk0_expected_session_count
              - initial volume burn options
            mirror_backend - optional mirror on the initial volume
            expected_archive_id and expected_volume_index - append identity
    Output: first session, init mode, expected count, mirror, and append identity
    """
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
    return marker_first_session, use_init, expected, chunk_mirror, burn_archive_id, burn_volume_index


def _prepare_next_volume(
    backend: "BurnBackend", crypto: "CryptoBackend", label: str,
    base_volume_index: int, new_volumes_swapped: int,
    iso_origin: Path | None, iso_disc_size: int | None,
) -> tuple["BurnBackend | None", object | None, str, bool]:
    """
    Input:  backend and crypto - current volume and baseline reader
            label - fallback label for the current volume
            base_volume_index and new_volumes_swapped - next volume index
            iso_origin and iso_disc_size - optional ISO destination settings
    Output: next backend, baseline, previous label, and swap success
    """
    from oddarchiver.volume import (
        build_volume_baseline, read_volume_baseline, read_volume_marker,
    )

    info = backend.mediainfo()
    previous_volume_label = info.label or label
    old_marker = read_volume_marker(backend)
    old_manifests = _read_disc_manifests(
        backend, crypto=crypto, marker=old_marker,
    )
    try:
        old_seed = read_volume_baseline(
            backend, crypto, marker=old_marker,
            manifests=old_manifests,
        )
    except BaselineReadError as exc:
        _log.exception(
            "Cannot read the volume baseline before the disc swap"
        )
        print(f"error: {exc}", file=sys.stderr)
        print(
            "Earlier chunks are burned. Run sync to resume.",
            file=sys.stderr,
        )
        return None, None, previous_volume_label, False
    pending_baseline = build_volume_baseline(
        old_seed, old_manifests, old_marker,
        previous_volume_label,
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
        return None, pending_baseline, previous_volume_label, False
    return swapped, pending_baseline, previous_volume_label, True


def _volume_iso_origin(
    backend: "BurnBackend", base_volume_index: int,
) -> tuple[Path | None, int | None]:
    """
    Input:  backend - initial burn backend
            base_volume_index - index of the initial medium
    Output: original ISO path and disc size, or two None values
    """
    if not isinstance(backend, ISOBackend):
        return None, None
    iso_origin = backend.iso_path
    if base_volume_index > 0:
        volume_suffix = f".vol{base_volume_index}"
        if iso_origin.stem.endswith(volume_suffix):
            iso_origin = iso_origin.with_name(
                iso_origin.stem[:-len(volume_suffix)] + iso_origin.suffix
            )
    return iso_origin, backend.disc_size


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
    from oddarchiver.volume import VolumeBaseline
    from oddarchiver.session import remove_tree

    iso_origin, iso_disc_size = _volume_iso_origin(backend, base_volume_index)

    previous_volume_label = ""
    new_volumes_swapped = 0
    pending_baseline: VolumeBaseline | None = None
    try:
        if len(chunks) > 1 and mirror_backend is not None:
            raise RuntimeError(
                "multi-volume disc-swap combined with --mirror is not yet supported"
            )
        for i, (staging, session_n, is_new_volume) in enumerate(chunks):
            _write_chunk_marker(
                staging, session_n, i, is_new_volume, write_chunk0_marker,
                archive_id, base_volume_index, new_volumes_swapped,
                chunk0_first_session, previous_volume_label, pending_baseline,
                crypto,
            )

            (
                marker_first_session, use_init, expected, chunk_mirror,
                burn_archive_id, burn_volume_index,
            ) = _chunk_burn_options(
                is_new_volume, session_n, chunk0_first_session, chunk0_use_init,
                chunk0_expected_session_count, mirror_backend,
                expected_archive_id, expected_volume_index,
            )

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
            if command == "init":
                _print_rescue_code_once(crypto)


            if i < len(chunks) - 1:
                swapped, pending_baseline, previous_volume_label, swap_ok = _prepare_next_volume(
                    backend, crypto, label, base_volume_index, new_volumes_swapped,
                    iso_origin, iso_disc_size,
                )
                if not swap_ok:
                    return 1, backend
                backend = swapped
                new_volumes_swapped += 1
        return 0, backend
    finally:
        for staging, _session_n, _is_new in chunks:
            remove_tree(staging, "burn staging directory")
            remove_tree(
                staging.with_name(staging.name + ".cache_full"),
                "delta cache sidecar",
            )


def _mask_current_paths(
    current_state: dict[str, str], disc_state: dict[str, str],
    group_paths: set[str],
) -> dict[str, str]:
    """
    Input:  current_state - current file checksums
            disc_state - checksums before this run
            group_paths - paths assigned to this group
    Output: masked state without new in-group paths
    """
    masked: dict[str, str] = {}
    for path in current_state:
        if path in group_paths:
            if path in disc_state:
                masked[path] = disc_state[path]
            # else: genuinely new, leave absent so build_staging stages it
        else:
            masked[path] = current_state[path]
    return masked


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
    masked = _mask_current_paths(current_state, disc_state, group_paths)
    if group_index == 0:
        for path, checksum in disc_state.items():
            if path not in current_state:
                masked[path] = checksum
    return masked


def _scan_source_with_progress(source: Path) -> dict[str, str]:
    """
    Input:  source - source directory to scan
    Output: mapping of source-relative path to SHA-256 hex digest
    Details:
        Walks the source in sorted order, hashes every file, and prints
        a per-file progress bar. Prints a final newline when any file exists.
    """
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
    return current_state


def _check_init_ledger(args: argparse.Namespace, backend: "BurnBackend", init_label: str) -> None:
    """
    Input:  args - parsed init namespace; backend - target disc; init_label - disc label
    Output: None
    """
    from oddarchiver.ledger import load_ledger

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


def _stage_init_group(
    args: argparse.Namespace, session_mod: object, group_index: int, group_paths: object,
    current_state: dict[str, str], disc_state: dict[str, str],
    disc_origin: dict[str, int], backend: "BurnBackend", crypto: "CryptoBackend",
    source: Path, cache: CacheManager, session_n: int, init_label: str,
) -> list[tuple[Path, int, bool]]:
    """
    Input:  args - init namespace; session_mod - staging module; group_index and
            group_paths - current group; current_state, disc_state, disc_origin -
            source and disc mappings; backend, crypto, source, cache - staging
            resources; session_n - next session; init_label - volume label
    Output: staged chunks for the current group
    """
    group_baseline = None
    group_marker = None
    if group_index:
        from oddarchiver.volume import read_volume_baseline, read_volume_marker
        group_marker = read_volume_marker(backend)
        group_manifests = _read_disc_manifests(
            backend, crypto=crypto, marker=group_marker,
        )
        group_baseline = read_volume_baseline(
            backend, crypto, marker=group_marker, manifests=group_manifests,
        )
    else:
        group_manifests = None
    masked_disc_state = _mask_diff_to_group(
        current_state, disc_state, set(group_paths), group_index,
    )
    return session_mod.build_staging(
        session_n=session_n,
        source=source,
        disc_state=masked_disc_state,
        disc_origin=disc_origin,
        backend=backend,
        cache=cache,
        crypto=crypto,
        _current_state=current_state,
        embed_software=getattr(args, "embed_software", None),
        manifests=group_manifests,
        volume_baseline=group_baseline,
        volume_marker=group_marker,
        volume_label=init_label,
    )


def _init_group_burn_options(
    group_index: int, session_n: int, archive_id: str,
) -> tuple[bool, bool, int, str | None, int | None]:
    """
    Input:  group_index - current group; session_n - next logical session;
            archive_id - initialized archive identifier
    Output: marker flag, init flag, expected session count, archive id, volume index
    """
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
    return (
        write_chunk0_marker, chunk0_use_init, chunk0_expected_session_count,
        group_expected_id, group_expected_vol,
    )


def _advance_init_volume(
    backend: "BurnBackend", base_volume_index: int, chunk0_first_session: int,
) -> tuple[int, int]:
    """
    Input:  backend - active disc; base_volume_index - prior volume index;
            chunk0_first_session - prior first session
    Output: volume index and first session from the active marker, or prior values
    """
    from oddarchiver.volume import read_volume_marker
    active_marker = read_volume_marker(backend)
    if active_marker is not None:
        base_volume_index = active_marker.volume_index
        chunk0_first_session = active_marker.first_session
    return base_volume_index, chunk0_first_session


def _announce_init_groups(groups: list, stage_paths: set[str], source: Path, session_mod: object) -> None:
    """
    Input:  groups - staging partitions; stage_paths - source paths;
            source - source root; session_mod - staging module
    Output: None
    """
    if len(groups) > 1:
        total_needed = sum((source / p).stat().st_size for p in stage_paths)
        staging_free = shutil.disk_usage(session_mod._default_staging_root()).free
        print(
            f"Source needs ~{_fmt_bytes(total_needed)}; staging has "
            f"~{_fmt_bytes(staging_free)} free. Splitting into {len(groups)} "
            "automatic passes.",
            flush=True,
        )


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

        After the first verified encrypted burn, prints the rescue code once
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

    init_label = getattr(args, "label", "ARCHIVE")
    _check_init_ledger(args, backend, init_label)

    mirror_backend = _make_mirror_backend(args)

    if getattr(args, "prefill", None):
        if isinstance(backend, ISOBackend):
            backend.prefill(parse_disc_size(args.prefill))

    crypto = _make_init_crypto(args)
    enc_block = _encryption_block(crypto)
    cache = CacheManager()
    source = Path(args.source)

    # This pass needs a full path set and sizes before any staging directory
    # exists, so it cannot delegate the scan to build_staging as before.
    current_state = _scan_source_with_progress(source)

    new_files = set(current_state)
    print(f"  {len(new_files)} new, 0 changed, 0 deleted, 0 unchanged", flush=True)

    disc_state: dict[str, str] = {}
    disc_origin: dict[str, int] = {}

    stage_paths = _apply_skip_large(
        args, new_files, source, current_state, backend, crypto,
        disc_state, disc_origin,
    )
    groups = session_mod.partition_by_staging_budget(stage_paths, source)
    _announce_init_groups(groups, stage_paths, source, session_mod)

    archive_id = uuid.uuid4().hex
    # Record the mode before the first burn. An init that stops after
    # volume 0 still leaves this host able to run later commands
    # without a terminal confirmation.
    remember_encryption_mode(archive_id, _crypto_mode_name(crypto) or "none")
    base_volume_index = 0
    chunk0_first_session = 0
    session_n = 0
    rc = 0
    for group_index, group_paths in enumerate(groups):
        chunks = _stage_init_group(
            args, session_mod, group_index, group_paths, current_state,
            disc_state, disc_origin, backend, crypto, source, cache, session_n,
            init_label,
        )
        (
            write_chunk0_marker, chunk0_use_init, chunk0_expected_session_count,
            group_expected_id, group_expected_vol,
        ) = _init_group_burn_options(group_index, session_n, archive_id)

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
        if group_index < len(groups) - 1:
            base_volume_index, chunk0_first_session = _advance_init_volume(
                backend, base_volume_index, chunk0_first_session,
            )

    return rc


def _load_sync_disc(
    backend: "BurnBackend",
    args: argparse.Namespace,
    disc_info: object,
) -> "tuple[CryptoBackend, list[Manifest], object, object] | None":
    """
    Input:  backend   - burn backend for the inserted disc
            args      - parsed sync namespace; reads key
            disc_info - backend.mediainfo() result
    Output: (crypto, manifests, marker, baseline), or None when a format-2
            volume has no baseline
    Details:
        Warns and continues for an old disc with no volume baseline.
        Raises ValueError when plaintext would go to an encrypted archive.
    """
    from oddarchiver.volume import read_volume_baseline
    crypto, manifests, raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )
    marker = getattr(raw, "marker", None)
    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=disc_info.session_count,
        command="sync",
    )
    baseline = read_volume_baseline(
        backend, crypto, marker=marker, manifests=manifests,
    )
    if marker is not None and marker.volume_index > 0 and baseline is None:
        if marker.format >= 2:
            print(
                f"error: Disc volume {marker.volume_index} has format 2 and no "
                "volume baseline.",
                file=sys.stderr,
            )
            return None
        message = (
            "Warning: this old disc has no volume baseline. "
            "The program continues."
        )
        _log.warning(message)
        print(message, file=sys.stderr)
    if isinstance(crypto, NullCrypto):
        catalog_encrypted = bool(
            # scbc ignore[is-true-false-strict] VolumeRecord.encrypted is bool | None; inspect only confirmed True, not an unknown value.
            baseline and any(vol.encrypted is True for vol in baseline.volumes)
        )
        memory_mode = (
            remembered_encryption_mode(marker.archive_id) if marker else None
        )
        if catalog_encrypted or memory_mode in ("passphrase", "keyfile"):
            raise ValueError(
                "This archive is encrypted. The program will not write "
                "plaintext to this disc."
            )
    return crypto, manifests, marker, baseline


def _diff_sync_source(
    current_state: dict[str, str],
    disc_state: dict[str, str],
) -> tuple[set[str], list[str]] | None:
    """
    Input:  current_state - source path to checksum
            disc_state    - disc path to checksum
    Output: (new and changed paths, deleted paths), or None when nothing changed
    Details:
        Always prints the diff summary. Prints "All files up to date."
        when nothing changed.
    """
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
        return None
    return new_files | changed, deleted_files


def _sync_changes(
    args: argparse.Namespace,
    source: Path,
    paths: set[str],
    deleted_files: list[str],
    current_state: dict[str, str],
    disc_state: dict[str, str],
    disc_origin: dict[str, int],
    backend: "BurnBackend",
    crypto: "CryptoBackend",
    manifests: list[Manifest],
    session_mod: object,
) -> list[set[str]] | None:
    """
    Input:  args          - parsed sync namespace; reads skip_large
            source        - source directory
            paths         - new and changed source-relative paths
            deleted_files - disc paths missing from the source
            current_state, disc_state, disc_origin, backend, crypto,
            manifests     - run context for _apply_skip_large
            session_mod   - oddarchiver.session module
    Output: staging-budget groups, or None when --skip-large leaves nothing
    Details:
        Prints the automatic-passes notice when the changeset needs more
        than one group.
    """
    changeset = _apply_skip_large(
        args, paths, source, current_state, backend, crypto,
        disc_state, disc_origin, manifests,
    )
    if not changeset and not deleted_files:
        print("Nothing left to burn after --skip-large.", flush=True)
        return None
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
    return groups


def _stage_sync_group(
    args: argparse.Namespace,
    session_mod: object,
    group_paths: object,
    group_index: int,
    current_state: dict[str, str],
    disc_state: dict[str, str],
    disc_origin: dict[str, int],
    source: Path,
    backend: "BurnBackend",
    cache: CacheManager,
    crypto: "CryptoBackend",
    manifests: list[Manifest],
    baseline: object,
    marker: object,
    chunk0_first_session: int,
    session_n: int,
    label: str,
) -> list[tuple[Path, int, bool]]:
    """
    Input:  args, session_mod, group_paths, group_index and the sync run state
    Output: staged chunks for this group
    Details:
        Masks the diff to this group and stages it. The private read cache
        closes before the caller burns.
    """
    masked_disc_state = _mask_diff_to_group(
        current_state, disc_state, set(group_paths), group_index,
    )
    with _prior_blob_cache(cache, getattr(args, "no_cache", False)) as read_cache:
        return session_mod.build_staging(
            session_n=session_n,
            source=source,
            disc_state=masked_disc_state,
            disc_origin=disc_origin,
            backend=backend,
            cache=read_cache,
            crypto=crypto,
            _current_state=current_state,
            manifests=manifests,
            volume_first_session=chunk0_first_session,
            embed_software=getattr(args, "embed_software", None),
            volume_baseline=baseline,
            volume_marker=marker,
            volume_label=label,
        )


def _burn_sync_group(
    chunks: list[tuple[Path, int, bool]],
    backend: "BurnBackend",
    args: argparse.Namespace,
    label: str,
    enc_block: dict,
    crypto: "CryptoBackend",
    cache: CacheManager,
    verify_mod: object,
    mirror_backend: "BurnBackend | None",
    archive_id: str,
    base_volume_index: int,
    chunk0_first_session: int,
    session_n: int,
    expected_archive_id: str | None,
    expected_volume_index: int | None,
) -> "tuple[int, BurnBackend]":
    """
    Input:  chunks and the sync run state for one group
    Output: (exit code, active backend after any media swap)
    """
    # session_n is first_session + len(manifests), the same quantity
    # _guard re-reads. MMC session_count is not: BD-R SRM+POW
    # reports 1 after every append.
    chunk0_expected_session_count = session_n
    return _burn_volume_chunks(
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


def _refresh_sync_volume(
    backend: "BurnBackend",
    crypto: "CryptoBackend",
) -> tuple[object, list[Manifest], object]:
    """
    Input:  backend - active backend after a group burn
            crypto  - archive crypto backend
    Output: (marker, manifests, baseline) read in that order
    """
    from oddarchiver.volume import read_volume_baseline, read_volume_marker
    marker = read_volume_marker(backend)
    manifests = _read_disc_manifests(
        backend, crypto=crypto, marker=marker,
    )
    baseline = read_volume_baseline(
        backend, crypto, marker=marker, manifests=manifests,
    )
    return marker, manifests, baseline


def _sync_volume_identity(
    marker: object,
) -> tuple[str, int, int, str | None, int | None]:
    """
    Input:  marker - VolumeMarker of the inserted disc, or None
    Output: (archive_id, base_volume_index, chunk0_first_session,
             expected_archive_id, expected_volume_index)
    Details:
        A missing marker gets a fresh archive id, volume 0, session 0,
        and no expected identity.
    """
    archive_id = marker.archive_id if marker else uuid.uuid4().hex
    base_volume_index = marker.volume_index if marker else 0
    chunk0_first_session = marker.first_session if marker else 0
    expected_archive_id = marker.archive_id if marker else None
    expected_volume_index = marker.volume_index if marker else None
    return (
        archive_id, base_volume_index, chunk0_first_session,
        expected_archive_id, expected_volume_index,
    )


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
    _require_iso_exists(backend)
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

    loaded = _load_sync_disc(backend, args, disc_info)
    if loaded is None:
        return 1
    crypto, manifests, marker, baseline = loaded
    disc_state = build_disc_state(
        manifests, seed=baseline.state if baseline else None,
    )
    disc_origin = build_disc_origin(
        manifests, seed=baseline.origin if baseline else None,
    )
    enc_block = _encryption_block(crypto)
    label = manifests[0].label if manifests else "ARCHIVE"

    # Use marker.first_session + manifest count, not dvd+rw-mediainfo
    # session count. BD-R SRM+POW always reports session_count=1
    # regardless of how many appends occurred. A missing marker
    # (legacy/single-volume) treats first_session as 0.
    session_n = (marker.first_session if marker else 0) + len(manifests)
    source = Path(args.source)

    # Scan source with progress bar, compute diff, always show summary.
    current_state = _scan_source_with_progress(source)
    diff = _diff_sync_source(current_state, disc_state)
    if diff is None:
        return 0
    paths, deleted_files = diff

    cache = CacheManager()
    groups = _sync_changes(
        args, source, paths, deleted_files, current_state, disc_state,
        disc_origin, backend, crypto, manifests, session_mod,
    )
    if groups is None:
        return 0

    (
        archive_id, base_volume_index, chunk0_first_session,
        expected_archive_id, expected_volume_index,
    ) = _sync_volume_identity(marker)
    rc = 0
    for group_index, group_paths in enumerate(groups):
        chunks = _stage_sync_group(
            args, session_mod, group_paths, group_index, current_state,
            disc_state, disc_origin, source, backend, cache, crypto,
            manifests, baseline, marker, chunk0_first_session, session_n,
            label,
        )
        rc, backend = _burn_sync_group(
            chunks, backend, args, label, enc_block, crypto, cache,
            verify_mod, mirror_backend, archive_id, base_volume_index,
            chunk0_first_session, session_n, expected_archive_id,
            expected_volume_index,
        )
        if rc != 0:
            return rc
        session_n += len(chunks)
        if group_index < len(groups) - 1:
            marker, manifests, baseline = _refresh_sync_volume(backend, crypto)
            if marker is not None:
                base_volume_index = marker.volume_index
                chunk0_first_session = marker.first_session
                expected_archive_id = marker.archive_id
                expected_volume_index = marker.volume_index
    return rc


def _print_catalog_notices(catalog, marker, higher: int | None) -> None:
    """
    Input: catalog, inserted disc marker and last known disc index
    Output: catalog notices on stderr
    """
    source = disc_name(catalog, catalog.source_volume_index)
    print(
        f"Catalog from {source}, sessions through {catalog.last_session}. "
        "If you have a newer disc, insert it and run this command again.",
        file=sys.stderr,
    )
    if marker and higher is not None and higher > marker.volume_index:
        print(
            f"Warning: You saw {disc_name(catalog, higher)} after this disc. "
            "Insert the newer disc for a newer catalog.",
            file=sys.stderr,
        )
    if not catalog.complete:
        print(
            "The disc names for some older sessions are not known. "
            "Insert each older disc one time to record its name.",
            file=sys.stderr,
        )


def _restore_from_catalog(
    args: argparse.Namespace, backend: BurnBackend, crypto: CryptoBackend,
    manifests: list[Manifest], marker: object, disc_info: object, options: dict,
) -> object | None:
    """
    Input:  restore arguments, loaded disc data, and restore options
    Output: restore result, or None if catalog building failed
    """
    from oddarchiver.restore_plan import DiscAcquisition, restore_archive

    try:
        catalog = build_archive_catalog(
            backend, crypto, manifests, marker, label=disc_info.label,
        )
    except BaselineReadError as exc:
        _log.warning("Cannot build restore catalog: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return None
    higher = highest_seen_volume(marker.archive_id)
    remember_discs(catalog)
    _print_catalog_notices(catalog, marker, higher)
    return restore_archive(
        **options, catalog=catalog,
        unskip_volumes=getattr(args, "unskip_volumes", None),
        acquisition=DiscAcquisition(
            backend, image=getattr(args, "test_iso", None),
        ),
    )


def _restore_single_disc(
    args: argparse.Namespace, backend: BurnBackend, crypto: CryptoBackend,
    manifests: list[Manifest], marker: object, options: dict,
) -> object:
    """
    Input:  restore arguments, loaded disc data, and mutable restore options
    Output: single-disc restore result
    """
    from oddarchiver.restore import restore

    if marker and getattr(args, "unskip_volumes", None):
        from oddarchiver.restore_state import load_restore_state, write_restore_state

        state = load_restore_state(Path(args.dest), marker.archive_id)
        if state is not None:
            state.missing_discs = sorted(
                set(state.missing_discs) - set(args.unskip_volumes)
            )
            write_restore_state(Path(args.dest), state)
    if marker:
        print(
            "Note: This restore uses this disc only. Other discs can hold "
            "newer versions. Use --all-disks to restore the newest version "
            "of each file.",
            file=sys.stderr,
        )
    options["read_mode"] = options["read_mode"] or DEFAULT_READ_MODE
    return restore(**options)


def _print_restore_result(result: object) -> None:
    """
    Input:  restore result with stale, missing, and unresolved paths
    Output: None; prints path reports to stdout or stderr
    """
    if result.stale or result.missing:
        if result.stale:
            print("  stale:", flush=True)
            for rel_path, session_n in result.stale:
                print(
                    f"    {safe_display(rel_path)}  (session {session_n})",
                    flush=True,
                )
        if result.missing:
            print("  missing:", flush=True)
            for rel_path in result.missing:
                print(f"    {safe_display(rel_path)}", flush=True)
    for rel_path, name in result.unresolved:
        print(
            f"  {safe_display(rel_path)}: needs {safe_display(name)}.",
            file=sys.stderr,
        )


def _restore_iso_exists(args: argparse.Namespace) -> bool:
    """
    Input:  restore arguments with optional test ISO
    Output: whether the requested ISO exists, reporting an absent image
    """
    if getattr(args, "test_iso", None):
        iso_path = Path(args.test_iso)
        if not iso_path.exists():
            print(
                f"error: ISO image {iso_path} does not exist.",
                file=sys.stderr,
            )
            return False
    return True


def _run_restore(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed restore namespace
    Output: exit code (0 success, 1 failure, 3 paused)
    Details:
        Restore from one disc or use the catalog to request needed discs.
        Report unavailable and older file versions by path.
    """
    if not _restore_iso_exists(args):
        return 1

    backend = _make_backend(args)
    crypto, manifests, raw = _crypto_and_manifests(
        backend,
        key_path=getattr(args, "key", None),
        rescue_key=getattr(args, "rescue_key", None),
    )
    marker = raw.marker
    disc_info = backend.mediainfo()
    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=disc_info.session_count,
        command="restore",
    )

    all_disks = getattr(args, "all_disks", False)
    options = dict(
        dest=Path(args.dest), backend=backend, crypto=crypto,
        session=getattr(args, "session", None),
        force=getattr(args, "force", False),
        paths=getattr(args, "paths", None),
        skip_volumes=getattr(args, "skip_volumes", None),
        read_mode=getattr(args, "read_mode", None),
        manifests=manifests,
    )
    if all_disks and marker:
        result = _restore_from_catalog(
            args, backend, crypto, manifests, marker, disc_info, options,
        )
        if result is None:
            return 1
    else:
        result = _restore_single_disc(
            args, backend, crypto, manifests, marker, options,
        )
    _print_restore_result(result)
    return 3 if result.paused else 1 if result.failures else 0


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
        enc_mode = safe_display(m.encryption.get("mode", "none") if m.encryption else "none")
        suspect_marker = " [SUSPECT]" if m.suspect else ""
        print(
            f"{m.session:03d}     {safe_display(m.timestamp):<22} {len(m.entries):>6}"
            f" {_fmt_bytes(total_bytes):>10} {enc_mode}{suspect_marker}"
        )

    return 0


def _listing_catalog(
    backend: "BurnBackend", crypto: "CryptoBackend", manifests: list[Manifest],
    marker: object, disc_info: object, all_disks: bool, command: str,
) -> tuple[object | None, bool]:
    """
    Input:  loaded disc data and listing mode
    Output: catalog and whether baseline reading failed
    """
    catalog = None
    if all_disks:
        try:
            catalog = build_archive_catalog(
                backend, crypto, manifests, marker, label=disc_info.label,
            )
        except BaselineReadError as exc:
            _log.warning("Cannot build %s catalog: %s", command, exc)
            print(f"error: {exc}", file=sys.stderr)
            return None, True
        higher = highest_seen_volume(marker.archive_id) if marker else None
        remember_discs(catalog)
        _print_catalog_notices(catalog, marker, higher)
    elif marker:
        remember_inserted_disc(marker, disc_info.label, manifests)
        print(
            "Note: This list shows this disc only. Other discs can hold newer "
            "versions. Use --all-disks to see the full archive.",
            file=sys.stderr,
        )
    return catalog, False


def _run_ls(args: argparse.Namespace) -> int:
    """
    Input:  args - parsed ls namespace
    Output: exit code
    Details:
        Loads manifests the same way as history. Prints a path table.
        No blob reads. --path is a listing scope filter, not repeatable.
    """
    backend = _make_backend(args)
    crypto, manifests, raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )
    marker = raw.marker
    disc_info = backend.mediainfo()
    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=disc_info.session_count,
        command="ls",
    )

    all_disks = getattr(args, "all_disks", False)
    catalog, catalog_failed = _listing_catalog(
        backend, crypto, manifests, marker, disc_info, all_disks, "ls",
    )
    if catalog_failed:
        return 1
    if not manifests and not all_disks:
        print("No sessions found.")
        return 0

    entries = (
        catalog_tree(catalog, at_session=getattr(args, "session", None))
        if all_disks else
        build_disc_tree(manifests, at_session=getattr(args, "session", None))
    )
    _print_ls_entries(_filter_ls_entries(entries, args), all_disks, catalog)
    return 0


def _filter_ls_entries(
    entries: list[TreeEntry], args: argparse.Namespace,
) -> list[TreeEntry]:
    prefix = getattr(args, "path", None)
    if prefix:
        entries = [e for e in entries if _path_matches(e.path, prefix)]
    if not getattr(args, "show_deleted", False):
        entries = [e for e in entries if not e.deleted]
    return entries


def _ls_entry_row(
    entry: TreeEntry, all_disks: bool, catalog: object | None,
) -> str:
    unknown = all_disks and any(
        version.session == entry.origin_session and not version.verified
        for version in catalog.history.get(entry.path, ())
    )
    unknown = unknown or (entry.size == 0 and not entry.last_modified)
    size = "?" if unknown else _fmt_bytes(entry.size)
    timestamp = "" if unknown else entry.last_modified
    row = (
        f"{safe_display(entry.path):<40} {size:>10} "
        f"{entry.origin_session:03d}           {timestamp:<22}"
    )
    if all_disks:
        session = entry.deleted_session if entry.deleted else entry.origin_session
        # scbc ignore[manual-str-join] row is extended at most once per entry, not accumulated across loop iterations.
        row += f" {disc_name(catalog, entry.volume_index, session)}"
    return f"{row}{_deleted_marker(entry)}"

def _print_ls_entries(
    entries: list[TreeEntry], all_disks: bool, catalog: object | None,
) -> None:
    """
    Input:  listing entries and disc context
    Output: printed path table
    """
    header = (
        f"{'Path':<40} {'Size':>10} {'Origin Session':<14} {'Last Modified':<22}"
    )
    if all_disks:
        header += f" {'Disc'}"
    print(header)
    print("-" * len(header))
    for entry in entries:
        print(_ls_entry_row(entry, all_disks, catalog))




def _print_browse_children(
    children: list[tuple[str, bool, TreeEntry | None]], current: str,
    selected: set[str], catalog: object | None, all_disks: bool,
) -> str:
    """
    Input:  current directory entries and listing context
    Output: displayed directory location
    """
    location = f"/{current}" if current else "/"
    print()
    print(f"Browsing {safe_display(location)}  ({len(selected)} selected)")
    if not children:
        print("  (empty)")
    for i, (name, is_dir, entry) in enumerate(children, start=1):
        child_path = f"{current}/{name}" if current else name
        mark = "*" if child_path in selected else " "
        if is_dir:
            print(f"  {mark}{i:>3}  {safe_display(name)}/")
        else:
            extra = _deleted_marker(entry) if entry is not None else ""
            size = _fmt_bytes(entry.size) if entry is not None else ""
            disc = (
                f"  {disc_name(catalog, entry.volume_index, entry.origin_session)}"
                if all_disks and entry is not None else ""
            )
            print(f"  {mark}{i:>3}  {safe_display(name):<32} {size:>10}{disc}{extra}")
    return location


def _browse_navigation(
    raw: str, current: str, selected: set[str],
) -> tuple[bool, str, int | None]:
    """
    Input:  command and current browse state
    Output: whether handled, updated directory, and optional exit code
    """
    if raw in ("q", "quit"):
        return True, current, 0
    if raw in ("h", "help", "?"):
        print(_BROWSE_HELP)
        return True, current, None
    if raw == "..":
        if not current:
            print("Already at archive root.")
        elif "/" in current:
            current = current.rsplit("/", 1)[0]
        else:
            current = ""
        return True, current, None
    if raw == "s":
        if not selected:
            print("Selection is empty.")
        else:
            print("Selected:")
            for p in sorted(selected):
                print(f"  {safe_display(p)}")
        return True, current, None
    return False, current, None


def _browse_restore(
    args: argparse.Namespace, backend: "BurnBackend", crypto: "CryptoBackend",
    manifests: list[Manifest], catalog: object | None, all_disks: bool,
    selected: set[str],
) -> int | None:
    """
    Input:  browse restore context and selected paths
    Output: restore exit code, or None when browsing continues
    """
    from oddarchiver.restore import restore
    from oddarchiver.restore_plan import DiscAcquisition, restore_archive

    if not selected:
        print("Nothing selected. Use a file number or t N to mark paths.")
        return None
    try:
        dest_s = input("Restore to directory: ").strip()
    except EOFError:
        _log.warning("browse: EOF on restore destination; quitting")
        print()
        return 0
    if not dest_s:
        print("No destination given.")
        return None
    if all_disks:
        result = restore_archive(
            dest=Path(dest_s), backend=backend, crypto=crypto,
            catalog=catalog, paths=sorted(selected), manifests=manifests,
            acquisition=DiscAcquisition(
                backend, image=getattr(args, "test_iso", None),
            ),
        )
    else:
        result = restore(
            dest=Path(dest_s), backend=backend, crypto=crypto,
            paths=sorted(selected), manifests=manifests,
        )
    return 3 if result.paused else 1 if result.failures else 0


def _browse_number(
    raw: str, children: list[tuple[str, bool, TreeEntry | None]],
    current: str, selected: set[str],
) -> str:
    """
    Input:  numeric browse command and current directory state
    Output: directory after selection or navigation
    """
    parts = raw.split()
    toggle = False
    idx_s = raw
    if parts[0] in ("t", "m") and len(parts) == 2:
        toggle = True
        idx_s = parts[1]
    if not idx_s.isdecimal():
        print(f"Unknown command {raw!r}. Enter h for help.")
        return current
    if len(idx_s) > 6:
        print("Enter a file number with at most six digits.")
        return current
    idx = int(idx_s)
    if idx < 1 or idx > len(children):
        print("No entry with that number.")
        return current
    name, is_dir, _entry = children[idx - 1]
    child_path = f"{current}/{name}" if current else name
    if toggle or not is_dir:
        if child_path in selected:
            selected.discard(child_path)
            print(f"  unselected {safe_display(child_path)}")
        else:
            selected.add(child_path)
            print(f"  selected {safe_display(child_path)}")
    else:
        current = child_path
    return current


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
    from oddarchiver.restore_plan import DiscAcquisition, restore_archive

    backend = _make_backend(args)
    crypto, manifests, raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )
    marker = raw.marker
    disc_info = backend.mediainfo()
    _check_loaded_manifests(
        manifests,
        backend=backend,
        args=args,
        disc_session_count=disc_info.session_count,
        command="browse",
    )
    if not manifests and not getattr(args, "all_disks", False):
        print("No sessions found.")
        return 0

    all_disks = getattr(args, "all_disks", False)
    catalog, catalog_failed = _listing_catalog(
        backend, crypto, manifests, marker, disc_info, all_disks, "browse",
    )
    if catalog_failed:
        return 1
    show_deleted = getattr(args, "show_deleted", False)
    tree = catalog_tree(catalog) if all_disks else build_disc_tree(manifests)
    current = ""
    selected: set[str] = set()
    print(_BROWSE_HELP)

    while True:
        children = _direct_children(tree, current, show_deleted)
        location = _print_browse_children(
            children, current, selected, catalog, all_disks,
        )
        prompt = f"{safe_display(location)}> "
        try:
            raw = input(prompt)
        except EOFError:
            _log.warning("browse: EOF on input; quitting")
            print()
            return 0
        raw = raw.strip()
        if not raw:
            continue
        handled, current, exit_code = _browse_navigation(raw, current, selected)
        if exit_code is not None:
            return exit_code
        if handled:
            continue
        if raw == "r":
            rc = _browse_restore(
                args, backend, crypto, manifests, catalog, all_disks, selected,
            )
            if rc is not None:
                return rc
            continue
        current = _browse_number(raw, children, current, selected)


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
            read_mode=getattr(args, "read_mode", None) or DEFAULT_READ_MODE,
        )
        return 0
    except SystemExit as exc:
        _log.warning("Verify exited with code %s", exc.code)
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
        results.extend(_manifest_mirror_rows(m, primary_id))
    return results


def _manifest_mirror_rows(
    m: Manifest, primary_id: str,
) -> list[tuple[int, str, bool]]:
    """
    Input:  one session manifest and primary backend identifier
    Output: accessible state for its non-primary mirrors
    """
    results = []
    if len(m.drives) < 2:
        return results
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
        warnings, and any SUSPECT manifests. The session count is the
        number of session manifests on this disc, not the MMC session
        count: BD-R SRM+POW reports 1 MMC session after any number of
        appends.
    """
    backend = _make_backend(args)
    crypto, manifests, raw = _crypto_and_manifests(
        backend, key_path=getattr(args, "key", None),
    )
    disc_info = backend.mediainfo()

    used_pct = (
        100 * disc_info.used_bytes / (disc_info.used_bytes + disc_info.remaining_bytes)
        if (disc_info.used_bytes + disc_info.remaining_bytes) > 0
        else 0
    )

    print(f"Label:     {safe_display(disc_info.label) if disc_info.label else '(none)'}")
    print(f"Sessions:  {len(manifests)}")
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
        control_raw=raw,
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
    _print_status_suspects(manifests)
    _print_status_mirrors(manifests, backend)
    return 0


def _print_status_suspects(manifests: list[Manifest]) -> None:
    """
    Input:  loaded session manifests
    Output: printed suspect rows
    """
    suspects = [m for m in manifests if m.suspect]
    if suspects:
        print(f"\nSUSPECT sessions ({len(suspects)}):")
        for m in suspects:
            suspect(_log, "session_%03d: manifest checksum mismatch", m.session)
            print(f"  session_{m.session:03d}: manifest checksum mismatch")


def _print_status_mirrors(
    manifests: list[Manifest], backend: "BurnBackend",
) -> None:
    """
    Input:  loaded manifests and primary backend
    Output: printed mirror health rows
    """
    mirror_rows = _mirror_health(manifests, _backend_id(backend))
    if mirror_rows:
        print("\nMirror health:")
        for session_n, drive, accessible in mirror_rows:
            status_str = "OK" if accessible else "MISSING"
            if not accessible:
                _log.error("session_%03d: mirror %s is MISSING", session_n, drive)
            print(f"  session_{session_n:03d}: {drive} [{status_str}]")



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
