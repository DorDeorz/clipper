"""TESTPLAN 2.1: deterministic synthetic scenarios S01–S18 with known expected results.

Each scenario describes chat traffic, platform events, audio bursts, a fake transcript and
the ground truth for the content path. `build()` writes the files for one scenario
(audio/video segments, chat.jsonl, events.json, fake_transcript.jsonl, expected.json);
`load_session()` turns them into a clipper session in a database.

Session time is seconds from the start of the stream (like a local VOD import).
"""

from __future__ import annotations

import itertools
import json
import math
import random
import sqlite3
import subprocess
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

from clipper import chatlog
from clipper.detect.events import LiveEvent, write_events
from clipper.models import ChatMessage, Segment
from clipper.sessions import (
    CHAT_FILE,
    add_segment,
    create_session,
    finish_session,
    get_or_create_streamer,
)
from clipper.util.fs import atomic_write_text
from clipper.util.proc import no_window

SEED = 20261118
AUDIO_RATE = 8000
SEGMENT_SECONDS = 60
FACECAM_RECT = {"x": 0.75, "y": 0.0, "w": 0.25, "h": 0.25}  # the blue CAM box
LAG = 20.0  # default detector.lag_seconds: chat peaks ~20 s after what caused it

FLAT_TEXTS = (
    "selam", "naber", "bu oyun güzel", "hangi build", "gg", "sa", "ne zaman bitecek",
    "müzik ne", "abi bunu dene", "heh", "olur", "evet", "hayır", "ilk", "bugün ne oynuyoruz",
)  # fmt: skip
LAUGH = ("KEKW", "KEKW KEKW", "ahahahaha", "sjsjsjsj", "LUL", "KEKW ahahah")
SHOCK = ("oha", "ohaaa", "yok artık", "PogChamp", "nasıl ya", "OMEGALUL")
THANKS = ("hoş geldin", "teşekkürler abi", "hoşgeldin", "abone olan herkese teşekkürler")
RAID = ("raid", "RAID", "raid geldi", "raiderlar hoş geldi")


@dataclass(frozen=True)
class Burst:
    at: float  # session seconds
    duration: float
    rate: float  # extra messages per second
    texts: tuple[str, ...] = LAUGH
    users: int | None = None  # distinct senders (None: anyone from the pool)
    new_users: bool = False  # senders outside the regular pool (raid)


@dataclass(frozen=True)
class Expected:
    """`events`: one entry per expected candidate: the session time the candidate must be
    about (inside its window), the tolerance for the detector's event time, and sources
    that must be present."""

    count: int
    events: tuple[tuple[float, float, tuple[str, ...]], ...] = ()
    max_seconds: float | None = None  # every candidate's window at most this long
    no_publish: bool = False
    publish_warning: bool = False
    no_ad_frames: bool = False
    facecam_top: bool = False
    caption_text: str | None = None
    prefilter_skipped: bool = False


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    seconds: int
    expected: Expected
    base_rate: float = 1.5
    users: int = 120
    bursts: tuple[Burst, ...] = ()
    events: tuple[LiveEvent, ...] = ()
    audio: tuple[tuple[float, float, float], ...] = ()  # (at, duration, +dB)
    transcript: tuple[tuple[float, float, str], ...] = ()
    content: tuple[tuple[float, float, int, str], ...] = ()  # oracle: (a, b, score, kind)
    chat_gaps: tuple[tuple[float, float], ...] = ()
    ads: tuple[tuple[float, float], ...] = ()
    facecam: bool = False
    permission_ok: bool = True
    auto_publish: bool = False
    music_risk: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def seed(self) -> int:
        return SEED + int(self.id[1:])


def _laugh_at(event: float, rate: float = 7.5, texts: tuple[str, ...] = LAUGH) -> Burst:
    """Chat reaction to something that happened at `event` (peaks ~LAG later)."""
    return Burst(event + LAG - 6, 10, rate, texts)


