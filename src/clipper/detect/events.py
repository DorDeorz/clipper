"""Platform events and the reactive event filter (ROADMAP 4.3 item 8b, r3).

Events (subs, gifts, bits, raids, donations, superchats, viewer clips) never open a
candidate on their own, except viewer clips. A sub train or a raid makes chat explode
without a "moment", so for a while after them the reactive trigger is raised or closed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from clipper.config import EventsConfig

EventKind = Literal[
    "sub", "gift_sub", "bits", "raid", "donation", "superchat", "membership", "viewer_clip"
]  # fmt: skip
EVENT_KINDS: tuple[str, ...] = (
    "sub", "gift_sub", "bits", "raid", "donation", "superchat", "membership", "viewer_clip"
)  # fmt: skip
SUB_KINDS = frozenset({"sub", "gift_sub", "membership"})


@dataclass(frozen=True, slots=True)
class LiveEvent:
    ts: float
    kind: str
    amount: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ts": self.ts, "kind": self.kind}
        if self.amount is not None:
            out["amount"] = self.amount
        if self.data:
            out["data"] = self.data
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> LiveEvent:
        return cls(
            ts=float(d["ts"]),
            kind=str(d["kind"]),
            amount=None if d.get("amount") is None else float(d["amount"]),
            data=dict(d.get("data") or {}),
        )


def write_events(conn: sqlite3.Connection, session_id: int, events: Iterable[LiveEvent]) -> int:
    rows = [
        (
            session_id,
            e.ts,
            e.kind,
            e.amount,
            json.dumps(e.data, ensure_ascii=False) if e.data else None,
        )
        for e in events
    ]
    conn.executemany(
        "INSERT INTO live_events (session_id, ts, kind, amount, data) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def read_events(
    conn: sqlite3.Connection, session_id: int, kinds: Sequence[str] | None = None
) -> list[LiveEvent]:
    sql = "SELECT ts, kind, amount, data FROM live_events WHERE session_id = ?"
    args: list[Any] = [session_id]
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    rows = conn.execute(sql + " ORDER BY ts", args).fetchall()
    return [LiveEvent(float(r[0]), str(r[1]), r[2], json.loads(r[3]) if r[3] else {}) for r in rows]


@dataclass(frozen=True, slots=True)
class FilterState:
    threshold_mult: float = 1.0
    suppressed: bool = False  # reactive trigger closed (raid)
    reason: str | None = None


class EventFilter:
    """Answers, for a second `t`, how the reactive trigger is affected by recent events.

    Works for streaming too: `add()` events as they arrive (in any order), then ask
    `state(t)` for increasing `t`.
    """

    def __init__(self, cfg: EventsConfig, events: Iterable[LiveEvent] = ()) -> None:
        self.cfg = cfg
        self._train_ends: list[tuple[float, float]] = []  # (start, end) boosted intervals
        self._raids: list[tuple[float, float]] = []
        self._subs: list[float] = []
        for e in events:
            self.add(e)

    def add(self, e: LiveEvent) -> None:
        cfg = self.cfg
        if e.kind == "raid":
            self._raids.append((e.ts, e.ts + cfg.raid_suppress_seconds))
            return
        if e.kind not in SUB_KINDS:
            return
        # A gift event carrying N subs counts as N for the train.
        n = max(1, int(e.amount or 1)) if e.kind == "gift_sub" else 1
        self._subs.extend([e.ts] * n)
        self._subs.sort()
        lo = e.ts - cfg.sub_train_window
        recent = [s for s in self._subs if lo <= s <= e.ts]
        if len(recent) >= cfg.sub_train_count:
            self._train_ends.append((recent[0], e.ts + cfg.suppress_after_sub_train))

    def state(self, t: float) -> FilterState:
        for a, b in self._raids:
            if a <= t < b:
                return FilterState(1.0, True, "raid")
        for a, b in self._train_ends:
            if a <= t < b:
                return FilterState(self.cfg.suppress_threshold_boost, False, "sub_train")
        return FilterState()

    @property
    def active_intervals(self) -> list[tuple[float, float, str]]:
        return [(a, b, "raid") for a, b in self._raids] + [
            (a, b, "sub_train") for a, b in self._train_ends
        ]


def events_near(events: Sequence[LiveEvent], t0: float, t1: float) -> dict[str, float]:
    """Summary of events in [t0, t1] for features / prompts: kind -> count (or amount sum
    for bits, donations and superchats)."""
    out: dict[str, float] = {}
    for e in events:
        if t0 <= e.ts <= t1:
            if e.kind in ("bits", "donation", "superchat") and e.amount is not None:
                out[e.kind] = out.get(e.kind, 0.0) + e.amount
            elif e.kind == "gift_sub":
                out[e.kind] = out.get(e.kind, 0.0) + (e.amount or 1)
            else:
                out[e.kind] = out.get(e.kind, 0.0) + 1
    return out
