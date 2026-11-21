"""9:16 layout filter graphs (ROADMAP 4.6). Input is `[0:v]` / `[0:a]` of raw.mp4;
output labels are `[v]` and `[a]`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

Layout = Literal["stack", "blur", "center"]
LAYOUTS: tuple[str, ...] = ("stack", "blur", "center")


@dataclass(frozen=True, slots=True)
class Rect:
    """Normalised rectangle (0-1) on the source frame."""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if not (0 <= self.x < 1 and 0 <= self.y < 1 and 0 < self.w <= 1 and 0 < self.h <= 1):
            raise ValueError(f"facecam dikdörtgeni 0-1 aralığında olmalı: {self}")
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError(f"facecam dikdörtgeni kareden taşıyor: {self}")

    @classmethod
    def from_json(cls, raw: str | None) -> Rect | None:
        if not raw:
            return None
        d = json.loads(raw)
        return cls(float(d["x"]), float(d["y"]), float(d["w"]), float(d["h"]))

    def to_json(self) -> str:
        return json.dumps({"x": self.x, "y": self.y, "w": self.w, "h": self.h})


def effective_layout(layout: str, facecam: Rect | None) -> str:
    """`stack` needs a facecam; without one we fall back to `blur`."""
    if layout not in LAYOUTS:
        return "blur"
    return "blur" if layout == "stack" and facecam is None else layout


def _f(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0"


def video_graph(
    layout: str,
    facecam: Rect | None,
    width: int = 1080,
    height: int = 1920,
    pan: str | None = None,
) -> str:
    """Filter chain from [0:v] to [v0] for the given layout. `pan` (center only) is an
    expression of `t` giving the crop position 0..1 along the free width (face tracking)."""
    layout = effective_layout(layout, facecam)
    if layout == "stack":
        assert facecam is not None
        cam_h = round(height * 608 / 1920)
        game_h = height - cam_h
        r = facecam
        return (
            f"[0:v]split=2[s0][s1];"
            f"[s0]crop=iw*{_f(r.w)}:ih*{_f(r.h)}:iw*{_f(r.x)}:ih*{_f(r.y)},"
            f"scale={width}:{cam_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{cam_h}[cam];"
            f"[s1]crop='min(iw,ih*{width}/{game_h})':ih:'(iw-min(iw,ih*{width}/{game_h}))/2':0,"
            f"scale={width}:{game_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{game_h}[game];"
            "[cam][game]vstack=inputs=2[v0]"
        )
    if layout == "center":
        pos = f":'(iw-ow)*({pan})':'(ih-oh)/2'" if pan else ""
        return f"[0:v]crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'{pos},scale={width}:{height}[v0]"
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:2[bgb];"
        f"[fg]scale={width}:-2[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v0]"
    )


def filter_graph(
    layout: str,
    facecam: Rect | None,
    *,
    ass_name: str | None,
    fontsdir: str | None,
    fps: int = 30,
    loudness: float = -14.0,
    width: int = 1080,
    height: int = 1920,
    pan: str | None = None,
    audio_input: int = 0,
) -> str:
    """Full -filter_complex. `ass_name` / `fontsdir` must be relative paths (the render
    runs with cwd = clip dir), which avoids escaping ':' and '\\' of Windows paths."""
    parts = [video_graph(layout, facecam, width, height, pan)]
    tail = f"[v0]setsar=1,fps={fps}"
    if ass_name:
        sub = f"subtitles={ass_name}"
        if fontsdir:
            sub += f":fontsdir={fontsdir}"
        tail += f",{sub}"
    parts.append(tail + ",format=yuv420p[v]")
    parts.append(f"[{audio_input}:a]loudnorm=I={_f(loudness)}:TP=-1.5:LRA=11,aresample=48000[a]")
    return ";".join(parts)
