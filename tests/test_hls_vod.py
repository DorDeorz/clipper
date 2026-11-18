from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from clipper.media import probe
from clipper.recorder.hls_vod import download_hls_vod
from clipper.recorder.m3u8 import parse_media


@pytest.fixture(scope="session")
def fmp4_hls(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 6 s fMP4/CMAF HLS rendition (EXT-X-MAP init + .m4s fragments), like Twitch's
    newer VODs."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg yok")
    out = tmp_path_factory.mktemp("fmp4")
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-g", "60", "-c:a", "aac", "-shortest",
            "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init-0.mp4",
            "-hls_segment_filename", str(out / "%d.mp4"),
            str(out / "index.m3u8"),
        ],
        check=True,
    )  # fmt: skip
    return out


def test_parse_media_reads_map(fmp4_hls: Path) -> None:
    pl = parse_media((fmp4_hls / "index.m3u8").read_text(), "https://cdn.test/v/index.m3u8")
    assert pl.segments and all(s.map_url == "https://cdn.test/v/init-0.mp4" for s in pl.segments)


def test_download_fmp4_vod(fmp4_hls: Path, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        if name == "master.m3u8":
            return httpx.Response(
                200,
                text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=320x180,"
                'VIDEO="180p30"\n180p30/index.m3u8\n',
            )
        f = fmp4_hls / name
        return httpx.Response(200, content=f.read_bytes()) if f.is_file() else httpx.Response(404)

    async def go() -> Path:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await download_hls_vod(
                client, "https://cdn.test/v/master.m3u8", tmp_path, ["best"]
            )
            return res.video

    video = asyncio.run(go())
    info = asyncio.run(probe(video))
    assert 5.5 < info.duration < 6.6 and info.has_audio and info.width == 320
    assert not (tmp_path / "parts").exists() and not (tmp_path / "joined.mp4").exists()
