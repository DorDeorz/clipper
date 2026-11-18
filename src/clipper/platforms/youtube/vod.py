"""YouTube VOD video download through yt-dlp (the only place yt-dlp is used)."""

from __future__ import annotations

from pathlib import Path

from clipper.config import YouTubeConfig
from clipper.ffmpeg_probe import run_tool, which


class YtDlpError(Exception):
    pass


async def download_video(
    cfg: YouTubeConfig, video_id: str, out_dir: Path, ffmpeg: str = "ffmpeg"
) -> Path:
    exe = which(cfg.yt_dlp)
    if exe is None:
        raise YtDlpError(
            "yt-dlp bulunamadı. Kur: `uv tool install yt-dlp` (veya config: youtube.yt_dlp)."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "video.mp4"
    args = [
        str(exe),
        "--no-playlist",
        "--no-progress",
        "-f",
        cfg.vod_format,
        "--merge-output-format",
        "mp4",
        "-o",
        str(target),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    ffmpeg_path = which(ffmpeg)
    if ffmpeg_path is not None:
        args[1:1] = ["--ffmpeg-location", str(ffmpeg_path)]
    res = await run_tool(*args, timeout=6 * 3600)
    if res.returncode != 0 or not target.is_file():
        raise YtDlpError(f"yt-dlp başarısız: {res.stderr.strip()[-500:]}")
    return target


async def live_hls_url(cfg: YouTubeConfig, video_id: str) -> str:
    """HLS master URL of a live video (`yt-dlp -g`); the recorder takes it from there."""
    exe = which(cfg.yt_dlp)
    if exe is None:
        raise YtDlpError("yt-dlp bulunamadı (YouTube canlı kayıt için gerekli).")
    res = await run_tool(
        str(exe), "--no-playlist", "-g", "-f", "best[protocol^=m3u8]/best",
        f"https://www.youtube.com/watch?v={video_id}", timeout=120,
    )  # fmt: skip
    urls = [ln for ln in res.stdout.splitlines() if ln.startswith("http")]
    if res.returncode != 0 or not urls:
        raise YtDlpError(f"yt-dlp canlı URL veremedi: {res.stderr.strip()[-300:]}")
    return urls[0]
