"""Audit B-06: a cancelled task (app shutdown, job cancel) must not leave ffmpeg running."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from clipper import ffmpeg_probe
from clipper.detect import audio
from clipper.render import ffmpeg as render_ffmpeg

SLEEPER = (sys.executable, "-c", "import time; time.sleep(60)")


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.subprocess.Process]:
    """Every spawn in the modules under test starts a 60 s sleeper instead of ffmpeg."""
    procs: list[asyncio.subprocess.Process] = []
    real = asyncio.create_subprocess_exec

    async def fake(*_args: Any, **kw: Any) -> asyncio.subprocess.Process:
        kw.pop("cwd", None)
        proc = await real(*SLEEPER, **kw)
        procs.append(proc)
        return proc

    for mod in (ffmpeg_probe, render_ffmpeg, audio):
        monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake)
    return procs


async def _cancel_soon(coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _check_killed(procs: list[asyncio.subprocess.Process]) -> None:
    assert len(procs) == 1
    alive = procs[0].returncode is None
    if alive:  # do not leave the sleeper behind when the test fails
        procs[0].kill()
    assert not alive, "child process still running after cancel"


def test_run_tool_kills_process_on_cancel(spawned: list[Any]) -> None:
    async def go() -> None:
        await _cancel_soon(ffmpeg_probe.run_tool("ffmpeg", "-version", timeout=60))
        _check_killed(spawned)

    asyncio.run(go())


def test_run_render_kills_process_on_cancel(spawned: list[Any], tmp_path: Path) -> None:
    async def go() -> None:
        await _cancel_soon(render_ffmpeg.run_render(["ffmpeg"], tmp_path, 10.0))
        _check_killed(spawned)

    asyncio.run(go())


def test_segment_db_kills_process_on_cancel(spawned: list[Any], tmp_path: Path) -> None:
    async def go() -> None:
        await _cancel_soon(audio.segment_db(tmp_path / "x.ts", 0.0))
        _check_killed(spawned)

    asyncio.run(go())
