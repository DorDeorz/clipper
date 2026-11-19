"""LLM output schemas (pydantic) and their strict JSON-schema form for structured outputs."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class PrefilterDecision(_Out):
    keep: bool
    rough_score: int
    reason: str

    @field_validator("rough_score")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(100, v))


ContentKind = Literal["story", "joke", "rant", "argument", "reaction", "info", "none"]


class ContentScanDecision(_Out):
    """r3 content path: one transcript window. Times are seconds from the window start."""

    score: int
    kind: ContentKind
    reason: str
    best_start: float
    best_end: float

    @field_validator("score")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(100, v))


class FacecamDecision(_Out):
    """Facecam location on a stream frame, normalised to 0-1 (x, y = top-left)."""

    found: bool
    x: float
    y: float
    w: float
    h: float
    confidence: int
    reason: str

    @field_validator("x", "y", "w", "h")
    @classmethod
    def _unit(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class CaptionFix(_Out):
    from_: str = Field(alias="from")
    to: str


class YouTubeText(_Out):
    title: str
    description: str


class CaptionText(_Out):
    caption: str


class XText(_Out):
    text: str


class PlatformTexts(_Out):
    youtube: YouTubeText
    instagram: CaptionText
    tiktok: CaptionText
    x: XText


class Flags(_Out):
    music_risk: bool
    nsfw: bool
    sensitive_topic: bool
    needs_context: bool

    @property
    def clean(self) -> bool:
        return not (self.music_risk or self.nsfw or self.sensitive_topic)


class JudgeDecision(_Out):
    keep: bool
    score: int
    reason: str
    start: float
    end: float
    title: str
    hook: str
    caption_fixes: list[CaptionFix]
    hashtags: list[str]
    texts: PlatformTexts
    flags: Flags

    @field_validator("score")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(100, v))

    @field_validator("title")
    @classmethod
    def _title_len(cls, v: str) -> str:
        if len(v) > 100:
            raise ValueError("başlık 100 karakterden uzun")
        return v

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: float, info: Any) -> float:
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("end, start'tan büyük olmalı")
        return v


_DROP = {"title", "default", "minimum", "maximum", "minLength", "maxLength", "description"}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema -> self-contained strict schema for `output_config.format`:
    refs inlined, every object closed (`additionalProperties: false`) with all keys
    required. Range checks stay in pydantic validators."""
    raw = model.model_json_schema(by_alias=True)
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
            out = {k: walk(v) for k, v in node.items() if k not in _DROP}
            if isinstance(node.get("properties"), dict):
                # Keys of `properties` are field names, not keywords: a field may be
                # called "title" or "description" (JudgeDecision, YouTubeText).
                out["properties"] = {k: walk(v) for k, v in node["properties"].items()}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(raw)