def _story(a: float, b: float) -> tuple[tuple[float, float, str], ...]:
    sentences = (
        "geçen gün markete gittim kasada sıra var",
        "önümdeki amca kasiyere bir şey sordu",
        "kasiyer de dönüp bana baktı ben de ne diyeceğimi bilemedim",
        "sonra amca bana dönüp oğlum sen söyle dedi",
        "ben de dedim ki amca ben buranın çalışanı değilim",
        "meğer amca beni müdür sanıyormuş",
        "bütün market gülmeye başladı",
    )
    out: list[tuple[float, float, str]] = []
    t, i = a, 0
    while t < b:
        out.append((t, min(t + 9.0, b), sentences[i % len(sentences)]))
        t += 10.0
        i += 1
    return tuple(out)


def _chatter(a: float, b: float, every: float = 25.0) -> tuple[tuple[float, float, str], ...]:
    """Routine talk so the content path has something (low-value) to read."""
    lines = ("evet şimdi bu tarafa geçelim", "chat ne diyor bakalım", "tamam bir dakika")
    return tuple(
        (t, t + 4.0, lines[i % len(lines)]) for i, t in enumerate(np.arange(a, b, every).tolist())
    )


# fmt: off
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("S01", "Düz trafik, olay yok", 1200, Expected(0)),
    Scenario(
        "S02",
        "3 net chat patlaması + öncesinde ses",
        1200,
        Expected(3, tuple((t, 3.0, ("reactive",)) for t in (300.0, 600.0, 900.0))),
        bursts=tuple(_laugh_at(t, rate=9.0) for t in (300.0, 600.0, 900.0)),
        audio=tuple((t - 1.0, 3.0, 18.0) for t in (300.0, 600.0, 900.0)),
    ),
    Scenario(
        "S03",
        "Tek kullanıcı spam",
        600,
        Expected(0),
        bursts=(Burst(300, 10, 4.0, ("KEKW KEKW KEKW",), users=1),),
    ),
    Scenario(
        "S04",
        "Sub yağmuru + teşekkür dalgası",
        600,
        Expected(0),
        events=tuple(LiveEvent(300 + i / 3, "gift_sub", 1.0) for i in range(15)),
        bursts=(Burst(301, 25, 5.0, THANKS),),
    ),
    Scenario(
        "S05",
        "Raid",
        600,
        Expected(0),
        events=(LiveEvent(240, "raid", 400.0, {"from": "baska_kanal"}),),
        bursts=(
            Burst(240, 60, 6.0, RAID, users=400, new_users=True),
            Burst(300, 300, 0.6, FLAT_TEXTS, users=120, new_users=True),
        ),
    ),
    Scenario(
        "S06",
        "Sakin chat, komik hikâye",
        720,
        Expected(1, ((395.0, 40.0, ("content",)),)),
        transcript=_chatter(0, 355) + _story(360, 430) + _chatter(440, 720),
        content=((360, 430, 86, "story"),),
    ),
    Scenario(
        "S07",
        "İzleyici klibi + hafif chat artışı",
        720,
        Expected(1, ((495.0, 30.0, ("viewer_clip",)),)),
        events=(
            LiveEvent(
                480,
                "viewer_clip",
                None,
                {"slug": "TestClip", "duration": 30, "title": "efsane an", "views": 12},
            ),
        ),
        bursts=(Burst(495, 20, 1.5, SHOCK),),
    ),
    Scenario(
        "S08",
        "Chat + içerik + izleyici klibi aynı anda",
        600,
        Expected(
            1, ((300.0, 10.0, ("reactive", "content", "viewer_clip")),), prefilter_skipped=True
        ),
        bursts=(_laugh_at(300, rate=9.0),),
        audio=((299, 3, 18.0),),
        events=(LiveEvent(290, "viewer_clip", None, {"slug": "Ayni", "duration": 25}),),
        transcript=_chatter(0, 270) + _story(275, 335) + _chatter(345, 600),
        content=((275, 335, 88, "story"),),
    ),
    Scenario(
        "S09",
        "Küçük kanal",
        1200,
        Expected(1, ((600.0, 4.0, ("reactive",)),)),
        base_rate=0.1,
        users=15,
        bursts=(Burst(600 + LAG - 8, 14, 1.1, LAUGH, users=10),),
        audio=((599, 3, 15.0),),
    ),
    Scenario(
        "S10",
        "Dev kanal",
        600,
        Expected(1, ((420.0, 4.0, ("reactive",)),)),
        base_rate=40.0,
        users=3000,
        bursts=(_laugh_at(420, rate=80.0),),
        audio=((419, 3, 18.0),),
        tags=("perf",),
    ),
    Scenario(
        "S11",
        "20 sn arayla iki patlama",
        300,
        Expected(1, ((200.0, 25.0, ("reactive",)),)),
        bursts=(_laugh_at(190, rate=9.0), _laugh_at(210, rate=9.0)),
        audio=((189, 3, 18.0), (209, 3, 18.0)),
        base_rate=1.5,
    ),
    Scenario(
        "S12",
        "3 dk chat kopması + ses patlaması",
        600,
        Expected(0),
        chat_gaps=((240, 420),),
        audio=((330, 4, 20.0),),
    ),
    Scenario(
        "S13",
        "Aday ortasında reklam",
        600,
        Expected(1, ((300.0, 4.0, ("reactive",)),), no_ad_frames=True),
        bursts=(_laugh_at(300, rate=9.0),),
        audio=((299, 3, 18.0),),
        ads=((280, 400),),
        tags=("render",),
    ),
    # S14/S15: 6 min with one reaction so a clip is actually rendered (the plan's 2-3 min
    # are inside detector.warmup_seconds and would give no clip to look at).
    Scenario(
        "S14", "Türkçe karakterli transkript", 360,
        Expected(1, ((270.0, 4.0, ("reactive",)),),
                 caption_text="İSTANBUL'DA ILIK ŞİŞLİ ÖĞÜŞ"),
        bursts=(_laugh_at(270, rate=9.0),), audio=((269, 3, 18.0),),
        transcript=((262, 266, "İstanbul'da ılık şişli öğüş"),),
        tags=("render",),
    ),
    Scenario(
        "S15", "Facecam düzeni", 360,
        Expected(1, ((270.0, 4.0, ("reactive",)),), facecam_top=True),
        bursts=(_laugh_at(270, rate=9.0),), audio=((269, 3, 18.0),),
        facecam=True, tags=("render",),
    ),
    Scenario(
        "S16",
        "Uzun hikâye (4 dk)",
        480,
        Expected(1, ((240.0, 120.0, ("content",)),), max_seconds=180.0),
        transcript=_chatter(0, 115) + _story(120, 360) + _chatter(370, 480),
        content=((120, 360, 87, "story"),),
    ),
    # S17/S18: 6 min instead of the plan's 3 (detector.warmup_seconds is 180).
    Scenario(
        "S17",
        "Müzik riski",
        360,
        Expected(1, ((270.0, 4.0, ("reactive",)),), no_publish=True, publish_warning=True),
        bursts=(_laugh_at(270, rate=9.0),),
        audio=((269, 3, 18.0),),
        transcript=((240, 300, "la la la aşkın olayım gel bana sevgilim la la"),),
        music_risk=True,
        auto_publish=True,
    ),
    Scenario(
        "S18",
        "İzinsiz yayıncı, otomatik yayın açık",
        360,
        Expected(1, ((270.0, 4.0, ("reactive",)),), no_publish=True),
        bursts=(_laugh_at(270, rate=9.0),),
        audio=((269, 3, 18.0),),
        permission_ok=False,
        auto_publish=True,
    ),
)
# fmt: on
BY_ID = {s.id: s for s in SCENARIOS}


