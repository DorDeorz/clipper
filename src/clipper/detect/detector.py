"""Highlight detector (ROADMAP 4.3). Same class for streaming (live, `feed()` each second)
and batch (VOD: feed every second, then `flush()`)."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from clipper.config import DetectorConfig

FEATURES = ("rate", "uniq", "emote", "audio")
Z_MIN, Z_MAX = -2.0, 10.0


@dataclass(frozen=True, slots=True)
class Observation:
    t: int
    msgs: int = 0
    uniq: int = 0
    emote: float = 0.0
    audio_db: float | None = None
    chat_ok: bool = True  # False while the chat connection is down
    threshold_mult: float = 1.0  # r3 event filter: >1 after a sub train
    suppressed: bool = False  # r3 event filter: reactive trigger closed (raid)


@dataclass(slots=True)
class Detection:
    peak_ts: float
    event_ts: float
    win_start: float
    win_end: float
    det_score: float
    features: dict[str, Any] = field(default_factory=dict)
    merged: int = 1


@dataclass(slots=True)
class ScoreRow:
    t: int
    score: float
    z: dict[str, float]
    x: dict[str, float]


class _Ewma:
    __slots__ = ("alpha", "mean", "n", "var")

    def __init__(self, halflife: float) -> None:
        self.alpha = 1.0 - 0.5 ** (1.0 / max(halflife, 1e-9))
        self.mean = 0.0
        self.var = 0.0
        self.n = 0

    def update(self, x: float, *, slow: bool = False) -> None:
        self.n += 1
        # Until enough samples exist, behave like a plain running mean/variance;
        # a fixed small alpha from a cold start would stay stuck near the first values.
        # `slow` (raid adaptation) always uses the half-life rate.
        a = self.alpha if slow else max(self.alpha, 1.0 / self.n)
        d = x - self.mean
        self.mean += a * d
        self.var = (1 - a) * (self.var + a * d * d)

    @property
    def std(self) -> float:
        return math.sqrt(self.var)


class Detector:
    def __init__(self, cfg: DetectorConfig) -> None:
        self.cfg = cfg
        self.history: list[ScoreRow] = []
        self._win: dict[str, deque[float]] = {
            k: deque(maxlen=cfg.burst_window) for k in ("rate", "uniq", "emote")
        }
        self._audio: dict[int, float] = {}
        self._base = {k: _Ewma(cfg.baseline_halflife) for k in FEATURES}
        self._start: int | None = None
        self._last_t: int | None = None
        self._last_trigger: float = -math.inf
        self._event: dict[str, Any] | None = None
        self._pending: Detection | None = None
        self._viewer_clips: list[float] = []

    # -- public -------------------------------------------------------------------------

    def add_viewer_clip(self, ts: float) -> None:
        """Twitch: a viewer created a clip at `ts` (strong signal, bonus on nearby peaks)."""
        self._viewer_clips.append(ts)

    def feed(self, obs: Observation) -> list[Detection]:
        if self._last_t is not None and obs.t <= self._last_t:
            raise ValueError("observations must be fed in increasing time order")
        out: list[Detection] = []
        # Fill gaps with empty seconds so windows stay time-correct.
        if self._last_t is not None:
            for gap_t in range(self._last_t + 1, obs.t):
                out += self._step(Observation(t=gap_t, chat_ok=obs.chat_ok))
        out += self._step(obs)
        return out

    def flush(self) -> list[Detection]:
        out: list[Detection] = []
        if self._event is not None:
            out += self._finish_event()
        if self._pending is not None:
            out.append(self._finalise(self._pending))
            self._pending = None
        return out

    # -- internals ----------------------------------------------------------------------

    def _step(self, obs: Observation) -> list[Detection]:
        cfg = self.cfg
        if self._start is None:
            self._start = obs.t
        self._last_t = obs.t
        self._win["rate"].append(obs.msgs)
        self._win["uniq"].append(obs.uniq)
        self._win["emote"].append(obs.emote)
        if obs.audio_db is not None:
            self._audio[obs.t] = obs.audio_db
        self._audio.pop(obs.t - int(cfg.lag_seconds) - cfg.burst_window - 5, None)

        x: dict[str, float] = {k: float(sum(self._win[k])) for k in ("rate", "uniq", "emote")}
        audio_x = self._audio_window(obs.t)
        z: dict[str, float] = {}
        floors = cfg.floors
        for k in ("rate", "uniq", "emote"):
            b = self._base[k]
            # Count data: noise is at least Poisson (sqrt of mean), so early/underestimated
            # variance does not make random fluctuations look like spikes.
            denom = max(b.std, getattr(floors, k), math.sqrt(max(b.mean, 0.0)))
            z[k] = (x[k] - b.mean) / denom if b.n else 0.0
        if audio_x is not None and self._base["audio"].n:
            b = self._base["audio"]
            x["audio"] = audio_x
            z["audio"] = (audio_x - b.mean) / max(b.std, floors.audio)
        else:
            z["audio"] = 0.0
        w = cfg.weights
        score = sum(getattr(w, k) * min(max(z[k], Z_MIN), Z_MAX) for k in FEATURES)
        self.history.append(ScoreRow(obs.t, score, z, x))

        out: list[Detection] = []
        hot = score >= cfg.threshold * obs.threshold_mult
        # Peak = highest score; on the clipped plateau, the strongest unclipped reaction.
        peak_key = (round(score, 2), sum(getattr(w, k) * z[k] for k in FEATURES))
        if self._event is not None:
            if peak_key > self._event["key"]:
                self._event.update(score=score, t=obs.t, z=dict(z), x=dict(x), key=peak_key)
            if obs.t >= self._event["t0"] + cfg.peak_window:
                out += self._finish_event()
        elif obs.chat_ok and not obs.suppressed and hot and self._can_trigger(obs.t, x):
            self._last_trigger = obs.t
            self._event = {
                "t0": obs.t,
                "t": obs.t,
                "score": score,
                "z": dict(z),
                "x": dict(x),
                "key": peak_key,
            }

        # Baseline is frozen during spikes so they don't inflate their own baseline. During
        # a raid it keeps adapting so the bigger audience slowly becomes the new normal.
        if obs.chat_ok and (not hot or obs.suppressed) and self._event is None:
            slow = hot  # only reachable while suppressed
            for k in ("rate", "uniq", "emote"):
                self._base[k].update(x[k], slow=slow)
            if audio_x is not None:
                self._base["audio"].update(audio_x, slow=slow)

        if (
            self._pending is not None
            and obs.t > self._pending.win_end + cfg.lag_seconds + cfg.pre_roll
        ):
            out.append(self._finalise(self._pending))
            self._pending = None
        return out

    def _audio_window(self, t: int) -> float | None:
        lag = round(self.cfg.lag_seconds)
        vals = [
            self._audio[s]
            for s in range(t - lag - self.cfg.burst_window + 1, t - lag + 1)
            if s in self._audio
        ]
        return sum(vals) / len(vals) if vals else None

    def _can_trigger(self, t: int, x: dict[str, float]) -> bool:
        cfg = self.cfg
        assert self._start is not None
        if t - self._start < cfg.warmup_seconds:
            return False
        if t - self._last_trigger < cfg.cooldown:
            return False
        rate = x["rate"] / cfg.burst_window
        if rate < cfg.min_rate:
            return False
        return x["rate"] >= cfg.min_ratio * self._base["rate"].mean

    def _finish_event(self) -> list[Detection]:
        ev = self._event
        assert ev is not None
        self._event = None
        cfg = self.cfg
        peak = float(ev["t"])
        event_ts = peak - cfg.lag_seconds
        det = Detection(
            peak_ts=peak,
            event_ts=event_ts,
            win_start=max(event_ts - cfg.pre_roll, float(self._start or 0)),
            win_end=peak + cfg.post_roll,
            det_score=float(ev["score"]),
            features={
                "score": round(ev["score"], 3),
                "z": {k: round(v, 3) for k, v in ev["z"].items()},
                "x": {k: round(v, 3) for k, v in ev["x"].items()},
                "baseline": {k: round(self._base[k].mean, 3) for k in FEATURES},
                "trigger_ts": ev["t0"],
            },
        )
        out: list[Detection] = []
        if self._pending is not None and det.win_start <= self._pending.win_end:
            p = self._pending
            if det.det_score > p.det_score:
                det.merged += p.merged
                det.win_start = min(p.win_start, det.win_start)
                self._pending = det
            else:
                p.win_end = max(p.win_end, det.win_end)
                p.merged += det.merged
        else:
            if self._pending is not None:
                out.append(self._finalise(self._pending))
            self._pending = det
        return out

    def _finalise(self, det: Detection) -> Detection:
        r = self.cfg.viewer_clip_radius
        clips = [c for c in self._viewer_clips if abs(c - det.peak_ts) <= r]
        if clips:
            det.det_score += self.cfg.viewer_clip_bonus
            det.features["viewer_clips"] = len(clips)
        det.features["merged"] = det.merged
        return det
