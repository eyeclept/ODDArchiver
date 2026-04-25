"""
Author: Richard Baldwin
Date:   2026
Email:  eyeclept@pm.me

Description:
    Tests for oddarchiver.verify — fast, checksum, and full verification levels.
"""
# Imports
import hashlib
from pathlib import Path

import pytest

from oddarchiver.crypto import NullCrypto
from oddarchiver.disc import ISOBackend
from oddarchiver.manifest import Manifest, ManifestEntry, write_manifest
from oddarchiver.verify import verify

# Globals
SMALL_DISC = 20 * 2**20  # 20 MiB
_crypto = NullCrypto()


# Functions


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_id(session_n: int, rel_path: str) -> str:
    """Matching session.py _blob_id: sha256(session_n:rel_path)."""
    return hashlib.sha256(f"{session_n}:{rel_path}".encode()).hexdigest()


def _make_staging(
    tmp_path: Path,
    session_n: int,
    files: dict[str, bytes],
    deleted: list[str] | None = None,
    based_on: int | None = None,
    timestamp: str = "2026-04-23T00:00:00Z",
) -> Path:
    """
    Input:  tmp_path, session_n, files — base dir, session index, file contents
            deleted, based_on, timestamp — optional manifest fields
    Output: staging Path ready for ISOBackend.init / append
    Details:
        Writes NullCrypto blobs (plaintext == ciphertext) and a valid manifest.
        Blob filenames are sha256(session_n:rel_path) matching session.py.
    """
    session_name = f"session_{session_n:03d}"
    staging = tmp_path / f"staging_{session_n}"
    session_dir = staging / session_name
    full_dir = session_dir / "full"
    full_dir.mkdir(parents=True)

    entries = []
    for rel_path, content in files.items():
        blob = _crypto.encrypt(content)
        blob_name = _blob_id(session_n, rel_path)
        dest = full_dir / blob_name
        dest.write_bytes(blob)
        entries.append(ManifestEntry(
            path=rel_path,
            type="full",
            result_checksum=_sha256(content),
            full_size_bytes=len(content),
            file=f"{session_name}/full/{blob_name}",
        ))

    manifest = Manifest(
        version=1,
        session=session_n,
        timestamp=timestamp,
        source="/src",
        label="TEST",
        based_on_session=based_on,
        encryption={},
        entries=entries,
        deleted=deleted or [],
        manifest_checksum="",
    )
    write_manifest(session_dir, manifest)
    return staging


def _burn(backend: ISOBackend, staging: Path, session_n: int) -> None:
    """Burn staging via init (session 0) or append."""
    if session_n == 0:
        backend.init(staging, label="TEST", expected_session_count=0)
    else:
        backend.append(staging, label="TEST", expected_session_count=session_n)


# Tests


def test_fast_ok_on_clean_iso(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Clean single-session ISO must pass fast verify and return True.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"hello"}), session_n=0)

    result = verify(backend, _crypto, level="fast")
    assert result is True


def test_fast_fail_on_tampered_manifest(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        Burn a clean session then corrupt its manifest.json on sessions_root.
        fast verify must detect SUSPECT and raise SystemExit(1).
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"hello"}), session_n=0)

    # tamper with the manifest on disc
    manifest_on_disc = backend._sessions_root / "session_000" / "manifest.json"
    text = manifest_on_disc.read_text()
    manifest_on_disc.write_text(text.replace("TEST", "TAMPERED"))

    with pytest.raises(SystemExit) as exc:
        verify(backend, _crypto, level="fast")
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "FAIL" in out


def test_checksum_fail_on_corrupted_blob(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        Burn a clean session then overwrite the stored blob with garbage.
        checksum verify must detect the hash mismatch and raise SystemExit(1).
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"real content"}), session_n=0)

    blob_path = backend._sessions_root / "session_000" / "full" / _blob_id(0, "a.txt")
    blob_path.write_bytes(b"CORRUPTED GARBAGE")

    with pytest.raises(SystemExit) as exc:
        verify(backend, _crypto, level="checksum")
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "checksum mismatch" in out.lower()


def test_full_ok_on_clean_iso(tmp_path):
    """
    Input:  tmp_path
    Output: None
    Details:
        Two-session ISO with consistent data must pass full verify.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"alpha"}, timestamp="2026-04-23T00:00:00Z"), session_n=0)
    _burn(backend, _make_staging(tmp_path, 1, {"b.txt": b"beta"}, based_on=0, timestamp="2026-04-23T01:00:00Z"), session_n=1)

    result = verify(backend, _crypto, level="full")
    assert result is True


def test_failed_session_reported_per_session_others_ok(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        Two-session ISO; corrupt session 0 blob only.
        Output must show session 0 FAIL and session 1 OK.
        Result line must show exactly 1 error.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"alpha"}, timestamp="2026-04-23T00:00:00Z"), session_n=0)
    _burn(backend, _make_staging(tmp_path, 1, {"b.txt": b"beta"}, based_on=0, timestamp="2026-04-23T01:00:00Z"), session_n=1)

    blob_path = backend._sessions_root / "session_000" / "full" / _blob_id(0, "a.txt")
    blob_path.write_bytes(b"CORRUPTED")

    with pytest.raises(SystemExit) as exc:
        verify(backend, _crypto, level="checksum")
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "Session 000" in out and "FAIL" in out
    assert "Session 001" in out and "OK" in out
    assert "1 error" in out


def test_fast_fail_on_invalid_json_manifest_no_traceback(tmp_path, capsys):
    """
    Input:  tmp_path, capsys
    Output: None
    Details:
        B5/B6 regression: corrupt JSON in a manifest (simulating `echo junk >>
        manifest.json`) must cause verify --level fast to exit 1 with a FAIL
        report, not raise JSONDecodeError.
    """
    iso = tmp_path / "test.iso"
    backend = ISOBackend(iso, disc_size=SMALL_DISC)
    _burn(backend, _make_staging(tmp_path, 0, {"a.txt": b"content"}), session_n=0)

    manifest_on_disc = backend._sessions_root / "session_000" / "manifest.json"
    with open(manifest_on_disc, "a", encoding="utf-8") as fh:
        fh.write("\njunk appended by accident")

    with pytest.raises(SystemExit) as exc:
        verify(backend, _crypto, level="fast")
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "FAIL" in out
