"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for rescue-key generation at init and restore via --rescue-key.
"""
# Imports
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from oddarchiver.cli import _crypto_for_disc, build_parser, dispatch
from oddarchiver.crypto import (
    KeyfileCrypto,
    PassphraseCrypto,
    generate_keyfile,
    generate_rescue_code,
    unwrap_rescue_secret,
    wrap_rescue_secret,
)
from oddarchiver.disc import ISOBackend

# Globals
SMALL_DISC = 50 * 2**20
PASSPHRASE = "rescue-test-passphrase"
logger = logging.getLogger(__name__)
_log = logger


# Functions


@pytest.fixture(autouse=True)
def _isolate_tool_writes(tmp_path, monkeypatch):
    """
    Input:  tmp_path, monkeypatch
    Output: None
    Details:
        Point HOME and the cache root at tmp_path so the "code is never
        written to a file" check can walk one tree.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "oddarchiver.cache.default_cache_dir",
        lambda: tmp_path / "cache",
    )


def _is_rescue_code_line(candidate: str) -> bool:
    """Input: candidate line. Output: whether it is a grouped rescue code."""
    return bool(candidate) and all(ch.isalnum() or ch == "-" for ch in candidate)


def _extract_rescue_code(stdout: str) -> str:
    """
    Input:  stdout - captured init output
    Output: printable rescue code
    Details:
        Finds the first all-base32 grouped line after the RESCUE KEY banner.
    """
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if "RESCUE KEY" not in line:
            continue
        for later in lines[i + 1:]:
            candidate = later.strip()
            if _is_rescue_code_line(candidate):
                return candidate
    raise AssertionError(f"no rescue key printed:\n{stdout}")


def _assert_code_absent(code: str, root: Path) -> None:
    """
    Input:  code - printable rescue code
            root - directory tree to scan
    Output: None
    Details:
        Fails if the grouped or compact form of the code appears in any file.
    """
    compact = code.replace("-", "")
    needles = (
        code.encode("ascii"),
        compact.encode("ascii"),
        code.lower().encode("ascii"),
        compact.lower().encode("ascii"),
    )
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        for needle in needles:
            assert needle not in data, f"rescue code found in {path}"


def _init_encrypted(
    tmp_path: Path,
    monkeypatch,
    capsys,
    mode: str,
    keyfile: Path | None = None,
) -> tuple[Path, Path, str]:
    """
    Input:  tmp_path, monkeypatch, capsys, mode, optional keyfile
    Output: (source, iso, rescue_code)
    Details:
        Runs init for passphrase or keyfile mode and returns the printed code.
    """
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    (source / "file.txt").write_bytes(b"rescue-key payload")
    iso = tmp_path / "archive.iso"
    parser = build_parser()
    argv = [
        "init", str(source),
        "--test-iso", str(iso),
        "--encrypt", mode,
        "--disc-size", "50mb",
        "--label", "RESCUE",
    ]
    if mode == "passphrase":
        monkeypatch.setenv("ODDARCHIVER_PASSPHRASE", PASSPHRASE)
    else:
        assert keyfile is not None
        argv.extend(["--key", str(keyfile)])
    rc = dispatch(parser.parse_args(argv))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    code = _extract_rescue_code(captured.out)
    assert captured.out.count("RESCUE KEY") == 1
    assert captured.out.count(code) == 1
    return source, iso, code


def test_single_volume_keyfile_prints_rescue_once(tmp_path, monkeypatch, capsys):
    """A verified single-volume keyfile init prints one rescue code."""
    keyfile = tmp_path / "archive.key"
    generate_keyfile(str(keyfile))
    _init_encrypted(tmp_path, monkeypatch, capsys, "keyfile", keyfile=keyfile)


