"""CLI helpers for Faz 4: `clipper test-render` and `clipper facecam`."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from clipper import db
from clipper.config import Config, Secrets
from clipper.ffmpeg_probe import run_tool
from clipper.media import MediaError, probe
from clipper.render.clip import render_clip
from clipper.render.ffmpeg import RenderError
from clipper.render.layouts import Rect

# A sample sentence with Turkish letters, so test renders show tr_upper and highlighting.
SAMPLE_WORDS: list[tuple[str, float, float]] = [
    ("istanbul'da", 1.0, 1.6), ("ılık", 1.7, 2.1), ("bir", 2.2, 2.4), ("akşam,", 2.5, 3.0),
    ("oha", 3.6, 4.0), ("ne", 4.1, 4.3), ("oldu", 4.4, 4.8), ("şimdi?", 4.9, 5.4),
    ("bunu", 6.2, 6.5), ("kimse", 6.6, 7.0), ("beklemiyordu", 7.1, 7.9),
    ("inanılmaz", 9.0, 9.8), ("bir", 9.9, 10.1), ("an", 10.2, 10.5),
]  # fmt: skip
SAMPLE_FACECAM = Rect(0.70, 0.62, 0.28, 0.34)


async def make_sample_video(ffmpeg: str, out: Path, seconds: float = 20.0) -> Path:
    """1920x1080 testsrc2 with a fake 'facecam' box bottom-right and a tone."""
    out.parent.mkdir(parents=True, exist_ok=True)
    r = SAMPLE_FACECAM
    res = await run_tool(
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
        "-vf", f"drawbox=x=iw*{r.x}:y=ih*{r.y}:w=iw*{r.w}:h=ih*{r.h}:color=orange@1:t=fill",
        "-af", "volume=-18dB",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out),
        timeout=300,
    )  # fmt: skip
    if res.returncode != 0:
        raise MediaError(f"örnek video üretilemedi: {res.stderr.strip()[:300]}")
    return out


async def test_render(cfg: Config, layouts: list[str], out_dir: Path) -> int:
    raw = await make_sample_video(cfg.paths.ffmpeg, out_dir / "sample_raw.mp4")
    for layout in layouts:
        try:
            res = await render_clip(
                cfg.render,
                raw,
                0.5,
                15.5,
                SAMPLE_WORDS,
                out_dir / layout,
                layout=layout,
                facecam=SAMPLE_FACECAM,
                hook="bunu izlemelisin",
                ffmpeg=cfg.paths.ffmpeg,
                fonts_dir=cfg.paths.fonts_dir,
                progress=lambda p, name=layout: print(
                    f"\r  {name}: %{p * 100:.0f}", end="", flush=True
                ),
            )
        except RenderError as exc:
            print(f"\nHata ({layout}): {exc}", file=sys.stderr)
            return 1
        info = await probe(res.final, cfg.paths.ffprobe)
        print(
            f"\r{layout}: {res.final} ({info.width}x{info.height}, {info.duration:.1f} sn,"
            f" {res.encoder})"
        )
    if not (cfg.paths.fonts_dir.is_dir() and any(cfg.paths.fonts_dir.glob("*.ttf"))):
        print(
            f"Not: {cfg.paths.fonts_dir} içinde font yok; altyazı sistem fontuyla çizildi."
            " Montserrat ExtraBold için assets/fonts/README.md'ye bak.",
            file=sys.stderr,
        )
    return 0


def _latest_session_video(conn: sqlite3.Connection, streamer: str) -> tuple[int, Path]:
    row = conn.execute(
        "SELECT s.id, g.path FROM sessions s JOIN streamers st ON st.id = s.streamer_id"
        " JOIN segments g ON g.session_id = s.id AND g.deleted = 0"
        " WHERE st.display_name = ? OR st.channel = ? ORDER BY s.id DESC, g.seq DESC LIMIT 1",
        (streamer, streamer),
    ).fetchone()
    if row is None:
        raise LookupError(f"'{streamer}' için kaydedilmiş video yok (önce `clipper vod`)")
    return int(row[0]), Path(row[1])


async def facecam(cfg: Config, secrets: Secrets, streamer: str, apply: bool) -> int:
    import anthropic

    from clipper.llm.client import LLMClient
    from clipper.llm.facecam_vision import grab_frames, preview, suggest_facecam

    if secrets.anthropic_api_key is None:
        print("Hata: ANTHROPIC_API_KEY tanımlı değil (.env).", file=sys.stderr)
        return 1
    conn = db.open_db(cfg.db_path)
    try:
        try:
            session_id, video = _latest_session_video(conn, streamer)
        except LookupError as exc:
            print(f"Hata: {exc}", file=sys.stderr)
            return 1
        info = await probe(video, cfg.paths.ffprobe)
        times = [info.duration * f for f in (0.2, 0.45, 0.7)]
        work = cfg.paths.data_dir / "facecam" / streamer
        frames = await grab_frames(cfg.paths.ffmpeg, video, times, work)
        if not frames:
            print("Hata: videodan kare alınamadı.", file=sys.stderr)
            return 1
        llm = LLMClient(
            conn,
            cfg.llm,
            anthropic.AsyncAnthropic(api_key=secrets.anthropic_api_key.get_secret_value()),
        )
        sug = await suggest_facecam(llm, frames, cfg.llm.judge_model)
        print(f"Session #{session_id}, {len(frames)} kare, ${sug.usd:.4f}")
        if sug.rect is None:
            print("Facecam bulunamadı; bu yayıncı için `blur` ya da `center` düzeni uygun.")
            for r in sug.reasons:
                print(f"  - {r}")
            return 0
        pv = await preview(cfg.paths.ffmpeg, frames[0], sug.rect, work / "preview.jpg")
        print(f"Öneri (güven {sug.confidence}): {sug.rect.to_json()}")
        print(f"Önizleme: {pv}")
        if apply:
            n = conn.execute(
                "UPDATE streamers SET facecam_rect = ?, layout = 'stack'"
                " WHERE display_name = ? OR channel = ?",
                (sug.rect.to_json(), streamer, streamer),
            ).rowcount
            print(f"Kaydedildi ({n} yayıncı): düzen 'stack'.")
        else:
            print("Uygulamak için: clipper facecam " + streamer + " --apply")
        return 0
    finally:
        conn.close()
