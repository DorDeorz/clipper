"""Face tracking crop, DeepFilterNet denoise wrapper, word timing refinement, pinned
downloads. All offline: a fake detector, a fake deep-filter and httpx.MockTransport."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest

from clipper.config import RenderConfig
from clipper.denoise import denoise_clip
from clipper.media import probe
from clipper.render.clip import render_clip
from clipper.render.facetrack import (
    FacePlan,
    cam_crop,
    nms,
    pan_expr,
    pan_keyframes,
    pick_track,
    plan_face,
)
from clipper.render.layouts import Rect, filter_graph
from clipper.transcribe.word_align import refine_from_wav, refine_words, silence_mask
from clipper.util.download import DownloadError, ensure_pinned

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")
FAKE_DF = Path(__file__).parent / "fakes" / "fake_deep_filter.py"


def _box(cx: float, cy: float, s: float = 0.2, score: float = 0.9):
    return (cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2, score)


@pytest.fixture(scope="module")
def video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg yok")
    out = tmp_path_factory.mktemp("face") / "raw.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=8", "-af", "volume=-20dB",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac", "-shortest",
         str(out)],
        check=True,
    )  # fmt: skip
    return out


# -- planning -----------------------------------------------------------------------------


def test_pick_track_follows_nearest_face() -> None:
    dets = [
        (0.0, [_box(0.2, 0.5, 0.3), _box(0.8, 0.5, 0.1)]),
        (0.5, []),
        (1.0, [_box(0.8, 0.5, 0.1), _box(0.25, 0.5, 0.25)]),
    ]
    track = pick_track(dets)
    assert track[1][1] is None
    b = track[2][1]
    assert b is not None and abs((b[0] + b[2]) / 2 - 0.25) < 1e-6


def test_pan_follows_face_left_to_right() -> None:
    crop_w = 0.316  # 720p: 405 / 1280
    track = [(i * 0.5, _box(0.15 + 0.7 * i / 19, 0.4)) for i in range(20)]
    keys = pan_keyframes(track, crop_w)
    assert keys is not None and len(keys) >= 2
    ps = [p for _, p in keys]
    assert ps == sorted(ps) and 0.0 <= ps[0] < 0.2 and ps[-1] > 0.8
    assert all(0.0 <= p <= 1.0 for p in ps)


def test_pan_still_face_is_one_keyframe_and_no_face_is_none() -> None:
    still = [(i * 0.5, _box(0.6 + (0.01 if i % 2 else 0), 0.4)) for i in range(10)]
    keys = pan_keyframes(still, 0.316)
    assert keys is not None and len(keys) == 1
    assert pan_keyframes([(i * 0.5, None) for i in range(10)], 0.316) is None
    assert pan_keyframes(still, 1.0) is None  # source already 9:16


def test_pan_speed_is_limited() -> None:
    track = [(0.0, _box(0.1, 0.4))] + [(0.5 * i, _box(0.9, 0.4)) for i in range(1, 8)]
    keys = pan_keyframes(track, 0.316, max_speed=0.35)
    assert keys is not None
    free = 1 - 0.316
    for (t0, p0), (t1, p1) in itertools.pairwise(keys):
        assert abs(p1 - p0) * free <= 0.35 * 0.316 * (t1 - t0) + 1e-3


def test_pan_expr_and_graph() -> None:
    expr = pan_expr(((0.0, 0.2), (2.0, 0.2), (4.0, 0.6)))
    assert expr == "0.2+0.4*clip((t-2)/2,0,1)"
    g = filter_graph("center", None, ass_name=None, fontsdir=None, pan=expr)
    assert "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)':'(iw-ow)*(0.2+0.4*clip(" in g


def test_cam_crop_centres_face_inside_facecam() -> None:
    facecam = Rect(0.7, 0.4, 0.3, 0.6)  # tall webcam box on a 1920x1080 frame
    track = [(i * 0.5, _box(0.5, 0.3, 0.25)) for i in range(6)]
    aspect = 1080 / 608
    r = cam_crop(track, facecam, (1920, 1080), aspect)
    assert r is not None
    assert r.x >= facecam.x - 1e-3 and r.x + r.w <= facecam.x + facecam.w + 1e-3
    assert r.y >= facecam.y - 1e-3 and r.y + r.h <= facecam.y + facecam.h + 1e-3
    assert abs((r.w * 1920) / (r.h * 1080) - aspect) < 0.02
    face_y = facecam.y + 0.3 * facecam.h
    assert r.y < face_y < r.y + r.h
    assert cam_crop([(0.0, None)] * 5, facecam, (1920, 1080), aspect) is None


def test_nms_keeps_best_of_overlaps() -> None:
    b = np.array([[0, 0, 0.5, 0.5], [0.01, 0.01, 0.5, 0.5], [0.6, 0.6, 0.9, 0.9]], np.float32)
    s = np.array([0.8, 0.95, 0.9], np.float32)
    assert nms(b, s) == [1, 2]


# -- with real frames ---------------------------------------------------------------------


@needs_ffmpeg
def test_plan_face_samples_frames_and_pans(video: Path) -> None:
    seen: list[tuple[int, ...]] = []

    def detector(rgb: np.ndarray) -> list:
        seen.append(rgb.shape)
        return [_box(0.15 + 0.1 * len(seen), 0.4)]

    plan = asyncio.run(plan_face(detector, video, 1.0, 6.0, layout="center", facecam=None))
    assert plan is not None and plan.kind == "pan" and len(plan.pan) >= 2
    assert 9 <= len(seen) <= 12  # 2 fps over 5 s
    assert seen[0][1] == 640

    seen.clear()
    plan = asyncio.run(
        plan_face(detector, video, 1.0, 6.0, layout="stack", facecam=Rect(0.6, 0.0, 0.4, 0.5))
    )
    assert plan is not None and plan.kind == "cam" and plan.cam is not None
    assert seen[0][1] == round(960 * 0.4)  # detector sees only the facecam region
    assert asyncio.run(plan_face(detector, video, 1, 6, layout="blur", facecam=None)) is None


@needs_ffmpeg
def test_render_with_pan_and_clean_audio(video: Path, tmp_path: Path) -> None:
    out = tmp_path / "clip"
    clean = tmp_path / "clean.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=500:duration=8", "-ac", "2", "-ar", "48000", str(clean)],
        check=True,
    )  # fmt: skip
    plan = FacePlan("pan", pan=((0.0, 0.0), (3.0, 1.0)))
    res = asyncio.run(
        render_clip(
            RenderConfig(encoder="libx264"), video, 1.0, 5.0, [("oha", 1.5, 2.0)], out,
            layout="center", face=plan, clean_audio=clean,
        )
    )  # fmt: skip
    info = asyncio.run(probe(res.final))
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert abs(info.duration - 4.0) <= 0.2


# -- denoise ------------------------------------------------------------------------------


@needs_ffmpeg
def test_denoise_clip_with_fake_deep_filter(video: Path, tmp_path: Path) -> None:
    raw = tmp_path / "raw.mp4"
    shutil.copy(video, raw)
    clean, clean16 = asyncio.run(denoise_clip(raw, [sys.executable, str(FAKE_DF)]))
    with wave.open(str(clean), "rb") as w:
        assert (w.getframerate(), w.getnchannels()) == (48000, 2)
        assert abs(w.getnframes() / 48000 - 8.0) < 0.2
    with wave.open(str(clean16), "rb") as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)
    assert not list(tmp_path.glob(".df-*"))


@needs_ffmpeg
def test_denoise_failure_raises(video: Path, tmp_path: Path) -> None:
    from clipper.denoise import DenoiseError

    raw = tmp_path / "raw.mp4"
    shutil.copy(video, raw)
    with pytest.raises(DenoiseError):
        asyncio.run(denoise_clip(raw, [sys.executable, "-c", "import sys; sys.exit(3)"]))


# -- word timing --------------------------------------------------------------------------


def _speech_wav(path: Path, spans: list[tuple[float, float]], dur: float = 4.0) -> None:
    sr = 16000
    t = np.arange(int(dur * sr)) / sr
    a = np.random.default_rng(1).normal(0, 0.0005, t.size)
    for s, e in spans:
        m = (t >= s) & (t < e)
        a[m] += 0.3 * np.sin(2 * np.pi * 220 * t[m])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())


def test_refine_moves_word_edges_out_of_silence(tmp_path: Path) -> None:
    wav = tmp_path / "a.wav"
    _speech_wav(wav, [(1.0, 1.6), (2.0, 2.5)])
    segs = [{"start": 0.6, "end": 2.9, "text": "oha bak",
             "words": [{"w": "oha", "s": 0.6, "e": 1.9, "p": 0.9},
                       {"w": "bak", "s": 1.95, "e": 2.9, "p": 0.9}]}]  # fmt: skip
    out = refine_from_wav(segs, wav)
    w1, w2 = out[0]["words"]
    assert w1["s"] == pytest.approx(1.0, abs=0.03) and w1["e"] == pytest.approx(1.6, abs=0.03)
    assert w2["s"] == pytest.approx(2.0, abs=0.03) and w2["e"] == pytest.approx(2.5, abs=0.03)
    assert out[0]["start"] == w1["s"] and out[0]["end"] == w2["e"]
    assert segs[0]["words"][0]["s"] == 0.6  # input untouched


def test_refine_leaves_words_in_speech_and_flat_audio_alone(tmp_path: Path) -> None:
    wav = tmp_path / "a.wav"
    _speech_wav(wav, [(0.0, 4.0)])
    segs = [{"start": 0.5, "end": 1.0, "text": "x", "words": [{"w": "x", "s": 0.5, "e": 1.0}]}]
    assert refine_from_wav(segs, wav)[0]["words"][0] == {"w": "x", "s": 0.5, "e": 1.0}
    assert not silence_mask(np.zeros(16000, np.float32), 16000).any()
    # A word fully inside silence keeps a minimum length instead of collapsing.
    mask = np.zeros(400, bool)
    mask[100:200] = True
    out = refine_words([{"words": [{"w": "y", "s": 1.2, "e": 1.5}]}], mask)
    w = out[0]["words"][0]
    assert w["e"] - w["s"] >= 0.08


# -- pinned downloads ---------------------------------------------------------------------


def test_ensure_pinned(tmp_path: Path) -> None:
    body = b"model-bytes"
    sha = hashlib.sha256(body).hexdigest()
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, content=body)

    t = httpx.MockTransport(handler)
    dest = tmp_path / "m" / "f.onnx"
    assert asyncio.run(ensure_pinned("https://x.test/f", sha, dest, transport=t)) == dest
    assert dest.read_bytes() == body
    asyncio.run(ensure_pinned("https://x.test/f", sha, dest, transport=t))
    assert len(calls) == 1  # already there with the right hash
    with pytest.raises(DownloadError):
        asyncio.run(ensure_pinned("https://x.test/g", "0" * 64, tmp_path / "g", transport=t))
    assert not (tmp_path / "g").exists()
