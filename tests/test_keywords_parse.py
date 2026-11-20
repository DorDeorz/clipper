from __future__ import annotations

import pytest

from clipper.detect.keywords_tr import DEFAULT_EXTRA_WEIGHT, KeywordScorer, parse_extra_keywords


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, {}),
        ("", {}),
        ("{}", {}),
        ("[]", {}),  # what the panel stores for an empty field (crashed the detector)
        ('{"tayfa": 2, "bozuk": "0.5"}', {"tayfa": 2.0, "bozuk": 0.5}),
        (
            '["tayfa", " katliam ", ""]',
            {"tayfa": DEFAULT_EXTRA_WEIGHT, "katliam": DEFAULT_EXTRA_WEIGHT},
        ),
        ('{"ok": 1, "bad": "x"}', {"ok": 1.0}),
        ("not json", {}),
        ("42", {}),
    ],
)
def test_parse_extra_keywords(raw: str | None, expected: dict[str, float]) -> None:
    assert parse_extra_keywords(raw) == expected


def test_panel_list_keywords_score_messages() -> None:
    scorer = KeywordScorer(extra=parse_extra_keywords('["tayfa"]'))
    assert scorer.extra == {"tayfa": DEFAULT_EXTRA_WEIGHT}
