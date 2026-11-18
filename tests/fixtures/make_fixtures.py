"""Generate test fixtures with ffmpeg (no binaries are committed).

- `video.mp4`: 1920x1080 testsrc2 + sine tone (default 180 s).
- `chat.jsonl`: synthetic chat, flat traffic with 3 clear spikes.

Usage: uv run python tests/fixtures/make_fixtures.py [--out DIR] [--seconds N]
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent / "out"
BASE_TS = 1_700_000_000.0


@dataclass(frozen=True, slots=True)
class Spike:
    at: float  # seconds from start
    duration: float
    rate: float  # messages per second during the spike
    texts: tuple[str, ...]


DEFAULT_SPIKES: tuple[Spike, ...] = (
    Spike(45.0, 8.0, 14.0, ("KEKW", "KEKW KEKW", "ahahahaha", "sjsjsjsj", "LUL")),
    Spike(100.0, 8.0, 16.0, ("oha", "ohaaa", "yok artık", "PogChamp", "nasıl ya")),
    Spike(150.0, 8.0, 15.0, ("clip", "kliple", "klip", "clip it", "efsane")),
)
FLAT_TEXTS = (
    "selam",
    "naber",
    "bu oyun güzel",
    "hangi build",
    "gg",
    "sa",
    "ne zaman bitecek",
    "müzik ne",
    "ilk",
    "abi bunu dene",
    "heh",
    "olur",
    "evet",
    "hayır",
)


def make_video(path: Path, seconds: int, ffmpeg: str = "ffmpeg") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "35",
        "-g",
        "60",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True)
    return path


def iter_chat(
    seconds: int,
    spikes: tuple[Spike, ...] = DEFAULT_SPIKES,
    base_rate: float = 1.0,
    seed: int = 1234,
    base_ts: float = BASE_TS,
) -> Iterator[dict[str, object]]:
    rng = random.Random(seed)
    users = [f"u{i:03d}" for i in range(200)]
    events: list[dict[str, object]] = []
    for sec in range(seconds):
        n = _poisson(rng, base_rate)
        for spike in spikes:
            if spike.at <= sec < spike.at + spike.duration:
                n += _poisson(rng, spike.rate)
                for _ in range(n):
                    events.append(
                        _msg(
                            rng,
                            base_ts + sec + rng.random(),
                            rng.choice(users),
                            rng.choice(spike.texts),
                        )
                    )
                n = 0
        for _ in range(n):
            events.append(
                _msg(rng, base_ts + sec + rng.random(), rng.choice(users), rng.choice(FLAT_TEXTS))
            )
    events.sort(key=lambda e: float(e["ts"]))  # type: ignore[arg-type]
    yield from events


def _msg(rng: random.Random, ts: float, user: str, text: str) -> dict[str, object]:
    emotes = [w for w in text.split() if w in {"KEKW", "LUL", "PogChamp"}]
    return {
        "ts": round(ts, 3),
        "user_id": user,
        "text": text,
        "emotes": emotes,
        "platform": "local",
    }


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth's algorithm; fine for the small lambdas used here.
    limit, k, p = pow(2.718281828459045, -lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def make_chat(path: Path, seconds: int, **kwargs: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for event in iter_chat(seconds, **kwargs):  # type: ignore[arg-type]
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Test fixture'larını üretir.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    video = make_video(args.out / "video.mp4", args.seconds, args.ffmpeg)
    chat = make_chat(args.out / "chat.jsonl", args.seconds)
    print(f"Video: {video}\nChat:  {chat}")


if __name__ == "__main__":
    main()
