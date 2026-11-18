"""Common chat log format (JSON lines, optionally gzip) and importers.

Line format (one `ChatMessage` per line):
    {"ts": 12.345, "user_id": "<sha1>", "text": "...", "emotes": ["KEKW"], "platform": "twitch"}

`ts` is either unix seconds (live) or seconds from the session's time base
(offline sessions use pdt=0, i.e. 0 = start of the video).
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO, Any

from clipper.models import ChatMessage, hash_user_id

# Anything below this is treated as an offset from video start, not a unix time.
ABSOLUTE_TS_MIN = 100_000_000.0


class ChatFormatError(Exception):
    pass


def _open_text(path: Path, mode: str, gz: bool | None = None) -> IO[str]:
    if gz if gz is not None else path.suffix == ".gz":
        return io.TextIOWrapper(
            gzip.open(path, mode.replace("t", "") + "b"), encoding="utf-8", newline="\n"
        )
    return path.open(mode, encoding="utf-8", newline="\n")


def read_jsonl(path: Path) -> Iterator[ChatMessage]:
    with _open_text(path, "rt") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield ChatMessage.from_json(json.loads(line))
            except (ValueError, KeyError, TypeError) as exc:
                raise ChatFormatError(f"{path}:{lineno} geçersiz chat satırı: {exc}") from exc


def write_jsonl(path: Path, messages: Iterable[ChatMessage]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    count = 0
    with _open_text(tmp, "wt", gz=path.suffix == ".gz") as fh:
        for msg in messages:
            fh.write(json.dumps(msg.to_json(), ensure_ascii=False) + "\n")
            count += 1
    tmp.replace(path)
    return count


def is_absolute(messages: list[ChatMessage]) -> bool:
    return bool(messages) and min(m.ts for m in messages) >= ABSOLUTE_TS_MIN


def shift(messages: Iterable[ChatMessage], offset: float) -> Iterator[ChatMessage]:
    """Return messages with `ts - offset`, dropping those before 0."""
    for m in messages:
        ts = m.ts - offset
        if ts >= 0:
            yield ChatMessage(
                ts=ts,
                user_id=m.user_id,
                text=m.text,
                emotes=m.emotes,
                platform=m.platform,
                weight=m.weight,
            )


# --- TwitchDownloader (lay295/TwitchDownloader) JSON import -------------------------------


def from_twitchdownloader(data: dict[str, Any]) -> list[ChatMessage]:
    """Convert a TwitchDownloader chat JSON export to messages with ts = offset seconds."""
    comments = data.get("comments")
    if not isinstance(comments, list):
        raise ChatFormatError("TwitchDownloader JSON'unda 'comments' listesi yok")
    out: list[ChatMessage] = []
    for c in comments:
        try:
            offset = float(c["content_offset_seconds"])
            commenter = c.get("commenter") or {}
            raw_uid = str(commenter.get("_id") or commenter.get("name") or "anon")
            message = c.get("message") or {}
            fragments = message.get("fragments") or []
            text = message.get("body")
            if text is None:
                text = "".join(str(f.get("text", "")) for f in fragments)
            emotes = [str(f["text"]).strip() for f in fragments if f.get("emoticon")]
        except (KeyError, TypeError, ValueError) as exc:
            raise ChatFormatError(f"TwitchDownloader yorumu okunamadı: {exc}") from exc
        out.append(
            ChatMessage(
                ts=offset,
                user_id=hash_user_id("twitch", raw_uid),
                text=text,
                emotes=emotes,
                platform="twitch",
            )
        )
    out.sort(key=lambda m: m.ts)
    return out


def load_any(path: Path) -> list[ChatMessage]:
    """Load our jsonl(.gz) or a TwitchDownloader `.json` export."""
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ChatFormatError(f"{path} JSON olarak okunamadı: {exc}") from exc
        if isinstance(data, dict) and "comments" in data:
            return from_twitchdownloader(data)
        raise ChatFormatError(f"{path}: tanınmayan chat JSON biçimi")
    msgs = list(read_jsonl(path))
    msgs.sort(key=lambda m: m.ts)
    return msgs