def test_first_burn_failure_does_not_print_rescue(tmp_path, monkeypatch, capsys):
    """A failed first burn has no rescue code on a disc to report."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "file.txt").write_bytes(b"payload")
    keyfile = tmp_path / "archive.key"
    generate_keyfile(str(keyfile))
    iso = tmp_path / "archive.iso"

    def fail_init(self, staging, label, expected_session_count=0):
        raise RuntimeError("first burn failed")

    monkeypatch.setattr(ISOBackend, "init", fail_init)
    rc = dispatch(build_parser().parse_args([
        "init", str(source),
        "--test-iso", str(iso),
        "--encrypt", "keyfile",
        "--key", str(keyfile),
        "--disc-size", "50mb",
    ]))
    captured = capsys.readouterr()
    assert rc == 1
    assert "first burn failed" in captured.err
    assert "RESCUE KEY" not in captured.out
    assert not iso.exists()


def test_wrap_unwrap_round_trip():
    """
    Input:  None
    Output: None
    Details:
        scrypt wrap then unwrap recovers the escrowed secret. Grouping and
        case on the printable code do not matter.
    """
    code, raw = generate_rescue_code()
    secret = b"x" * 32
    assert len(secret) == 32
    block = wrap_rescue_secret(raw, secret)
    assert unwrap_rescue_secret(code, block) == secret
    smashed = code.lower().replace("-", " ")
    assert unwrap_rescue_secret(smashed, block) == secret


def test_wrong_rescue_code_raises_like_wrong_passphrase():
    """
    Input:  None
    Output: None
    Details:
        A wrong rescue code raises ValueError, the same type
        PassphraseCrypto.decrypt raises for a wrong passphrase.
    """
    code, raw = generate_rescue_code()
    other, _other_raw = generate_rescue_code()
    assert other != code
    block = wrap_rescue_secret(raw, b"x" * 32)
    with pytest.raises(ValueError):
        unwrap_rescue_secret(other, block)

    right = PassphraseCrypto(passphrase="right")
    wrong = PassphraseCrypto(passphrase="wrong")
    ciphertext = right.encrypt(b"payload")
    with pytest.raises(ValueError):
        wrong.decrypt(ciphertext)


def test_passphrase_init_restore_with_rescue_key(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        init --encrypt passphrase, then restore --rescue-key without the
        original passphrase, reconstructs the source file.
    """
    _source, iso, code = _init_encrypted(
        tmp_path, monkeypatch, capsys, "passphrase",
    )
    monkeypatch.delenv("ODDARCHIVER_PASSPHRASE", raising=False)

    dest = tmp_path / "dest"
    parser = build_parser()
    monkeypatch.setattr("oddarchiver.cli.sys.stdin", io.StringIO(code + "\n"))
    rc = dispatch(parser.parse_args([
        "restore", str(dest),
        "--test-iso", str(iso),
        "--rescue-key",
    ]))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert (dest / "file.txt").read_bytes() == b"rescue-key payload"


def test_keyfile_init_restore_with_rescue_key(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        init --encrypt keyfile, then restore --rescue-key without --key,
        reconstructs the source file.
    """
    keyfile = tmp_path / "archive.key"
    generate_keyfile(str(keyfile))
    _source, iso, code = _init_encrypted(
        tmp_path, monkeypatch, capsys, "keyfile", keyfile=keyfile,
    )
    keyfile.unlink()

    dest = tmp_path / "dest"
    parser = build_parser()
    monkeypatch.setattr("oddarchiver.cli.sys.stdin", io.StringIO(code + "\n"))
    rc = dispatch(parser.parse_args([
        "restore", str(dest),
        "--test-iso", str(iso),
        "--rescue-key",
    ]))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert (dest / "file.txt").read_bytes() == b"rescue-key payload"


def test_encrypt_none_has_no_rescue(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        --encrypt none prints no rescue code and writes no rescue field.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "file.txt").write_bytes(b"plain")
    iso = tmp_path / "archive.iso"
    parser = build_parser()
    rc = dispatch(parser.parse_args([
        "init", str(source),
        "--test-iso", str(iso),
        "--encrypt", "none",
        "--disc-size", "50mb",
    ]))
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "RESCUE KEY" not in captured.out

    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    files = backend.read_paths(["session_000/enc_mode.json"])
    enc_bytes = files.get("session_000/enc_mode.json")
    if enc_bytes is not None:
        data = json.loads(enc_bytes)
        assert "rescue" not in data


def test_wrong_rescue_code_on_archive_raises_value_error(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        _crypto_for_disc with a wrong code raises ValueError, matching
        a wrong-passphrase decrypt. CLI restore exits 1.
    """
    _source, iso, code = _init_encrypted(
        tmp_path, monkeypatch, capsys, "passphrase",
    )
    monkeypatch.delenv("ODDARCHIVER_PASSPHRASE", raising=False)
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    other, _raw = generate_rescue_code()
    assert other != code
    monkeypatch.setattr("oddarchiver.cli.sys.stdin", io.StringIO(other + "\n"))
    with pytest.raises(ValueError, match="authentication error"):
        _crypto_for_disc(backend, rescue_key="")
    monkeypatch.setattr("oddarchiver.cli.sys.stdin", io.StringIO(other + "\n"))

    dest = tmp_path / "dest"
    parser = build_parser()
    rc = dispatch(parser.parse_args([
        "restore", str(dest),
        "--test-iso", str(iso),
        "--rescue-key",
    ]))
    assert rc == 1


def test_missing_rescue_blob_has_specific_message(
    tmp_path, monkeypatch, capsys,
):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        restore --rescue-key against --encrypt none, and against a
        passphrase archive with the rescue field stripped, fails with
        the pre-feature message.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "file.txt").write_bytes(b"plain")
    iso_none = tmp_path / "none.iso"
    parser = build_parser()
    assert dispatch(parser.parse_args([
        "init", str(source),
        "--test-iso", str(iso_none),
        "--encrypt", "none",
        "--disc-size", "50mb",
    ])) == 0
    capsys.readouterr()

    dest = tmp_path / "dest-none"
    rc = dispatch(parser.parse_args([
        "restore", str(dest),
        "--test-iso", str(iso_none),
        "--rescue-key",
    ]))
    captured = capsys.readouterr()
    assert rc == 1
    assert "no rescue key" in captured.err
    assert "predates this feature" in captured.err

    _source, iso, code = _init_encrypted(
        tmp_path, monkeypatch, capsys, "passphrase",
    )
    enc_path = iso.with_suffix(".d") / "session_000" / "enc_mode.json"
    data = json.loads(enc_path.read_text(encoding="utf-8"))
    data.pop("rescue", None)
    enc_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.delenv("ODDARCHIVER_PASSPHRASE", raising=False)
    monkeypatch.setattr("oddarchiver.cli.sys.stdin", io.StringIO(code + "\n"))

    dest2 = tmp_path / "dest-stripped"
    rc = dispatch(parser.parse_args([
        "restore", str(dest2),
        "--test-iso", str(iso),
        "--rescue-key",
    ]))
    captured = capsys.readouterr()
    assert rc == 1
    assert "no rescue key" in captured.err
    assert "predates this feature" in captured.err


