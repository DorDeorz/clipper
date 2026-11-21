"""Render one clip: raw.mp4 [start, end] -> final.mp4 (9:16, captions, hook, loudnorm)
+ thumb.jpg."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from clipper.config import RenderConfig
from clipper.render.ass_captions import build_ass, clip_words
from clipper.render.facetrack import FacePlan, pan_expr
from clipper.render.ffmpeg import (
    RenderResult,
    build_render_args,
    install_fonts,
    pick_encoder,
    run_render,
    thumbnail,
)
from clipper.render.layouts import Rect, effective_layout, filter_graph

ASS_NAME = "clip.ass"


async def render_clip(
    cfg: RenderConfig,
    raw: Path,
    start: float,
    end: float,
    words: Sequence[tuple[str, float, float]],
    out_dir: Path,
    *,
    layout: str = "blur",
    facecam: Rect | None = None,
    hook: str | None = None,
    thumb_at: float | None = None,
    ffmpeg: str = "ffmpeg",
    fonts_dir: Path | None = None,
    progress: Callable[[float], None] | None = None,
    final_name: str = "final.mp4",
    face: FacePlan | None = None,
    clean_audio: Path | None = None,
) -> RenderResult:
    """`start`/`end` and `words` are in raw.mp4 time; `thumb_at` too (None = middle).
    `face` (face tracking) replaces the facecam crop / pans the center crop; `clean_audio`
    (denoised WAV on raw.mp4's timeline) replaces the clip's own audio."""
    if end <= start:
        raise ValueError("bitiş başlangıçtan sonra olmalı")
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = end - start
    layout = effective_layout(layout, facecam)
    pan = None
    if face is not None and face.kind == "cam" and face.cam is not None and layout == "stack":
        facecam = face.cam
    elif face is not None and face.kind == "pan" and face.pan and layout == "center":
        pan = pan_expr(face.pan)
    if clean_audio is not None and not clean_audio.is_file():
        clean_audio = None
    ass = build_ass(
        clip_words(words, start, end),
        duration=duration,
        layout=layout,
        hook=hook,
        width=cfg.width,
        height=cfg.height,
    )
    (out_dir / ASS_NAME).write_text(ass, encoding="utf-8")
    fontsdir = install_fonts(fonts_dir, out_dir)
    graph = filter_graph(
        layout,
        facecam,
        ass_name=ASS_NAME,
        fontsdir=fontsdir,
        fps=cfg.fps,
        loudness=cfg.loudness_lufs,
        width=cfg.width,
        height=cfg.height,
        pan=pan,
        audio_input=1 if clean_audio else 0,
    )
    encoder = await pick_encoder(cfg, ffmpeg)
    final = out_dir / final_name
    tmp = out_dir / f".{final_name}.part.mp4"
    args = build_render_args(
        ffmpeg,
        raw.resolve(),
        start,
        end,
        graph,
        tmp.name,
        encoder,
        cfg.fps,
        audio=clean_audio.resolve() if clean_audio else None,
    )
    await run_render(args, out_dir, duration, progress)
    tmp.replace(final)
    at = duration / 2 if thumb_at is None else min(max(thumb_at - start, 0.0), duration - 0.1)
    thumb = await thumbnail(ffmpeg, final, at, out_dir / "thumb.jpg")
    return RenderResult(final, thumb, duration, encoder)
