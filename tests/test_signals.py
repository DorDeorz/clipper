from __future__ import annotations

import pytest

from clipper.detect.keywords_tr import KeywordScorer
from clipper.detect.signals import ChatBinner, bin_messages
from clipper.models import ChatMessage


@pytest.mark.parametrize(
    ("text", "expected", "cats"),
    [
        ("KEKW", 1.0, {"laugh"}),
        ("kekw", 0.0, set()),  # emotes are case-sensitive
        ("ahahahaha", 1.0, {"laugh"}),
        ("sjsjsjsj", 1.0, {"laugh"}),
        ("OHAAA", 1.2, {"shock"}),
        ("Yok Artık", 1.2, {"shock"}),
        ("bunu KLİPLE", 2.0, {"clip"}),
        ("klippp", 2.0, {"clip"}),
        ("efsane 🔥", 0.6, {"praise"}),
        ("F", 0.8, {"fail"}),
        ("Fenerbahçe", 0.0, set()),
        ("KEKW oha", 2.2, {"laugh", "shock"}),
        ("KEKW KEKW KEKW", 1.0, {"laugh"}),  # a category counts once per message
        ("selam", 0.0, set()),
    ],
)
def test_keyword_scores(text: str, expected: float, cats: set[str]) -> None:
    score, matched = KeywordScorer().score(text)
    assert score == pytest.approx(expected)
    assert matched == cats


def test_extra_streamer_keywords() -> None:
    score, matched = KeywordScorer(extra={"abiPls": 1.5}).score("abiPls KEKW")
    assert score == pytest.approx(2.5) and "extra:abiPls" in matched


def test_same_user_same_second_counts_once() -> None:
    b = ChatBinner()
    assert b.add(ChatMessage(10.1, "u", "a"))
    assert not b.add(ChatMessage(10.5, "u", "b"))
    assert b.add(ChatMessage(10.5, "v", "c"))
    (bin_,) = b.pop_all()
    assert (bin_.msgs, bin_.uniq) == (2, 2)


def test_user_max_two_per_five_seconds() -> None:
    bins = bin_messages(ChatMessage(float(t), "spammer", "KEKW") for t in range(10))
    assert sum(b.msgs for b in bins.values()) == 4  # t=0,1 then t=5,6


def test_repeat_spam_bonus() -> None:
    msgs = [ChatMessage(1.0 + i * 0.1, f"u{i}", "W") for i in range(6)]
    bins = bin_messages(msgs)
    # 5th and 6th distinct user repeating "W" within 5 s get +0.5 each
    assert bins[1].emote == pytest.approx(1.0)


def test_paid_weight_adds_to_emote() -> None:
    bins = bin_messages([ChatMessage(3.0, "u", "teşekkürler", weight=1.5)])
    assert bins[3].emote == pytest.approx(0.5)


def test_pop_until() -> None:
    b = ChatBinner()
    for t in (1.0, 2.0, 5.0):
        b.add(ChatMessage(t, f"u{t}", "x"))
    assert [x.t for x in b.pop_until(3)] == [1, 2]
    assert [x.t for x in b.pop_all()] == [5]
