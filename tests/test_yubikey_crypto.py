"""Unit tests for optional YubiKey challenge-response mixing."""

import base64
import hashlib
import logging
import subprocess

import pytest

from oddarchiver.crypto import (
    YUBIKEY_CHALLENGE_LEN,
    YUBIKEY_HMAC_LEN,
    YUBIKEY_MIX_PREFIX,
    KeyfileCrypto,
    NullCrypto,
    PassphraseCrypto,
    apply_yubikey_mix,
    generate_yubikey_challenge,
    mix_yubikey_secret,
    parse_yubikey_block,
    yubikey_challenge_response,
)

logger = logging.getLogger(__name__)


def test_generate_yubikey_challenge():
    first = generate_yubikey_challenge()
    second = generate_yubikey_challenge()
    assert len(first) == YUBIKEY_CHALLENGE_LEN
    assert first != second


def test_mix_changes_with_each_input():
    primary = b"primary"
    response = bytes(range(YUBIKEY_HMAC_LEN))
    mixed = mix_yubikey_secret(primary, response)
    assert mixed == hashlib.sha256(YUBIKEY_MIX_PREFIX + primary + response).digest()
    assert len(mixed) == 32
    assert mixed != hashlib.sha256(primary + response).digest()
    assert mixed != mix_yubikey_secret(b"different", response)
    assert mixed != mix_yubikey_secret(primary, bytes(reversed(response)))


@pytest.mark.parametrize("primary,response", [(b"", b"x" * 20), (b"primary", b"short")])
def test_mix_rejects_invalid_inputs(primary, response):
    with pytest.raises(ValueError):
        mix_yubikey_secret(primary, response)


def test_parse_yubikey_block_round_trip():
    challenge = generate_yubikey_challenge()
    for slot in (1, 2):
        block = {"alg": "hmac-sha1", "slot": slot,
                 "challenge": base64.b64encode(challenge).decode("ascii")}
        assert parse_yubikey_block(block) == (slot, challenge)


@pytest.mark.parametrize("block", [
    None,
    {},
    {"alg": "other", "slot": 2, "challenge": "YQ=="},
    {"alg": "hmac-sha1", "slot": True, "challenge": "YQ=="},
    {"alg": "hmac-sha1", "slot": 3, "challenge": "YQ=="},
    {"alg": "hmac-sha1", "slot": 2, "challenge": "not-base64!"},
    {"alg": "hmac-sha1", "slot": 2, "challenge": ""},
    {"alg": "hmac-sha1", "slot": 2, "challenge": base64.b64encode(b"x" * 65).decode("ascii")},
])
def test_parse_yubikey_block_rejects_invalid_data(block):
    with pytest.raises(ValueError):
        parse_yubikey_block(block)


@pytest.mark.parametrize("backend", ["passphrase", "keyfile"])
def test_apply_yubikey_mix_rekeys_backend(backend):
    response = b"r" * YUBIKEY_HMAC_LEN
    if backend == "passphrase":
        original = PassphraseCrypto(b"passphrase")
        mixed = PassphraseCrypto(b"passphrase")
        secret_attr = "_passphrase"
    else:
        original = KeyfileCrypto.from_kek(b"k" * 32)
        mixed = KeyfileCrypto.from_kek(b"k" * 32)
        secret_attr = "_kek"
    assert apply_yubikey_mix(mixed, response) is mixed
    assert getattr(mixed, secret_attr) == mix_yubikey_secret(getattr(original, secret_attr), response)
    payload = b"encrypted with the mixed secret"
    ciphertext = mixed.encrypt(payload)
    assert mixed.decrypt(ciphertext) == payload
    with pytest.raises(ValueError):
        original.decrypt(ciphertext)


def test_apply_yubikey_mix_rejects_other_backends():
    with pytest.raises(TypeError):
        apply_yubikey_mix(NullCrypto(), b"r" * YUBIKEY_HMAC_LEN)


def test_yubikey_challenge_response_with_injected_run():
    calls = []
    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ab" * 20 + "\n")

    assert yubikey_challenge_response(b"\x00\xff", slot=1, run=run) == bytes.fromhex("ab" * 20)
    assert calls == [["ykchalresp", "-1", "-x", "00ff"]]


def test_yubikey_challenge_response_falls_back_when_tool_missing():
    calls = []
    def run(argv):
        calls.append(argv)
        if argv[0] == "ykchalresp":
            raise FileNotFoundError("ykchalresp")
        return subprocess.CompletedProcess(argv, 0, stdout="cd" * 20)

    assert yubikey_challenge_response(b"a", run=run) == bytes.fromhex("cd" * 20)
    assert calls[1] == ["ykman", "otp", "calculate", "2", "61"]


def test_yubikey_challenge_response_missing_both_tools():
    def run(argv):
        raise FileNotFoundError(argv[0])

    with pytest.raises(RuntimeError, match="ykman.*slot 2"):
        yubikey_challenge_response(b"a", run=run)


@pytest.mark.parametrize("stdout,returncode", [("not hex", 0), ("ab" * 19, 0), ("ab" * 20, 1)])
def test_yubikey_challenge_response_rejects_invalid_output(stdout, returncode):
    def run(argv):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout)

    with pytest.raises(RuntimeError, match="ykchalresp.*slot 2"):
        yubikey_challenge_response(b"a", run=run)


@pytest.mark.parametrize("challenge,slot", [(b"", 2), (b"a" * 65, 2), (b"a", 0), (b"a", 3)])
def test_yubikey_challenge_response_rejects_invalid_arguments(challenge, slot):
    with pytest.raises(ValueError):
        yubikey_challenge_response(challenge, slot=slot, run=lambda argv: None)
