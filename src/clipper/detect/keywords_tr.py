"""Default Turkish chat keyword / emote weights (ROADMAP 4.3). Extendable per streamer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from clipper.render.tr_text import tr_casefold

REPEAT_SPAM_BONUS = 0.5
REPEAT_SPAM_USERS = 5
REPEAT_SPAM_WINDOW = 5.0


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    weight: float
    tokens: tuple[str, ...] = ()  # whole-word, case-sensitive (emotes like KEKW, F)
    words: tuple[str, ...] = ()  # whole-word or phrase, Turkish case-insensitive
    substrings: tuple[str, ...] = ()  # emoji etc.
    patterns: tuple[str, ...] = ()  # regexes on the case-folded text


DEFAULT_CATEGORIES: tuple[Category, ...] = (
    Category(
        "laugh",
        1.0,
        tokens=("KEKW", "OMEGALUL", "LUL", "LULW", "ICANT", "pepeLaugh", "xD"),
        words=("xd", "sjsjsj", "ahsdj"),
        substrings=("😂", "🤣"),
        patterns=(r"(?:a?ha){3,}", r"(?:s?j){4,}"),
    ),
    Category(
        "shock",
        1.2,
        tokens=("monkaS", "PogChamp", "Pog", "POGGERS"),
        words=("oha", "yok artık", "nasıl ya"),
        substrings=("😱", "😳"),
        patterns=(r"\bohaa+\b",),
    ),
    Category(
        "clip",
        2.0,
        words=("clip", "klip", "kliple", "clipit", "clip it"),
        patterns=(r"\bklipp+\b",),
    ),
    Category(
        "praise", 0.6, tokens=("GG",), words=("efsane", "helal", "gg", "ez"), substrings=("🔥",)
    ),
    Category("fail", 0.8, tokens=("F", "RIP"), words=("rip",), substrings=("💀",)),
)


@dataclass(slots=True)
class KeywordScorer:
    categories: tuple[Category, ...] = DEFAULT_CATEGORIES
    extra: dict[str, float] = field(default_factory=dict)  # streamer-specific token weights
    _compiled: list[tuple[Category, re.Pattern[str] | None, set[str]]] = field(
        default_factory=list, init=False, repr=False
    )

    def __post_init__(self) -> None:
        for cat in self.categories:
            alternatives = [r"(?<!\w)" + re.escape(tr_casefold(w)) + r"(?!\w)" for w in cat.words]
            alternatives += [re.escape(s) for s in cat.substrings]
            alternatives += list(cat.patterns)
            rx = re.compile("|".join(alternatives)) if alternatives else None
            self._compiled.append((cat, rx, set(cat.tokens)))

    def score(self, text: str, emotes: list[str] | None = None) -> tuple[float, set[str]]:
        """Sum of weights of matched categories (each category counted once per message)."""
        tokens = set(text.split()) | set(emotes or ())
        folded = tr_casefold(text)
        total = 0.0
        matched: set[str] = set()
        for cat, rx, cat_tokens in self._compiled:
            if (cat_tokens & tokens) or (rx is not None and rx.search(folded)):
                total += cat.weight
                matched.add(cat.name)
        for token, weight in self.extra.items():
            if token in tokens:
                total += weight
                matched.add(f"extra:{token}")
        return total, matched
