import base64
import io
import json
import logging

import pytest

from oddarchiver.cli import build_parser, dispatch
from oddarchiver.crypto import generate_keyfile
from oddarchiver.disc import ISOBackend

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def isolate_archive_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "oddarchiver.cache.default_cache_dir", lambda: tmp_path / "cache",
    )


@pytest.mark.parametrize("mode", ["passphrase", "keyfile"])
def test_yubikey_init_restore_and_rescue(tmp_path, monkeypatch, capsys, mode):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_bytes(b"yubikey payload")
    iso = tmp_path / "archive.iso"
    parser = build_parser()
    key_options = []
    if mode == "passphrase":
        monkeypatch.setenv("ODDARCHIVER_PASSPHRASE", "test-passphrase")
    else:
        keyfile = tmp_path / "archive.key"
        generate_keyfile(str(keyfile))
        key_options = ["--key", str(keyfile)]

    calls = []

    def correct_response(challenge, slot):
        calls.append((challenge, slot))
        return b"A" * 20

    monkeypatch.setattr("oddarchiver.crypto.yubikey_challenge_response", correct_response)
    assert dispatch(parser.parse_args([
        "init", str(source), "--test-iso", str(iso), "--disc-size", "50mb",
        "--label", "YUBI", "--encrypt", mode, "--yubikey",
        "--yubikey-slot", "1", *key_options,
    ])) == 0
    output = capsys.readouterr().out
    assert "needs the YubiKey" in output
    assert "RESCUE KEY" in output
    rescue = output.split("RESCUE KEY", 1)[1].splitlines()
    rescue_code = next(line.strip() for line in rescue if line.strip() and
                       all(ch.isalnum() or ch == "-" for ch in line.strip()))
    raw = ISOBackend(iso).read_paths(["session_000/enc_mode.json"])
    metadata = json.loads(raw["session_000/enc_mode.json"])
    block = metadata["yubikey"]
    assert block["alg"] == "hmac-sha1"
    assert block["slot"] == 1
    assert len(base64.b64decode(block["challenge"], validate=True)) == 64
    assert len(calls) == 1

    def restore(name, *extra):
        destination = tmp_path / name
        rc = dispatch(parser.parse_args([
            "restore", str(destination), "--test-iso", str(iso),
            *key_options, *extra,
        ]))
        capsys.readouterr()
        return rc, destination

    rc, dest = restore("restored")
    assert rc == 0
    assert (dest / "file.txt").read_bytes() == b"yubikey payload"
    assert len(calls) == 2
    assert calls[0] == calls[1]

    monkeypatch.setattr(
        "oddarchiver.crypto.yubikey_challenge_response",
        lambda challenge, slot: b"B" * 20,
    )
    rc, wrong = restore("wrong")
    assert rc == 1
    assert not (wrong / "file.txt").exists()

    rescue_calls = []

    def forbidden_response(challenge, slot):
        rescue_calls.append((challenge, slot))
        raise AssertionError("rescue must not call the YubiKey")

    monkeypatch.setattr("oddarchiver.crypto.yubikey_challenge_response", forbidden_response)
    monkeypatch.setattr("oddarchiver.cli.sys.stdin", io.StringIO(rescue_code + "\n"))
    rc, rescued = restore("rescued", "--rescue-key")
    assert rc == 0
    assert (rescued / "file.txt").read_bytes() == b"yubikey payload"
    assert rescue_calls == []


