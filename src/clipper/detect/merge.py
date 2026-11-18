"""Candidate merging across the three r3 paths (ROADMAP 4.3b "Birleştirme").

Reactive (chat + audio), content (transcript scan) and viewer-clip candidates that
overlap within `radius` seconds become one candidate; all sources are kept.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

SOURCE_ORDER = ("reactive", "content", "viewer_clip")


@dataclass(slots=True)
class Draft:
    peak_ts: float
    win_start: float
    win_end: float
    det_score: float
    sources: list[str]
    features: dict[str, Any] = field(default_factory=dict)
    content_score: float | None = None
    content_reason: str | None = None

    @property
    def primary(self) -> str:
        return min(self.sources, key=SOURCE_ORDER.index)


def _close(a: Draft, b: Draft, radius: float) -> bool:
    return a.win_start - radius <= b.win_end and b.win_start - radius <= a.win_end


def _absorb(into: Draft, other: Draft, max_seconds: float) -> None:
    keep_peak = SOURCE_ORDER.index(other.primary) < SOURCE_ORDER.index(into.primary) or (
        other.primary == into.primary and other.det_score > into.det_score
    )
    if keep_peak:
        into.peak_ts = other.peak_ts
    for s in other.sources:
        if s not in into.sources:
            into.sources.append(s)
    into.sources.sort(key=SOURCE_ORDER.index)
    into.det_score = max(into.det_score, other.det_score)
    if other.content_score is not None and (
        into.content_score is None or other.content_score > into.content_score
    ):
        into.content_score = other.content_score
        into.content_reason = other.content_reason
    for k, v in other.features.items():
        if k == "viewer_clips" and isinstance(v, list):
            into.features.setdefault("viewer_clips", [])
            if isinstance(into.features["viewer_clips"], list):
                into.features["viewer_clips"] += v
        else:
            into.features.setdefault(k, v)
    start = min(into.win_start, other.win_start)
    end = max(into.win_end, other.win_end)
    if end - start > max_seconds:
        # Keep the window around the peak, within the length cap.
        half = max_seconds / 2
        start = max(start, into.peak_ts - half)
        end = min(end, start + max_seconds)
    into.win_start, into.win_end = start, end


def merge_drafts(drafts: Iterable[Draft], radius: float, max_seconds: float = 240.0) -> list[Draft]:
    """Merge drafts from different sources that overlap within `radius`. Drafts from the
    same source are assumed already de-duplicated by their producer and are never merged
    with each other directly (but can end up together via a third, cross-source draft)."""
    out: list[Draft] = []
    for d in sorted(drafts, key=lambda d: d.win_start):
        d.sources = sorted(dict.fromkeys(d.sources), key=SOURCE_ORDER.index)
        target = next(
            (o for o in out if _close(o, d, radius) and not set(o.sources) >= set(d.sources)),
            None,
        )
        if target is None:
            out.append(d)
        else:
            _absorb(target, d, max_seconds)
    for d in out:
        d.features["sources"] = list(d.sources)
    return sorted(out, key=lambda d: d.peak_ts)
