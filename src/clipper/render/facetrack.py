"""Face tracking for the vertical crop (no OpenCV): frames are decoded with PyAV at a low
rate, faces come from the 1 MB UltraFace RFB-320 ONNX model on onnxruntime (both already
installed through faster-whisper), and the result is turned into an ffmpeg crop.

- `center`: the 9:16 window pans horizontally after the face (dead zone + speed limit,
  written as a piecewise-linear `crop` x expression of `t`).
- `stack`: the top panel is cut from the facecam rectangle around the face, in the panel's
  own aspect, so a tall or wide webcam box no longer cuts off the head.

Anything that fails or finds no face returns None and the render keeps its old crop."""

from __future__ import annotations

import asyncio
import functools
import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from clipper.log import get_logger
from clipper.render.layouts import Rect
from clipper.util.download import ensure_pinned

log = get_logger("facetrack")

# github.com/Linzaer/Ultra-Light-Fast-Generic-Face-Detector-1MB (MIT), pinned commit.
MODEL_URL = (
    "https://raw.githubusercontent.com/Linzaer/Ultra-Light-Fast-Generic-Face-Detector-1MB/"
    "dffdddda9794a50607cba8f318507a28c1c27cab/models/onnx/version-RFB-320.onnx"
)
MODEL_SHA256 = "34cd7e60aeff28744c657de7a3dc64e872d506741de66987f3426f2b79f88017"
MODEL_NAME = "version-RFB-320.onnx"
_IN_W, _IN_H = 320, 240

# A face box (x0, y0, x1, y1, score), normalised to the analysed image.
Box = tuple[float, float, float, float, float]


class Detector(Protocol):
    def __call__(self, rgb: np.ndarray) -> list[Box]: ...


@dataclass(frozen=True, slots=True)
class FacePlan:
    """`pan`: (t, p) keyframes, p = crop position 0..1 of the free width (center layout).
    `cam`: face-centred facecam crop for the stack layout."""

    kind: str  # "pan" | "cam"
    pan: tuple[tuple[float, float], ...] = ()
    cam: Rect | None = None


# -- detector -----------------------------------------------------------------------------


def _resize_nearest(img: np.ndarray, w: int, h: int) -> np.ndarray:
    ys = (np.arange(h) * img.shape[0] / h).astype(np.intp)
    xs = (np.arange(w) * img.shape[1] / w).astype(np.intp)
    return img[ys][:, xs]


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float = 0.3, top_k: int = 5) -> list[int]:
    order = np.argsort(-scores)
    keep: list[int] = []
    while order.size and len(keep) < top_k:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        xx0 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy0 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx1 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy1 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx1 - xx0, 0, None) * np.clip(yy1 - yy0, 0, None)
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        union = area[i] + area[rest] - inter
        order = rest[inter / np.maximum(union, 1e-9) <= iou]
    return keep