# ---- generation -------------------------------------------------------------------------


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:  # normal approximation keeps big channels fast
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def chat_messages(s: Scenario) -> list[ChatMessage]:
    rng = random.Random(s.seed)
    pool = [f"u{i:04d}" for i in range(s.users)]
    out: list[ChatMessage] = []

    def in_gap(t: float) -> bool:
        return any(a <= t < b for a, b in s.chat_gaps)

    for sec in range(s.seconds):
        for _ in range(_poisson(rng, s.base_rate)):
            t = sec + rng.random()
            if not in_gap(t):
                out.append(ChatMessage(t, rng.choice(pool), rng.choice(FLAT_TEXTS), [], "twitch"))
    for bi, b in enumerate(s.bursts):
        senders = (
            [f"r{bi}_{i:04d}" for i in range(b.users or s.users)]
            if b.new_users
            else (pool[: b.users] if b.users else pool)
        )
        for sec in range(int(b.at), int(b.at + b.duration)):
            for _ in range(_poisson(rng, b.rate)):
                t = sec + rng.random()
                if not in_gap(t):
                    text = rng.choice(b.texts)
                    emotes = [w for w in text.split() if w.isupper() and len(w) > 2]
                    out.append(ChatMessage(t, rng.choice(senders), text, emotes, "twitch"))
    out.sort(key=lambda m: m.ts)
    return out


