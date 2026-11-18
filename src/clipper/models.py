"""Shared plain data types passed between components."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

Platform = Literal["twitch", "kick", "youtube", "local"]


def hash_user_id(platform: str, raw_user_id: str) -> str:
    """User ids are stored only as sha1 hashes (privacy; still unique per platform)."""
    return hashlib.sha1(f"{platform}:{raw_user_id}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChatMessage:
    ts: float  # unix seconds (platform send time, not local receive time)
    user_id: str  # sha1-hashed
    text: str
    emotes: list[str] = field(default_factory=list)
    platform: str = "local"
    weight: float = 1.0  # e.g. 1.5 for YouTube paid / membership messages

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ts": self.ts,
            "user_id": self.user_id,
            "text": self.text,
            "emotes": list(self.emotes),
            "platform": self.platform,
        }
        if self.weight != 1.0:
            data["weight"] = self.weight
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ChatMessage:
        return cls(
            ts=float(data["ts"]),
            user_id=str(data["user_id"]),
            text=str(data["text"]),
            emotes=[str(e) for e in data.get("emotes", [])],
            platform=str(data.get("platform", "local")),
            weight=float(data.get("weight", 1.0)),
        )


@dataclass(frozen=True, slots=True)
class Segment:
    session_id: int
    seq: int
    path: str
    pdt_start: float
    duration: float
    is_ad: bool = False

    @property
    def pdt_end(self) -> float:
        return self.pdt_start + self.duration


@dataclass(slots=True)
class Candidate:
    session_id: int
    peak_ts: float
    win_start: float
    win_end: float
    det_score: float
    features: dict[str, Any] = field(default_factory=dict)
    status: str = "new"
    id: int | None = None
