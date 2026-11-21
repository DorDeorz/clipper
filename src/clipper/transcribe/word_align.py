"""Word timing refinement in the style of stable-ts `suppress_silence` (without its PyTorch
dependency): whisper's word boundaries often start inside the silence before a word or run
into the pause after it, which makes captions pop up early and linger. Here a loudness
mask of the WAV marks the non-speech parts; a word start inside silence moves forward to
where the voice begins, an end inside silence moves back to where it stops."""

from __future__ import annotations

import itertools
import wave
from pathlib import Path
from typing import Any

import numpy as np

FRAME = 0.01  # seconds per loudness frame
MIN_WORD = 0.08  # never shrink a word below this
MIN_SILENCE = 0.06  # shorter dips are not pauses


def load_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("sadece 16 bit PCM WAV")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def silence_mask(samples: np.ndarray, sr: int, frame: float = FRAME) -> np.ndarray:
    """True per `frame` where there is no speech-level sound. The threshold adapts to the
    clip: 30 dB under its loud frames but at least 8 dB over its quiet floor."""
    n = max(1, round(sr * frame))
    cnt = len(samples) // n
    if cnt == 0:
        return np.zeros(0, dtype=bool)
    rms = np.sqrt(np.mean(samples[: cnt * n].reshape(cnt, n) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    floor, loud = float(np.percentile(db, 10)), float(np.percentile(db, 95))
    thr = max(loud - 30.0, floor + 8.0)
    if loud - floor < 10.0:  # flat audio (constant noise / music): no usable pauses
        return np.zeros(cnt, dtype=bool)
    mask = db < thr
    # Drop silent runs shorter than MIN_SILENCE.
    min_run = max(1, round(MIN_SILENCE / frame))
    i = 0
    while i < cnt:
        if mask[i]:
            j = i
            while j < cnt and mask[j]:
                j += 1
            if j - i < min_run:
                mask[i:j] = False
            i = j
        else:
            i += 1
    return mask


def refine_words(
    segments: list[dict[str, Any]], mask: np.ndarray, frame: float = FRAME
) -> list[dict[str, Any]]:
    """Adjust `s`/`e` of each word ({"w","s","e","p"}) in place-free fashion; segment
    start/end follow their words."""
    n = len(mask)
    if n == 0 or not mask.any():
        return segments

    def idx(t: float) -> int:
        return min(max(int(t / frame), 0), n - 1)

    out: list[dict[str, Any]] = []
    for seg in segments:
        words = []
        for w in seg.get("words", []):
            s, e = float(w["s"]), float(w["e"])
            i = idx(s)
            if mask[i]:  # start in silence: move to the first speech frame
                j = i
                while j < n and mask[j]:
                    j += 1
                ns = j * frame
                if ns <= e - MIN_WORD:
                    s = ns
            k = idx(max(e - 1e-6, 0.0))
            if mask[k]:  # end in silence: move back to the last speech frame
                j = k
                while j > 0 and mask[j]:
                    j -= 1
                ne = (j + 1) * frame
                if ne >= s + MIN_WORD:
                    e = ne
            words.append({**w, "s": round(s, 3), "e": round(e, 3)})
        for a, b in itertools.pairwise(words):  # keep words in order
            if b["s"] < a["s"]:
                b["s"] = a["s"]
            if a["e"] > b["s"] and b["s"] > a["s"]:
                a["e"] = b["s"]
        new = dict(seg)
        new["words"] = words
        if words:
            new["start"], new["end"] = words[0]["s"], max(x["e"] for x in words)
        out.append(new)
    return out


def refine_from_wav(segments: list[dict[str, Any]], wav: Path) -> list[dict[str, Any]]:
    samples, sr = load_wav_mono(wav)
    return refine_words(segments, silence_mask(samples, sr))
