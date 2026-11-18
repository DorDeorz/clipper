from __future__ import annotations

from pathlib import Path

import pytest

from clipper.config import (
    Config,
    ConfigError,
    load_config,
    load_secrets,
    parse_dotenv,
)

ROOT = Path(__file__).resolve().parents[1]


def test_defaults_match_roadmap() -> None:
    cfg = Config()
    assert cfg.detector.threshold == 3.0
    assert cfg.detector.weights.rate == 0.40
    assert cfg.llm.judge_model == "claude-sonnet-5-5"
    assert cfg.render.width == 1080 and cfg.render.height == 1920
    assert cfg.recorder.quality_order[0] == "1080p60"
    assert cfg.monitoring.mode == "anonymous"


def test_monitoring_mode_validated(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("monitoring: { mode: official }\n", encoding="utf-8")
    assert load_config(p).monitoring.mode == "official"
    p.write_text("monitoring: { mode: sneaky }\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_example_config_loads_and_equals_defaults() -> None:
    cfg = load_config(ROOT / "config.example.yaml")
    defaults = Config()
    assert cfg.detector == defaults.detector
    assert cfg.publish == defaults.publish
    assert cfg.paths.data_dir.is_absolute()


def test_missing_config_gives_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "nope.yaml") == Config()


def test_unknown_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("detector: { treshold: 2 }\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="geçersiz"):
        load_config(p)


def test_bad_yaml(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_parse_dotenv() -> None:
    text = """
# comment
export A=1
B = two words  # trailing comment
C="quoted # not comment\\nnext"
D='single \\n raw'
E=
"""
    env = parse_dotenv(text)
    assert env == {
        "A": "1",
        "B": "two words",
        "C": "quoted # not comment\nnext",
        "D": "single \\n raw",
        "E": "",
    }


def test_parse_dotenv_errors() -> None:
    with pytest.raises(ConfigError):
        parse_dotenv("NOEQUALS\n")
    with pytest.raises(ConfigError):
        parse_dotenv('X="open\n')


def test_secrets_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / ".env"
    p.write_text("ANTHROPIC_API_KEY=from-file\nYOUTUBE_API_KEY=yt\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    s = load_secrets(p)
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "from-env"
    assert s.youtube_api_key is not None and s.youtube_api_key.get_secret_value() == "yt"
    assert "from-env" not in repr(s)
