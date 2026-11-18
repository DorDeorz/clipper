from __future__ import annotations

import json
from pathlib import Path

from clipper.log import RingBufferHandler, get_logger, redact, ring_buffer, setup_logging


def test_json_lines_to_file_and_ring(tmp_path: Path) -> None:
    setup_logging(tmp_path)
    log = get_logger("test")
    log.info("merhaba dünya", extra={"session_id": 3})
    lines = (tmp_path / "clipper.jsonl").read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["msg"] == "merhaba dünya"
    assert entry["session_id"] == 3
    assert entry["level"] == "INFO"
    assert entry["logger"] == "clipper.test"
    assert json.loads(ring_buffer().lines()[-1])["msg"] == "merhaba dünya"


def test_ring_buffer_capacity() -> None:
    import logging

    h = RingBufferHandler(capacity=5)
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("ringtest")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    for i in range(12):
        lg.info(str(i))
    assert h.lines() == ["7", "8", "9", "10", "11"]
    lg.removeHandler(h)


def test_secrets_redacted(tmp_path: Path) -> None:
    setup_logging(tmp_path)
    get_logger("t").warning("key sk-ant-abc123XYZ header Bearer abcdefghijklmnop")
    line = ring_buffer().lines()[-1]
    assert "sk-ant-abc123XYZ" not in line
    assert "abcdefghijklmnop" not in line
    assert redact("refresh_token=supersecret&x=1") == "refresh_token=***&x=1"
    assert redact('{"access_token": "tok"}') == '{"access_token": "***"}'
