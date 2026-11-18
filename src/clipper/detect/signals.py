"""Per-second chat signals (ROADMAP 4.3): message count with spam clamping, unique
users, and emote/keyword score including the repeated-text bonus."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from clipper.detect.keywords_tr import (
    REPEAT_SPAM_BONUS,
    REPEAT_SPAM_USERS,
    REPEAT_SPAM_WINDOW,
    KeywordScorer,
)
from clipper.models import ChatMessage
from clipper.render.tr_text import tr_casefold

USER_WINDOW = 5.0  # seconds
USER_MAX_IN_WINDOW = 2


@dataclass(slots=True)
class SecondBin:
    t: int
    msgs: int = 0
    uniq: int = 0
    emote: float = 0.0  # combined emote + keyword + repeat-spam score
    kw: int = 0  # clip-request messages (informational)
    users: set[str] = field(default_factory=set)


class ChatBinner:
    """Streaming binner. Feed messages in time order; read finished bins with `pop_until`.

    Spam rules: a user's repeats within the same second count once, and at most
    `USER_MAX_IN_WINDOW` messages per user per `USER_WINDOW` seconds are counted.
    """

    def __init__(self, scorer: KeywordScorer | None = None) -> None:
        self.scorer = scorer or KeywordScorer()
        self._bins: dict[int, SecondBin] = {}
        self._user_recent: dict[str, deque[float]] = defaultdict(deque)
        self._user_last_sec: dict[str, int] = {}
        self._text_recent: dict[str, deque[tuple[float, str]]] = defaultdict(deque)

    def add(self, msg: ChatMessage) -> bool:
        """Returns False if the message was dropped by spam clamping."""
        sec = math.floor(msg.ts)
        if self._user_last_sec.get(msg.user_id) == sec:
            return False
        recent = self._user_recent[msg.user_id]
        while recent and msg.ts - recent[0] >= USER_WINDOW:
            recent.popleft()
        if len(recent) >= USER_MAX_IN_WINDOW:
            return False
        recent.append(msg.ts)
        self._user_last_sec[msg.user_id] = sec

        b = self._bins.get(sec)
        if b is None:
            b = self._bins[sec] = SecondBin(t=sec)
        b.msgs += 1
        b.users.add(msg.user_id)
        b.uniq = len(b.users)
        score, matched = self.scorer.score(msg.text, msg.emotes)
        b.emote += score + max(msg.weight - 1.0, 0.0)
        if "clip" in matched:
            b.kw += 1
        if self._repeat_spam(msg):
            b.emote += REPEAT_SPAM_BONUS
        return True

    def _repeat_spam(self, msg: ChatMessage) -> bool:
        key = tr_casefold(msg.text.strip())
        if not key:
            return False
        q = self._text_recent[key]
        q.append((msg.ts, msg.user_id))
        while q and msg.ts - q[0][0] > REPEAT_SPAM_WINDOW:
            q.popleft()
        return len({u for _, u in q}) >= REPEAT_SPAM_USERS

    def pop_until(self, t_exclusive: int) -> list[SecondBin]:
        """Remove and return bins with t < t_exclusive (sorted)."""
        done = sorted(t for t in self._bins if t < t_exclusive)
        return [self._bins.pop(t) for t in done]

    def pop_all(self) -> list[SecondBin]:
        return self.pop_until(math.inf)  # type: ignore[arg-type]


def bin_messages(
    messages: Iterable[ChatMessage], scorer: KeywordScorer | None = None
) -> dict[int, SecondBin]:
    binner = ChatBinner(scorer)
    for m in sorted(messages, key=lambda m: m.ts):
        binner.add(m)
    return {b.t: b for b in binner.pop_all()}


def dense_range(bins: dict[int, SecondBin], start: int, end: int) -> Iterator[SecondBin]:
    """Every second in [start, end), empty bins filled in."""
    for t in range(start, end):
        yield bins.get(t) or SecondBin(t=t)
