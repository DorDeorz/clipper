from __future__ import annotations

from pathlib import Path

import pytest

from clipper.util import fs, timeutil
from clipper.util.crypto import CryptoError, SecretBox, generate_key


def test_crypto_roundtrip() -> None:
    box = SecretBox(generate_key())
    token = box.encrypt_json({"access": "a", "refresh": "r", "exp": 1})
    assert b"refresh" not in token
    assert box.decrypt_json(token) == {"access": "a", "refresh": "r", "exp": 1}


def test_crypto_wrong_key() -> None:
    token = SecretBox(generate_key()).encrypt(b"x")
    with pytest.raises(CryptoError):
        SecretBox(generate_key()).decrypt(token)
    with pytest.raises(CryptoError):
        SecretBox("not-a-key")


def test_atomic_write(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b.json"
    fs.atomic_write_text(target, "ilk")
    fs.atomic_write_text(target, "ikinci ğ")
    assert target.read_text(encoding="utf-8") == "ikinci ğ"
    assert [p.name for p in target.parent.iterdir()] == ["b.json"]


def test_disk_usage_nonexistent_subdir(tmp_path: Path) -> None:
    u = fs.disk_usage(tmp_path / "does" / "not" / "exist")
    assert u.total > 0 and 0 <= u.used_ratio <= 1


def test_human_bytes() -> None:
    assert fs.human_bytes(512) == "512 B"
    assert fs.human_bytes(1536) == "1.5 KB"
    assert fs.human_bytes(3 * 1024**3) == "3.0 GB"


def test_safe_join(tmp_path: Path) -> None:
    assert fs.safe_join(tmp_path, "x", "y.mp4") == (tmp_path / "x" / "y.mp4").resolve()
    with pytest.raises(ValueError):
        fs.safe_join(tmp_path, "..", "etc")


def test_time_helpers() -> None:
    ts = timeutil.parse_iso("2026-11-18T09:00:00.500Z")
    assert ts == pytest.approx(1794992400.5)
    assert timeutil.parse_iso(timeutil.to_iso(ts)) == pytest.approx(ts)
    assert timeutil.fmt_duration(3725.4) == "1:02:05"
    assert timeutil.fmt_duration(65) == "1:05"
