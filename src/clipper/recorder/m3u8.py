"""Minimal HLS playlist parser (master + media), enough for Twitch/Kick/YouTube."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from clipper.util.timeutil import parse_iso

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("(?:[^"]*)"|[^,]*)')


def parse_attrs(text: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR_RE.findall(text)}


@dataclass(frozen=True, slots=True)
class Variant:
    url: str
    name: str  # e.g. "1080p60", "chunked" (source)
    bandwidth: int
    resolution: str | None
    frame_rate: float | None


@dataclass(frozen=True, slots=True)
class MediaSegment:
    seq: int
    url: str
    duration: float
    title: str
    pdt: float | None  # EXT-X-PROGRAM-DATE-TIME (unix seconds)
    is_ad: bool = False
    map_url: str | None = None  # EXT-X-MAP init section (fMP4/CMAF playlists)


@dataclass(slots=True)
class MediaPlaylist:
    target_duration: float
    media_sequence: int
    segments: list[MediaSegment] = field(default_factory=list)
    ended: bool = False


def parse_master(text: str, base_url: str) -> list[Variant]:
    variants: list[Variant] = []
    media_names: dict[str, str] = {}
    pending: dict[str, str] | None = None
    for line in (ln.strip() for ln in text.splitlines()):
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            if attrs.get("TYPE") == "VIDEO" and "GROUP-ID" in attrs:
                media_names[attrs["GROUP-ID"]] = attrs.get("NAME", attrs["GROUP-ID"])
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attrs(line.split(":", 1)[1])
        elif line and not line.startswith("#") and pending is not None:
            group = pending.get("VIDEO", "")
            name = media_names.get(group) or group or pending.get("RESOLUTION", "unknown")
            fr = pending.get("FRAME-RATE")
            variants.append(
                Variant(
                    url=urljoin(base_url, line),
                    name=_normalise_name(name),
                    bandwidth=int(pending.get("BANDWIDTH", "0") or 0),
                    resolution=pending.get("RESOLUTION"),
                    frame_rate=float(fr) if fr else None,
                )
            )
            pending = None
    return variants


def _normalise_name(name: str) -> str:
    # Twitch names the source "1080p60 (source)"; the group id is "chunked".
    return name.replace("(source)", "").strip()


def pick_variant(variants: list[Variant], preference: list[str]) -> Variant:
    """First variant matching the preference order; `best` = highest bandwidth."""
    if not variants:
        raise ValueError("playlist has no variants")
    by_name = {v.name: v for v in variants}
    for pref in preference:
        if pref == "best":
            return max(variants, key=lambda v: v.bandwidth)
        if pref in by_name:
            return by_name[pref]
    return max(variants, key=lambda v: v.bandwidth)


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    pl = MediaPlaylist(target_duration=0.0, media_sequence=0)
    seq: int | None = None
    duration = 0.0
    title = ""
    pdt: float | None = None
    map_url: str | None = None
    ad_ranges: list[tuple[float, float]] = []
    for line in (ln.strip() for ln in text.splitlines()):
        if line.startswith("#EXT-X-TARGETDURATION:"):
            pl.target_duration = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            pl.media_sequence = int(line.split(":", 1)[1])
            seq = pl.media_sequence
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            if "BYTERANGE" in attrs:
                raise ValueError("EXT-X-MAP BYTERANGE desteklenmiyor")
            map_url = urljoin(base_url, attrs["URI"])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt = parse_iso(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-DATERANGE:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            if "stitched-ad" in attrs.get("CLASS", "") or "stitched-ad" in attrs.get("ID", ""):
                start = parse_iso(attrs["START-DATE"]) if "START-DATE" in attrs else None
                dur = float(attrs.get("DURATION", attrs.get("PLANNED-DURATION", "0")) or 0)
                if start is not None:
                    ad_ranges.append((start, start + dur))
        elif line.startswith("#EXTINF:"):
            dur_text, _, title = line.split(":", 1)[1].partition(",")
            duration = float(dur_text)
        elif line == "#EXT-X-ENDLIST":
            pl.ended = True
        elif line and not line.startswith("#"):
            if seq is None:
                seq = pl.media_sequence
            # Twitch live: content segments are titled "live"; anything else is an ad.
            is_ad = bool(title) and title != "live"
            if pdt is not None and any(a <= pdt < b for a, b in ad_ranges):
                is_ad = True
            pl.segments.append(
                MediaSegment(
                    seq=seq,
                    url=urljoin(base_url, line),
                    duration=duration,
                    title=title,
                    pdt=pdt,
                    is_ad=is_ad,
                    map_url=map_url,
                )
            )
            seq += 1
            if pdt is not None:
                pdt += duration
            title = ""
    return pl