def audio_samples(s: Scenario) -> np.ndarray:
    rng = np.random.default_rng(s.seed)
    n = s.seconds * AUDIO_RATE
    noise = rng.normal(0.0, 0.02, n)  # about -37 dBFS background
    t = np.arange(n) / AUDIO_RATE
    for at, dur, boost in s.audio:
        a, b = int(at * AUDIO_RATE), int(min(at + dur, s.seconds) * AUDIO_RATE)
        amp = 0.02 * 10 ** (boost / 20.0) * math.sqrt(2)
        noise[a:b] += amp * np.sin(2 * math.pi * 880.0 * t[a:b])
    return np.clip(noise, -1.0, 1.0)


def _write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes((samples * 32767).astype("<i2").tobytes())


def _segment_bounds(s: Scenario) -> list[tuple[float, float, bool]]:
    """(start, duration, is_ad) covering the whole scenario; ads get their own segments."""
    cuts = {0.0, float(s.seconds)}
    for a, b in s.ads:
        cuts |= {float(a), float(b)}
    t = 0.0
    while t < s.seconds:
        cuts.add(t)
        t += SEGMENT_SECONDS
    pts = sorted(c for c in cuts if 0 <= c <= s.seconds)
    out = []
    for a, b in itertools.pairwise(pts):
        is_ad = any(x <= a and b <= y for x, y in s.ads)
        out.append((a, b - a, is_ad))
    return out


def _video_segment(
    path: Path, wav: Path, a: float, dur: float, is_ad: bool, facecam: bool, ffmpeg: str
) -> None:
    """1920x1080 30 fps testsrc2 (red frames for ads, a blue 'CAM' box top-right for the
    facecam layout) with the scenario audio."""
    src = (
        f"color=c=red:size=1920x1080:rate=30:duration={dur:.3f}"
        if is_ad
        else f"testsrc2=size=1920x1080:rate=30:duration={dur:.3f}"
    )
    vf = "drawbox=x=1440:y=0:w=480:h=270:color=blue@1:t=fill" if facecam and not is_ad else "null"
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", src,
        "-ss", f"{a:.3f}", "-t", f"{dur:.3f}", "-i", str(wav),
        "-vf", vf, "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-g", "30",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-b:a", "64k",
        "-f", "mpegts", str(path),
    ]  # fmt: skip
    subprocess.run(cmd, check=True, **no_window())


