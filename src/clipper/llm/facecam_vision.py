"""`clipper facecam <streamer>`: ask Claude (vision) where the facecam is on a few frames
of the streamer's latest session, and suggest a `facecam_rect` for the `stack` layout."""

from __future__ import annotations

import base64
import statistics
from dataclasses import dataclass
from pathlib import Path

from clipper.ffmpeg_probe import run_tool
from clipper.llm.client import LLMClient
from clipper.llm.schemas import FacecamDecision
from clipper.render.layouts import Rect

FACECAM_SYSTEM = """\
Sana bir canlı yayından ekran görüntüleri veriliyor. Görevin, yayıncının kamerasının \
(facecam: yayıncının yüzünün göründüğü küçük video kutusu) ekrandaki yerini bulmak.
- Koordinatları görüntü boyutuna göre 0-1 aralığında ver: x, y sol üst köşe; w, h genişlik \
ve yükseklik. Kutunun kenarlarını sıkı al ama yüzü kesme.
- Kamera tüm ekranı kaplıyorsa (sadece sohbet yayını) ya da hiç kamera yoksa found=false ver.
- confidence 0-100. reason alanına Türkçe kısa gerekçe yaz."""


@dataclass(frozen=True, slots=True)
class FacecamSuggestion:
    rect: Rect | None
    confidence: int
    reasons: list[str]
    usd: float


async def grab_frames(
    ffmpeg: str, video: Path, times: list[float], out_dir: Path, width: int = 1280
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, t in enumerate(times):
        out = out_dir / f"frame{i}.jpg"
        res = await run_tool(
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{t:.2f}", "-i", str(video),
            "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out),
            timeout=120,
        )  # fmt: skip
        if res.returncode == 0 and out.is_file():
            frames.append(out)
    return frames


def _image_block(path: Path) -> dict[str, object]:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


async def suggest_facecam(
    llm: LLMClient, frames: list[Path], model: str, effort: str = "low"
) -> FacecamSuggestion:
    """One call per frame; the suggestion is the median box of the frames where a facecam
    was found (robust to one odd frame)."""
    found: list[FacecamDecision] = []
    reasons: list[str] = []
    usd = 0.0
    for f in frames:
        res = await llm.structured(
            purpose="facecam",
            model=model,
            system=FACECAM_SYSTEM,
            user=[_image_block(f), {"type": "text", "text": "Facecam nerede?"}],
            schema=FacecamDecision,
            effort=effort,
            max_tokens=1000,
        )
        usd += res.usd
        reasons.append(res.value.reason)
        if res.value.found and res.value.w > 0.02 and res.value.h > 0.02:
            found.append(res.value)
    if not found or len(found) * 2 < len(frames):
        return FacecamSuggestion(None, 0, reasons, usd)
    med = {k: statistics.median(getattr(d, k) for d in found) for k in ("x", "y", "w", "h")}
    w = min(med["w"], 1 - med["x"])
    h = min(med["h"], 1 - med["y"])
    rect = Rect(round(med["x"], 4), round(med["y"], 4), round(w, 4), round(h, 4))
    conf = round(statistics.median(d.confidence for d in found))
    return FacecamSuggestion(rect, conf, reasons, usd)


async def preview(ffmpeg: str, frame: Path, rect: Rect, out: Path) -> Path:
    box = f"drawbox=x=iw*{rect.x}:y=ih*{rect.y}:w=iw*{rect.w}:h=ih*{rect.h}:color=red@0.9:t=6"
    res = await run_tool(
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(frame),
        "-vf", box, "-q:v", "3", str(out), timeout=60,
    )  # fmt: skip
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:300])
    return out