class UltraFace:
    def __init__(self, model: Path, min_score: float = 0.7) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        opts.intra_op_num_threads = 2
        self._sess: Any = ort.InferenceSession(
            str(model), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input = self._sess.get_inputs()[0].name
        self.min_score = min_score

    def __call__(self, rgb: np.ndarray) -> list[Box]:
        img = _resize_nearest(rgb, _IN_W, _IN_H).astype(np.float32)
        x = ((img - 127.0) / 128.0).transpose(2, 0, 1)[None]
        scores, boxes = self._sess.run(None, {self._input: x})
        s = scores[0, :, 1]
        mask = s > self.min_score
        if not mask.any():
            return []
        b, s = np.clip(boxes[0][mask], 0.0, 1.0), s[mask]
        return [(*map(float, b[i]), float(s[i])) for i in nms(b, s)]  # type: ignore[misc]


@functools.lru_cache(maxsize=2)
def _load_detector(model: str, min_score: float) -> UltraFace:
    return UltraFace(Path(model), min_score)


async def get_detector(models_dir: Path, min_score: float) -> Detector:
    path = await ensure_pinned(MODEL_URL, MODEL_SHA256, models_dir / "face" / MODEL_NAME)
    return await asyncio.to_thread(_load_detector, str(path), min_score)


# -- frames -------------------------------------------------------------------------------


def sample_frames(
    video: Path, start: float, end: float, fps: float, width: int = 960
) -> tuple[tuple[int, int], Iterator[tuple[float, np.ndarray]]]:
    """((src_w, src_h), iterator of (t - start, rgb frame `width` wide))."""
    import av

    container = av.open(str(video))
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    src = (int(stream.codec_context.width), int(stream.codec_context.height))
    height = max(2, round(width * src[1] / max(src[0], 1)) // 2 * 2)

    def gen() -> Iterator[tuple[float, np.ndarray]]:
        try:
            if start > 0 and stream.time_base is not None:
                container.seek(int(start / stream.time_base), stream=stream)
            nxt = start
            for frame in container.decode(stream):
                t = frame.time
                if t is None or t < nxt - 1e-3:
                    continue
                if t > end:
                    break
                nxt = t + 1.0 / fps
                yield t - start, frame.to_ndarray(format="rgb24", width=width, height=height)
        finally:
            container.close()

    return src, gen()


def crop_region(rgb: np.ndarray, r: Rect) -> np.ndarray:
    h, w = rgb.shape[:2]
    x0, y0 = round(r.x * w), round(r.y * h)
    x1, y1 = max(x0 + 2, round((r.x + r.w) * w)), max(y0 + 2, round((r.y + r.h) * h))
    return rgb[y0:y1, x0:x1]


# -- planning (pure) ----------------------------------------------------------------------


def pick_track(dets: Sequence[tuple[float, list[Box]]]) -> list[tuple[float, Box | None]]:
    """One face per sample: start with the biggest, then the one closest to the last."""
    out: list[tuple[float, Box | None]] = []
    last: Box | None = None
    for t, boxes in dets:
        if not boxes:
            out.append((t, None))
            continue
        if last is None:
            best = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]) * b[4])
        else:
            lx, ly = (last[0] + last[2]) / 2, (last[1] + last[3]) / 2
            best = min(
                boxes,
                key=lambda b: (
                    abs((b[0] + b[2]) / 2 - lx) + abs((b[1] + b[3]) / 2 - ly) - 0.5 * (b[2] - b[0])
                ),
            )
        out.append((t, best))
        last = best
    return out


def _median_filter(vals: list[float], k: int = 5) -> list[float]:
    h = k // 2
    return [float(np.median(vals[max(0, i - h) : i + h + 1])) for i in range(len(vals))]


def pan_keyframes(
    track: Sequence[tuple[float, Box | None]],
    crop_w: float,
    *,
    min_hit: float = 0.3,
    dead_zone: float = 0.15,
    max_speed: float = 0.35,
) -> tuple[tuple[float, float], ...] | None:
    """Keyframes (t, p) for the center layout; p is the crop's position along the free
    width (0 = left edge, 1 = right edge). `crop_w` = crop width / frame width. The camera
    stays still while the face is inside the middle `dead_zone` part of the crop and moves
    at most `max_speed` crop widths per second."""
    if crop_w >= 0.999 or not track:
        return None
    hits = [(t, (b[0] + b[2]) / 2) for t, b in track if b is not None]
    if len(hits) < max(2, min_hit * len(track)):
        return None
    times = [t for t, _ in track]
    centers: list[float] = []
    j = 0
    for t, b in track:  # fill gaps with the nearest detection
        if b is not None:
            centers.append((b[0] + b[2]) / 2)
            continue
        while j + 1 < len(hits) and abs(hits[j + 1][0] - t) <= abs(hits[j][0] - t):
            j += 1
        centers.append(hits[j][1])
    centers = _median_filter(centers)
    free = 1.0 - crop_w
    zone = dead_zone * crop_w
    left = min(max(centers[0] - crop_w / 2, 0.0), free)
    keys: list[tuple[float, float]] = [(times[0], left)]
    for i in range(1, len(times)):
        dt = max(times[i] - times[i - 1], 1e-3)
        off = centers[i] - (left + crop_w / 2)
        if abs(off) > zone:
            step = off - zone if off > 0 else off + zone
            lim = max_speed * crop_w * dt
            left = min(max(left + max(-lim, min(lim, step)), 0.0), free)
        keys.append((times[i], left))
    pts = [(t, round(x / free, 4)) for t, x in keys]
    # Drop points on a straight line between their neighbours.
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        (t0, p0), (t1, p1), (t2, p2) = out[-1], pts[i], pts[i + 1]
        if abs(p0 + (p2 - p0) * (t1 - t0) / max(t2 - t0, 1e-6) - p1) > 0.004:
            out.append(pts[i])
    out.append(pts[-1])
    if all(abs(p - out[0][1]) < 1e-3 for _, p in out):
        out = [out[0]]
    return tuple(out)