def build(s: Scenario, out_dir: Path, *, video: bool = False, ffmpeg: str = "ffmpeg") -> Path:
    """Write a scenario's inputs to `out_dir/<id>/`. Audio-only segments are enough for the
    detector (DET tests); `video=True` makes real 1080p segments for the full pipeline."""
    d = out_dir / s.id
    d.mkdir(parents=True, exist_ok=True)
    full = d / "audio.wav"
    _write_wav(full, audio_samples(s))
    segs = []
    for i, (a, dur, is_ad) in enumerate(_segment_bounds(s)):
        if video:
            p = d / "seg" / f"{i:04d}.ts"
            if not p.is_file():
                _video_segment(p, full, a, dur, is_ad, s.facecam, ffmpeg)
        else:
            p = d / "seg" / f"{i:04d}.wav"
            lo, hi = int(a * AUDIO_RATE), int((a + dur) * AUDIO_RATE)
            _write_wav(p, audio_samples(s)[lo:hi] if not is_ad else np.zeros(hi - lo))
        segs.append({"seq": i, "path": str(p), "start": a, "duration": dur, "is_ad": is_ad})
    chatlog.write_jsonl(d / "chat.jsonl", chat_messages(s))
    atomic_write_text(d / "segments.json", json.dumps(segs))
    atomic_write_text(d / "events.json", json.dumps([e.to_json() for e in s.events]))
    atomic_write_text(
        d / "fake_transcript.jsonl",
        "".join(
            json.dumps({"start": a, "end": b, "text": t}, ensure_ascii=False) + "\n"
            for a, b, t in s.transcript
        ),
    )
    atomic_write_text(d / "expected.json", json.dumps(expected_json(s), ensure_ascii=False))
    return d


def expected_json(s: Scenario) -> dict[str, Any]:
    e = asdict(s.expected)
    e["id"], e["title"] = s.id, s.title
    e["events"] = [
        {"at": at, "tol": tol, "sources": list(src)} for at, tol, src in s.expected.events
    ]
    return e


def write_transcript(session_dir: Path, s: Scenario) -> None:
    """The session transcript file as the batch transcribe step would write it."""
    from clipper.content.transcript import TRANSCRIPT_FILE

    lines = []
    for a, b, text in s.transcript:
        toks = text.split()
        step = (b - a) / max(len(toks), 1)
        words = [
            {"w": w, "s": round(a + i * step, 3), "e": round(a + (i + 1) * step, 3), "p": 1.0}
            for i, w in enumerate(toks)
        ]
        lines.append({"t0": a, "t1": b, "text": text, "words": words})
    atomic_write_text(
        session_dir / TRANSCRIPT_FILE,
        "".join(json.dumps(ln, ensure_ascii=False) + "\n" for ln in lines),
    )


def load_session(
    conn: sqlite3.Connection,
    data_dir: Path,
    s: Scenario,
    built: Path,
    *,
    with_transcript: bool = True,
) -> int:
    """Create a finished VOD session for the scenario. Returns the session id."""
    sid = get_or_create_streamer(conn, f"sentetik_{s.id.lower()}", "twitch", s.id.lower())
    conn.execute(
        "UPDATE streamers SET permission_ok = ?, auto_publish = ?, publish_targets = ?"
        " WHERE id = ?",
        (int(s.permission_ok), int(s.auto_publish),
         json.dumps(["youtube", "x"] if s.auto_publish else []), sid),
    )  # fmt: skip
    if s.facecam:
        conn.execute(
            "UPDATE streamers SET layout = 'stack', facecam_rect = ? WHERE id = ?",
            (json.dumps(FACECAM_RECT), sid),
        )
    session_id, session_dir = create_session(conn, data_dir, sid, "vod", title=s.title)
    for seg in json.loads((built / "segments.json").read_text(encoding="utf-8")):
        add_segment(
            conn,
            Segment(session_id, seg["seq"], seg["path"], seg["start"], seg["duration"],
                    seg["is_ad"]),
        )  # fmt: skip
    chatlog.write_jsonl(session_dir / CHAT_FILE, chatlog.read_jsonl(built / "chat.jsonl"))
    events = [LiveEvent.from_json(e) for e in json.loads((built / "events.json").read_text())]
    write_events(conn, session_id, events)
    if with_transcript and s.transcript:
        write_transcript(session_dir, s)
    finish_session(conn, session_id)
    return session_id


Mode = Literal["detector", "full"]
