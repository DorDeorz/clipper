"""TESTPLAN 8.2 security checks (`pytest -m security`)."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from clipper.config import Config
from clipper.render.ass_captions import build_ass
from clipper.render.layouts import filter_graph

pytestmark = pytest.mark.security
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "clipper"


def test_sec01_panel_binds_localhost_by_default() -> None:
    assert Config().panel.host == "127.0.0.1"


SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)authorization:\s*bearer\s+\S{8,}"),
    re.compile(r"(?i)\b(access|refresh)_token[\"']?[ \t]*[:=][ \t]*[\"'][A-Za-z0-9._-]{12,}"),
    re.compile(r"(?i)client_secret[\"']?[ \t]*[:=][ \t]*[\"'][A-Za-z0-9._-]{12,}"),
)


def scan_text_for_secrets(text: str) -> list[str]:
    return [m.group(0) for p in SECRET_PATTERNS for m in p.finditer(text)]


def test_sec03_secret_scanner_catches_and_logs_are_clean() -> None:
    assert scan_text_for_secrets('{"access_token": "ya29.abcdefghijklmnop"}')
    assert scan_text_for_secrets("Authorization: Bearer abcdefghijkl")
    assert not scan_text_for_secrets('{"msg": "token refreshed", "platform": "x"}')
    # Any log files from earlier runs in the repo's data dir must be clean too.
    for f in (ROOT / "data" / "logs").glob("*.log*") if (ROOT / "data" / "logs").is_dir() else []:
        assert not scan_text_for_secrets(f.read_text(encoding="utf-8", errors="replace")), f


def test_sec04_gitignore_and_no_committed_secrets() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and "data/" in ignored
    try:
        files = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git yok")
    assert ".env" not in files
    for name in files:
        if name.startswith("tests/"):
            continue  # tests carry obviously fake keys on purpose
        p = ROOT / name
        if p.suffix in {".py", ".md", ".yaml", ".yml", ".toml", ".json", ".example", ".js"}:
            text = p.read_text(encoding="utf-8", errors="replace")
            hits = scan_text_for_secrets(text)
            assert not hits, f"{name}: {hits}"


def test_sec05_no_shell_true() -> None:
    bad = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell" and not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is False
                    ):
                        bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                f = node.func
                owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(
                    f.value, ast.Name) else None  # fmt: skip
                if isinstance(f, ast.Attribute) and (
                    (owner == "os" and f.attr in {"system", "popen"})
                    or f.attr == "create_subprocess_shell"
                ):
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not bad


EVIL = "{\\an8}KÖTÜ\\N satır; rm -rf $(whoami) 'x' `y`"


def test_sec06_titles_cannot_inject_ass_or_filters() -> None:
    from clipper.render.ass_captions import CapWord

    ass = build_ass([CapWord(EVIL, 0.0, 1.0)], duration=2.0, layout="blur", hook=EVIL)
    dialogue = [ln for ln in ass.splitlines() if ln.startswith("Dialogue")]
    body = "\n".join(ln.split(",,", 1)[1] for ln in dialogue)
    assert "{\\an8}" not in body and "\\N" not in body.replace("{\\", "")
    # The text never reaches the ffmpeg filter graph (it lives in the .ass file).
    graph = filter_graph("blur", None, ass_name="clip.ass", fontsdir=None, fps=30,
                         loudness=-14.0, width=1080, height=1920)  # fmt: skip
    assert "KÖTÜ" not in graph and "whoami" not in graph