def test_rescue_code_never_written_to_files(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        After passphrase init, the printed code is absent from every file
        under tmp_path (ISO, session tree, cache, ledger, log).
    """
    _source, iso, code = _init_encrypted(
        tmp_path, monkeypatch, capsys, "passphrase",
    )
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    enc = json.loads(backend.read_path("session_000/enc_mode.json"))
    assert enc["mode"] == "passphrase"
    assert "wrapped" in enc["rescue"]
    assert "salt" in enc["rescue"]
    assert enc["rescue"]["n"] == 16384
    assert enc["rescue"]["r"] == 8
    assert enc["rescue"]["p"] == 1
    _assert_code_absent(code, tmp_path)


def test_rescue_key_flag_without_value_prompts(tmp_path, monkeypatch, capsys):
    """
    Input:  tmp_path, monkeypatch, capsys
    Output: None
    Details:
        The flag prompts once via getpass on a terminal and restores.
    """
    _source, iso, code = _init_encrypted(
        tmp_path, monkeypatch, capsys, "passphrase",
    )
    monkeypatch.delenv("ODDARCHIVER_PASSPHRASE", raising=False)
    dest = tmp_path / "dest"
    parser = build_parser()
    args = parser.parse_args([
        "restore", str(dest),
        "--test-iso", str(iso),
        "--rescue-key",
    ])
    assert args.rescue_key == ""
    with patch("oddarchiver.cli.sys.stdin.isatty", return_value=True):
        with patch("oddarchiver.cli.getpass.getpass", return_value=code) as mock_gp:
            rc = dispatch(args)
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    mock_gp.assert_called_once_with("Rescue key: ")
    assert (dest / "file.txt").read_bytes() == b"rescue-key payload"


def test_rescue_key_argv_value_is_rejected(tmp_path, capsys):
    """
    Input:  tmp_path and capsys
    Output: None
    Details:
        The CLI refuses a rescue code supplied as a command argument.
    """
    parser = build_parser()
    with pytest.raises(SystemExit, match="2"):
        parser.parse_args([
            "restore", str(tmp_path / "dest"),
            "--rescue-key", "MFRG-GZDF",
        ])
    assert "unrecognized arguments: MFRG-GZDF" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="2"):
        parser.parse_args(["restore", "--rescue-key", "MFRG-GZDF"])
    assert "put the destination before --rescue-key" in capsys.readouterr().err


@pytest.mark.parametrize(("name", "value"), [
    ("n", 2**20), ("r", 16), ("p", 16),
])
def test_tampered_rescue_cost_never_runs_scrypt(name, value):
    """
    Input:  modified scrypt parameter
    Output: None
    Details:
        Reject a modified rescue cost before it can start scrypt.
    """
    code, raw = generate_rescue_code()
    block = wrap_rescue_secret(raw, b"x" * 32)
    block[name] = value
    with patch("oddarchiver.crypto.Scrypt", side_effect=AssertionError("scrypt ran")):
        with pytest.raises(ValueError, match="Decryption failed: authentication error."):
            unwrap_rescue_secret(code, block)


def test_keyfile_from_kek_matches_file_constructor(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        KeyfileCrypto.from_kek decrypts ciphertext produced by a
        file-path constructor with the same 32 bytes.
    """
    keyfile = tmp_path / "k.bin"
    generate_keyfile(str(keyfile))
    kek = keyfile.read_bytes()
    original = KeyfileCrypto(str(keyfile))
    rescued = KeyfileCrypto.from_kek(kek)
    plaintext = b"round-trip via from_kek"
    assert rescued.decrypt(original.encrypt(plaintext)) == plaintext
