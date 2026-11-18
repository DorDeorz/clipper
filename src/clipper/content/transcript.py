"""Session transcript (`live_transcript.jsonl`) and the cheap signals derived from it.

Each line: `{"t0": .., "t1": .., "text": "..", "words": [{"w", "s", "e", "p"}]}` with
times in the session time base (same as chat and segments). Written by the live
transcriber (Faz 6) or by the batch VOD transcription; read by the content scanner,
the detector (speech rate, streamer keywords) and the clip transcribe stage.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipper.detect.keywords_tr import KeywordScorer

TRANSCRIPT_FILE = "live_transcript.jsonl"


@dataclass(frozen=True, slots=True)
class TWord:
    w: str
    s: float
    e: float
    p: float = 1.0


@dataclass(frozen=True, slots=True)
class TLine:
    t0: float
    t1: float
    text: str
    words: tuple[TWord, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        return {
            "t0": round(self.t0, 3),
            "t1": round(self.t1, 3),
            "text": self.text,
            "words": [
                {"w": w.w, "s": round(w.s, 3), "e": round(w.e, 3), "p": round(w.p, 3)}
                for w in self.words
            ],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TLine:
        words = tuple(
            TWord(str(w["w"]), float(w["s"]), float(w["e"]), float(w.get("p", 1.0)))
            for w in d.get("words") or []
        )
        return cls(float(d["t0"]), float(d["t1"]), str(d.get("text", "")), words)


def from_whisper(segments: Iterable[dict[str, Any]], offset: float = 0.0) -> list[TLine]:
    """Convert `Transcriber` output (start/end/text/words with w/s/e/p) to lines."""
    out: list[TLine] = []
    for seg in segments:
        words = tuple(
            TWord(str(w["w"]), float(w["s"]) + offset, float(w["e"]) + offset, float(w["p"]))
            for w in seg.get("words") or []
        )
        out.append(
            TLine(float(seg["start"]) + offset, float(seg["end"]) + offset, str(seg["text"]), words)
        )
    return out


def append_lines(path: Path, lines: Iterable[TLine]) -> int:
    n = 0
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(json.dumps(line.to_json(), ensure_ascii=False) + "\n")
            n += 1
    return n


def read_lines(path: Path) -> list[TLine]:
    if not path.is_file():
        return []
    out: list[TLine] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                out.append(TLine.from_json(json.loads(raw)))
    out.sort(key=lambda ln: ln.t0)
    return out


def words_in(lines: Sequence[TLine], a: float, b: float) -> Iterator[TWord]:
    for ln in lines:
        if ln.t1 < a or ln.t0 > b:
            continue
        for w in ln.words:
            if a <= w.s < b:
                yield w


def speech_ratio(lines: Sequence[TLine], a: float, b: float) -> float:
    """Fraction of [a, b) covered by spoken words (union of word spans)."""
    if b <= a:
        return 0.0
    spans = sorted((max(w.s, a), min(w.e, b)) for w in words_in(lines, a, b))
    covered = 0.0
    cur_s = cur_e = -math.inf
    for s, e in spans:
        if e <= s:
            continue
        if s > cur_e:
            if cur_e > cur_s:
                covered += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e > cur_s:
        covered += cur_e - cur_s
    return min(1.0, covered / (b - a))


def window_text(lines: Sequence[TLine], a: float, b: float) -> str:
    """Transcript of [a, b) as `[+12.3] text` lines, offsets relative to `a`. Lines are
    rebuilt from the words inside the window so text outside it does not leak in."""
    out: list[str] = []
    for ln in lines:
        if ln.t1 < a or ln.t0 > b:
            continue
        ws = [w for w in ln.words if a <= w.s < b]
        if ws:
            out.append(f"[+{ws[0].s - a:.1f}] " + " ".join(w.w for w in ws))
        elif not ln.words and a <= ln.t0 < b and ln.text.strip():
            out.append(f"[+{ln.t0 - a:.1f}] {ln.text.strip()}")
    return "\n".join(out)


def speech_rate(lines: Sequence[TLine]) -> dict[int, float]:
    """Words per second, by second."""
    out: dict[int, float] = {}
    for ln in lines:
        for w in ln.words:
            sec = math.floor(w.s)
            out[sec] = out.get(sec, 0.0) + 1.0
    return out


def voice_keywords(lines: Sequence[TLine], scorer: KeywordScorer) -> dict[int, float]:
    """Keyword score of what the streamer says (shock / laugh words), by second of the
    first word of each line."""
    out: dict[int, float] = {}
    for ln in lines:
        score, _ = scorer.score(ln.text)
        if score > 0:
            sec = math.floor(ln.words[0].s if ln.words else ln.t0)
            out[sec] = out.get(sec, 0.0) + score
    return out


def covered_seconds(lines: Sequence[TLine]) -> tuple[float, float] | None:
    if not lines:
        return None
    return min(ln.t0 for ln in lines), max(ln.t1 for ln in lines)