def pan_expr(keys: Sequence[tuple[float, float]]) -> str:
    """Piecewise-linear position of `t` without nesting: p0 + sum of clipped ramps."""

    def f(v: float) -> str:
        return f"{v:.4f}".rstrip("0").rstrip(".") or "0"

    expr = f(keys[0][1])
    for (t0, p0), (t1, p1) in itertools.pairwise(keys):
        if abs(p1 - p0) < 1e-6:
            continue
        expr += f"+{f(p1 - p0)}*clip((t-{f(t0)})/{f(max(t1 - t0, 1e-3))},0,1)"
    return expr


def cam_crop(
    track: Sequence[tuple[float, Box | None]],
    facecam: Rect,
    src: tuple[int, int],
    panel_aspect: float,
    *,
    min_hit: float = 0.4,
    face_scale: float = 2.8,
) -> Rect | None:
    """Static crop inside `facecam` (frame-normalised) around the median face, with the
    top panel's aspect (width / height in pixels). Boxes are normalised to the facecam."""
    boxes = [b for _, b in track if b is not None]
    if not track or len(boxes) < min_hit * len(track):
        return None
    rw, rh = facecam.w * src[0], facecam.h * src[1]
    cx = float(np.median([(b[0] + b[2]) / 2 for b in boxes])) * rw
    cy = float(np.median([(b[1] + b[3]) / 2 for b in boxes])) * rh
    fh = float(np.median([b[3] - b[1] for b in boxes])) * rh
    max_h = min(rh, rw / panel_aspect)
    ch = min(max(fh * face_scale, 0.55 * max_h), max_h)
    cw = ch * panel_aspect
    x = min(max(cx - cw / 2, 0.0), rw - cw)
    y = min(max(cy - 0.45 * ch, 0.0), rh - ch)
    fx, fy = facecam.x + x / src[0], facecam.y + y / src[1]
    fw, fhn = min(cw / src[0], 1 - fx), min(ch / src[1], 1 - fy)
    return Rect(round(fx, 4), round(fy, 4), round(fw, 4), round(fhn, 4))


# -- entry point --------------------------------------------------------------------------


def _analyse(
    detector: Detector,
    video: Path,
    start: float,
    end: float,
    fps: float,
    layout: str,
    facecam: Rect | None,
    panel_aspect: float,
) -> FacePlan | None:
    src, frames = sample_frames(video, start, end, fps, 960 if layout == "stack" else 640)
    dets: list[tuple[float, list[Box]]] = []
    for t, rgb in frames:
        img = crop_region(rgb, facecam) if layout == "stack" and facecam else rgb
        dets.append((t, detector(img)))
    track = pick_track(dets)
    if layout == "stack" and facecam is not None:
        cam = cam_crop(track, facecam, src, panel_aspect)
        return FacePlan("cam", cam=cam) if cam else None
    if layout == "center":
        crop_w = min(1.0, src[1] * 9 / 16 / max(src[0], 1))
        keys = pan_keyframes(track, crop_w)
        return FacePlan("pan", pan=keys) if keys else None
    return None


async def plan_face(
    detector: Detector,
    video: Path,
    start: float,
    end: float,
    *,
    layout: str,
    facecam: Rect | None,
    fps: float = 2.0,
    panel_aspect: float = 1080 / 608,
) -> FacePlan | None:
    if layout not in ("center", "stack") or (layout == "stack" and facecam is None):
        return None
    return await asyncio.to_thread(
        _analyse, detector, video, start, end, fps, layout, facecam, panel_aspect
    )