def test_sync_copies_yubikey_block_to_later_session(tmp_path, monkeypatch, capsys):
    """Input: passphrase 2FA archive. Output: session_001 keeps yubikey and rescue."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_bytes(b"session zero")
    iso = tmp_path / "archive.iso"
    parser = build_parser()
    monkeypatch.setenv("ODDARCHIVER_PASSPHRASE", "test-passphrase")
    monkeypatch.setattr(
        "oddarchiver.crypto.yubikey_challenge_response",
        lambda challenge, slot: b"A" * 20,
    )
    assert dispatch(parser.parse_args([
        "init", str(source), "--test-iso", str(iso), "--disc-size", "50mb",
        "--label", "YUBISYNC", "--encrypt", "passphrase", "--yubikey",
    ])) == 0
    init_out = capsys.readouterr().out
    rescue_lines = init_out.split("RESCUE KEY", 1)[1].splitlines()
    rescue_code = next(
        line.strip() for line in rescue_lines
        if line.strip() and all(ch.isalnum() or ch == "-" for ch in line.strip())
    )
    (source / "file.txt").write_bytes(b"session one")
    assert dispatch(parser.parse_args([
        "sync", str(source), "--test-iso", str(iso),
    ])) == 0
    capsys.readouterr()
    raw = ISOBackend(iso).read_paths([
        "session_000/enc_mode.json", "session_001/enc_mode.json",
    ])
    first = json.loads(raw["session_000/enc_mode.json"])
    later = json.loads(raw["session_001/enc_mode.json"])
    assert later["yubikey"] == first["yubikey"]
    assert later["yubikey"]["alg"] == "hmac-sha1"
    assert len(base64.b64decode(later["yubikey"]["challenge"], validate=True)) == 64
    assert later["rescue"]["wrapped"] == first["rescue"]["wrapped"]
    rescue_calls = []

    def forbidden(challenge, slot):
        rescue_calls.append((challenge, slot))
        raise AssertionError("rescue must not call the YubiKey")

    monkeypatch.setattr("oddarchiver.crypto.yubikey_challenge_response", forbidden)
    monkeypatch.delenv("ODDARCHIVER_PASSPHRASE", raising=False)
    monkeypatch.setattr("oddarchiver.cli.sys.stdin", io.StringIO(rescue_code + "\n"))
    dest = tmp_path / "rescued"
    assert dispatch(parser.parse_args([
        "restore", str(dest), "--test-iso", str(iso), "--rescue-key",
    ])) == 0
    assert (dest / "file.txt").read_bytes() == b"session one"
    assert rescue_calls == []




@pytest.mark.parametrize("mode", ["passphrase", "keyfile"])
def test_archive_without_yubikey_skips_challenge(tmp_path, monkeypatch, capsys, mode):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_bytes(b"ordinary payload")
    iso = tmp_path / "archive.iso"
    parser = build_parser()
    key_options = []
    if mode == "passphrase":
        monkeypatch.setenv("ODDARCHIVER_PASSPHRASE", "test-passphrase")
    else:
        keyfile = tmp_path / "archive.key"
        generate_keyfile(str(keyfile))
        key_options = ["--key", str(keyfile)]

    def forbidden_response(challenge, slot):
        raise AssertionError("ordinary archives must not call the YubiKey")
    monkeypatch.setattr("oddarchiver.crypto.yubikey_challenge_response", forbidden_response)
    assert dispatch(parser.parse_args([
        "init", str(source), "--test-iso", str(iso), "--disc-size", "50mb",
        "--label", "ORDINARY", "--encrypt", mode, *key_options,
    ])) == 0
    capsys.readouterr()
    destination = tmp_path / "restored"
    assert dispatch(parser.parse_args([
        "restore", str(destination), "--test-iso", str(iso), *key_options,
    ])) == 0
    assert (destination / "file.txt").read_bytes() == b"ordinary payload"


def test_none_mode_with_yubikey_block_refuses():
    """Input: NullCrypto plus a yubikey block. Output: ValueError, no HMAC call."""
    from oddarchiver.cli import _maybe_apply_disc_yubikey
    from oddarchiver.crypto import NullCrypto, generate_yubikey_challenge

    block = {
        "alg": "hmac-sha1",
        "slot": 2,
        "challenge": base64.b64encode(generate_yubikey_challenge()).decode("ascii"),
    }
    with pytest.raises(ValueError, match="YubiKey metadata"):
        _maybe_apply_disc_yubikey(NullCrypto(), {"yubikey": block})

