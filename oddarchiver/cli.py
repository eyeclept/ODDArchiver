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
import sys

# Globals
COMMANDS = ["init", "sync", "restore", "history", "verify", "status"]

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
    p.add_argument("--device", default="/dev/sr0", metavar="DEV")
    p.add_argument("--label", default="ARCHIVE", metavar="LABEL")
    p.add_argument("--encrypt", choices=["none", "passphrase", "keyfile"], default="none")
    p.add_argument("--key", metavar="PATH", help="Keyfile path (keyfile mode only).")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--disc-size", default="25gb", dest="disc_size", metavar="SIZE")
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
    p.add_argument("--device", default="/dev/sr0", metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--no-cache", action="store_true", dest="no_cache")
    p.add_argument("--disc-size", default="25gb", dest="disc_size", metavar="SIZE")
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
    p.add_argument("--device", default="/dev/sr0", metavar="DEV")
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
    p.add_argument("--device", default="/dev/sr0", metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")


def _add_verify(sub: argparse._SubParsersAction) -> None:
    """
    Input:  sub — subparsers action
    Output: None
    Details:
        verify — integrity check at specified depth.
    """
    p = sub.add_parser("verify", help="Check integrity of disc or ISO.")
    p.add_argument("--device", default="/dev/sr0", metavar="DEV")
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
    p.add_argument("--device", default="/dev/sr0", metavar="DEV")
    p.add_argument("--test-iso", metavar="PATH", dest="test_iso")


def _add_dry_iso_mutex(parser: argparse.ArgumentParser) -> None:
    """
    Input:  parser — subcommand parser that already has --dry-run and --test-iso
    Output: None
    Details:
        Registers a post-parse check; argparse mutually_exclusive_group cannot
        span add_argument calls already made, so validation is deferred to
        dispatch().
    """
    pass  # validated in dispatch()


def dispatch(args: argparse.Namespace) -> int:
    """
    Input:  args — parsed namespace from build_parser()
    Output: int — exit code (0 success, 1 error)
    Details:
        Validates cross-flag constraints then routes to the correct handler.
        Returns exit code; does not call sys.exit() directly.
    """
    if getattr(args, "dry_run", False) and getattr(args, "test_iso", None):
        print("error: --dry-run and --test-iso are mutually exclusive.", file=sys.stderr)
        return 1

    # stub dispatch — handlers not yet implemented
    print(f"Command '{args.command}' not yet implemented.", file=sys.stderr)
    return 1


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
