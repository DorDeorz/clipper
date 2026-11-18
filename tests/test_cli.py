from __future__ import annotations

from pathlib import Path

import pytest

from clipper.__main__ import main
from clipper.util.crypto import SecretBox


def _cfg(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(f"paths: {{ data_dir: {tmp_path.as_posix()}/data }}\n", encoding="utf-8")
    return p


def test_keygen(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["keygen"]) == 0
    line = capsys.readouterr().out.strip()
    key = line.split("=", 1)[1]
    SecretBox(key)


def test_db_migrate_and_version(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _cfg(tmp_path)
    env = tmp_path / ".env"
    assert main(["--config", str(cfg), "--env", str(env), "db", "migrate"]) == 0
    assert "v0 → v3" in capsys.readouterr().out
    assert main(["--config", str(cfg), "--env", str(env), "db", "version"]) == 0
    assert "v3" in capsys.readouterr().out
    assert (tmp_path / "data" / "clipper.db").is_file()


def test_bad_config_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("detector: { nope: 1 }\n", encoding="utf-8")
    assert main(["--config", str(p), "doctor"]) == 2
    assert "Hata:" in capsys.readouterr().err


def test_doctor_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--config", str(_cfg(tmp_path)), "--env", str(tmp_path / ".env"), "doctor"])
    out = capsys.readouterr().out
    assert "ffmpeg" in out and "Veritabanı" in out
    assert code in (0, 1)


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "doctor" in capsys.readouterr().out


def test_doctor_output_survives_cp1252_pipe(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "clipper",
            "--config",
            str(_cfg(tmp_path)),
            "--env",
            str(tmp_path / ".env"),
            "doctor",
        ],
        capture_output=True,
        env=env,
        check=False,
    )
    assert res.returncode in (0, 1), res.stderr.decode("utf-8", "replace")
    assert "Veritabanı".encode() in res.stdout
