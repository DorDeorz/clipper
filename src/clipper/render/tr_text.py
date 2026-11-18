"""Turkish-aware text helpers. Never use `str.upper()` / `str.lower()` on Turkish text:
they map i -> I and I -> i, but Turkish needs i <-> İ and ı <-> I."""

from __future__ import annotations

_UPPER_MAP = str.maketrans({"i": "İ", "ı": "I"})
_LOWER_MAP = str.maketrans({"I": "ı", "İ": "i"})


def tr_upper(text: str) -> str:
    return text.translate(_UPPER_MAP).upper()


def tr_lower(text: str) -> str:
    return text.translate(_LOWER_MAP).lower()


def tr_casefold(text: str) -> str:
    """Case-insensitive matching key for Turkish (also folds dotted/dotless i together,
    since chat users mix them freely: "ıyı", "iyi", "IYI" all match)."""
    return tr_lower(text).replace("ı", "i")
