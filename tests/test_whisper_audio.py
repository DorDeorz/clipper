"""The real faster-whisper audio decoder must work with the installed PyAV.

Unit tests use a fake transcriber, so a PyAV release that breaks faster-whisper's
`av.open(...)` call (PyAV 19 dropped `metadata_errors`) only showed up on a real VOD.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from clipper.util.proc import no_window


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")
def test_decode_audio_with_installed_pyav(tmp_path: Path) -> None:
    from faster_whisper.audio import decode_audio

    clip = tmp_path / "tone.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=1", "-c:a", "aac", str(clip)],
        check=True, **no_window(),
    )  # fmt: skip
    audio = decode_audio(str(clip))
    assert 15_000 < len(audio) < 17_500  # ~1 s at 16 kHz
