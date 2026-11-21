from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any

import pytest

from clipper.config import LLMConfig
from clipper.llm.client import LLMClient, LLMError, LLMRefused, cost_usd, month_spend
from clipper.llm.prompts import (
    Word,
    apply_caption_fixes,
    chat_timeline,
    judge_system,
    judge_user,
    snap_to_words,
    spoken_name,
)
from clipper.llm.schemas import (
    ContentScanDecision,
    FacecamDecision,
    JudgeDecision,
    PrefilterDecision,
    strict_schema,
)
from clipper.models import ChatMessage
from tests.llm_mock import FakeAnthropic, judge_payload, message


def _client(conn: sqlite3.Connection, fake: FakeAnthropic, **cfg: object) -> LLMClient:
    return LLMClient(conn, LLMConfig(**cfg), fake.client())  # type: ignore[arg-type]


def _prefilter(llm: LLMClient, model: str = "claude-haiku-5-5"):  # type: ignore[no-untyped-def]
    return asyncio.run(
        llm.structured(
            purpose="prefilter",
            model=model,
            system="sys",
            user="user",
            schema=PrefilterDecision,
            effort="low",
        )
    )


def test_structured_output_request_shape(conn: sqlite3.Connection) -> None:
    fake = FakeAnthropic([lambda b: json.dumps({"keep": True, "rough_score": 70, "reason": "iyi"})])
    res = _prefilter(_client(conn, fake))
    assert res.value.keep and res.value.rough_score == 70 and res.attempts == 1
    body = fake.requests[0]
    assert body["model"] == "claude-haiku-5-5"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == {"keep", "rough_score", "reason"}
    assert body["output_config"]["effort"] == "low"
    assert "tool_choice" not in body
    # Haiku has no server-side fallback -> plain endpoint.
    assert fake.betas[0] == ""


def test_invalid_json_is_retried_once(conn: sqlite3.Connection) -> None:
    fake = FakeAnthropic(
        [
            lambda b: "bu json değil",
            lambda b: json.dumps({"keep": False, "rough_score": 10, "reason": "spam"}),
        ]
    )
    res = _prefilter(_client(conn, fake))
    assert res.attempts == 2 and res.value.keep is False
    second = fake.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert "şemaya uymadı" in second[2]["content"]
    rows = conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    assert rows == 2


def test_invalid_twice_raises(conn: sqlite3.Connection) -> None:
    fake = FakeAnthropic([lambda b: "{}", lambda b: "{}"])
    with pytest.raises(LLMError):
        _prefilter(_client(conn, fake))


def test_semantic_validation_triggers_retry(conn: sqlite3.Connection) -> None:
    bad = judge_payload(start=30.0, end=10.0)
    fake = FakeAnthropic([lambda b: json.dumps(bad), lambda b: json.dumps(judge_payload())])
    llm = _client(conn, fake)
    res = asyncio.run(
        llm.structured(
            purpose="judge",
            model="claude-sonnet-5-5",
            system="s",
            user="u",
            schema=JudgeDecision,
            effort="medium",
        )
    )
    assert res.attempts == 2
    assert res.value.caption_fixes[0].from_ == "valoran"
    # Sonnet 5.5 goes through the server-side refusal fallback beta.
    assert "server-side-fallback-2026-07-01" in fake.betas[0]
    assert fake.requests[0]["fallbacks"] == "default"


def test_cost_recorded(conn: sqlite3.Connection) -> None:
    fake = FakeAnthropic(
        [
            lambda b: message(
                b["model"],
                json.dumps({"keep": True, "rough_score": 60, "reason": "x"}),
                inp=10_000,
                out=1_000,
                cache_read=5_000,
            )
        ]
    )
    res = _prefilter(_client(conn, fake))
    expected = cost_usd("claude-haiku-5-5", 10_000, 1_000, 5_000, 0)
    assert res.usd == pytest.approx(expected)
    assert expected == pytest.approx((10_000 * 0.10 + 1_000 * 0.50 + 5_000 * 0.01) / 1e6)
    row = conn.execute("SELECT model, purpose, usd, cache_read FROM llm_usage").fetchone()
    assert row["model"] == "claude-haiku-5-5" and row["purpose"] == "prefilter"
    assert row["cache_read"] == 5_000
    assert month_spend(conn) == pytest.approx(round(expected, 6))


def test_budget_exceeded_downgrades_judge(conn: sqlite3.Connection) -> None:
    llm = _client(conn, FakeAnthropic(), monthly_budget_usd=1.0)
    assert llm.judge_model() == ("claude-sonnet-5-5", False)
    conn.execute(
        "INSERT INTO llm_usage (ts, model, purpose, input_tokens, output_tokens, cache_read, usd)"
        " VALUES (?, 'claude-sonnet-5-5', 'judge', 0, 0, 0, 1.5)",
        (time.time(),),
    )
    assert llm.judge_model() == ("claude-haiku-5-5", True)


def test_last_month_spend_not_counted(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO llm_usage (ts, model, purpose, input_tokens, output_tokens, cache_read, usd)"
        " VALUES (?, 'm', 'judge', 0, 0, 0, 99)",
        (time.time() - 40 * 86400,),
    )
    assert month_spend(conn) == 0


def test_refusal_raises(conn: sqlite3.Connection) -> None:
    fake = FakeAnthropic([lambda b: message(b["model"], "", stop="refusal")])
    with pytest.raises(LLMRefused):
        _prefilter(_client(conn, fake))


def test_strict_schema_is_self_contained() -> None:
    schema = strict_schema(JudgeDecision)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert schema["properties"]["caption_fixes"]["items"]["required"] == ["from", "to"]
    assert schema["properties"]["texts"]["additionalProperties"] is False


def _assert_schema_covers(schema: dict[str, Any], value: Any, path: str = "$") -> None:
    """Every key of a valid output must be allowed (and required) by the strict schema;
    with `additionalProperties: false` the API cannot produce a key the schema lacks."""
    if isinstance(value, dict):
        props = schema.get("properties", {})
        assert sorted(schema.get("required", [])) == sorted(value), path
        for key, sub in value.items():
            assert key in props, f"{path}.{key} missing from schema"
            _assert_schema_covers(props[key], sub, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _assert_schema_covers(schema["items"], item, f"{path}[{i}]")


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (JudgeDecision, judge_payload()),
        (PrefilterDecision, {"keep": True, "rough_score": 50, "reason": "r"}),
        (
            ContentScanDecision,
            {"score": 70, "kind": "story", "reason": "r", "best_start": 1.0, "best_end": 9.0},
        ),
        (
            FacecamDecision,
            {
                "found": True,
                "x": 0.1,
                "y": 0.1,
                "w": 0.2,
                "h": 0.2,
                "confidence": 80,
                "reason": "r",
            },
        ),
    ],
)
def test_strict_schema_keeps_every_model_field(model: type[Any], payload: dict[str, Any]) -> None:
    # Audit B-01: fields named "title"/"description" were dropped as JSON Schema keywords.
    model.model_validate(payload)  # the payload is a valid answer
    _assert_schema_covers(strict_schema(model), payload)


def test_judge_system_formats_lengths() -> None:
    s = judge_system(12, 59)
    assert "12-59 saniye" in s and '{"from": "yanlış"' in s


def test_judge_system_asks_for_third_person_specific_hooks() -> None:
    s = judge_system(12, 59)
    # Titles/hooks are written by the page sharing the clip, not by the streamer.
    assert "üçüncü şahıs" in s and "birinci şahıs" in s
    assert "Metinlerde kullanılacak ad" in s
    # Generic hooks are called out explicitly.
    assert "chat çıldırdı" in s and "somut" in s
    assert "Eray'ın parası kulübe yetmedi" in s


@pytest.mark.parametrize(
    ("display", "spoken"),
    [
        ("BerkcanOguslu", "Berkcan"),
        ("Eray", "Eray"),
        ("AOGofficial", "AOGofficial"),
        ("evelone2004", "evelone2004"),
        ("RRaenee", "RRaenee"),
        ("ŞahinÖzdemir", "Şahin"),
        ("@CanBey", "Can"),
    ],
)
def test_spoken_name(display: str, spoken: str) -> None:
    assert spoken_name(display) == spoken


def test_judge_user_names_the_streamer() -> None:
    user = judge_user("BerkcanOguslu", "GTA 6", 60, "[0.0] selam", "(chat yok)", "", [])
    assert "Yayıncı: BerkcanOguslu" in user
    assert "Metinlerde kullanılacak ad: Berkcan" in user


def test_snap_to_words() -> None:
    words = [Word("a", 1.0, 1.4), Word("b", 5.0, 5.5), Word("c", 30.0, 30.6), Word("d", 41, 41.5)]
    start, end = snap_to_words(4.6, 31.0, words, duration=80, min_len=12, max_len=59)
    assert start == pytest.approx(4.85) and end == pytest.approx(30.9)
    # Too short -> extended to min_len.
    start, end = snap_to_words(5.0, 6.0, words, duration=80, min_len=12, max_len=59)
    assert end - start == pytest.approx(12)
    # Too long -> cut to max_len, never past duration.
    start, end = snap_to_words(0, 200, [], duration=70, min_len=12, max_len=59)
    assert (start, end) == (0, 59)
    start, end = snap_to_words(60, 70, [], duration=65, min_len=12, max_len=59)
    assert end == 65 and end - start == pytest.approx(12)


def test_caption_fixes() -> None:
    tr = [
        {
            "start": 0,
            "end": 1,
            "text": "valoran oynuyoruz, valoran!",
            "words": [{"w": "valoran", "s": 0, "e": 0.5, "p": 0.9}],
        }
    ]
    out = apply_caption_fixes(tr, [("valoran", "Valorant")])
    assert out[0]["text"] == "Valorant oynuyoruz, Valorant!"
    assert out[0]["words"][0]["w"] == "Valorant"
    # reviewer fixes are Turkish case-insensitive (captions are shown upper-cased)
    out = apply_caption_fixes(tr, [("VALORAN", "Valorant")])
    assert out[0]["text"] == "Valorant oynuyoruz, Valorant!"
    out = apply_caption_fixes([{"text": "Iyi", "words": [{"w": "Iyi", "s": 0, "e": 1}]}],
                              [("ıyı", "iyi")])  # fmt: skip
    assert out[0]["words"][0]["w"] == "iyi"


def test_chat_timeline_relative_seconds() -> None:
    msgs = [ChatMessage(ts=100 + i * 0.5, user_id=f"u{i}", text="KEKW") for i in range(6)]
    out = chat_timeline(msgs, 100, 110)
    assert out.splitlines()[0] == "0: 2 | KEKW"
    assert chat_timeline([], 0, 10) == "(chat yok)"


def _spend(conn: sqlite3.Connection, usd: float, ts: float | None = None) -> None:
    conn.execute(
        "INSERT INTO llm_usage (ts, model, purpose, input_tokens, output_tokens, cache_read, usd)"
        " VALUES (?, 'claude-sonnet-5-5', 'judge', 0, 0, 0, ?)",
        (ts if ts is not None else time.time(), usd),
    )


@pytest.mark.parametrize(
    ("limits", "spent", "msg"),
    [
        ({"monthly_budget_usd": 1.0}, 1.0, "Aylık"),
        ({"daily_budget_usd": 0.5}, 0.6, "Günlük"),
    ],
)
def test_hard_budget_sends_nothing(
    conn: sqlite3.Connection, limits: dict[str, float], spent: float, msg: str
) -> None:
    from clipper.llm.client import LLMBudgetExceeded

    fake = FakeAnthropic([lambda b: json.dumps({"keep": True, "rough_score": 1, "reason": ""})])
    _spend(conn, spent)
    with pytest.raises(LLMBudgetExceeded, match=msg) as info:
        _prefilter(_client(conn, fake, **limits))
    assert fake.requests == []  # no request left the machine
    assert info.value.retry is False


def test_daily_budget_resets_next_day(conn: sqlite3.Connection) -> None:
    fake = FakeAnthropic([lambda b: json.dumps({"keep": True, "rough_score": 1, "reason": ""})])
    _spend(conn, 5.0, ts=time.time() - 2 * 86400)  # same month maybe, but not today
    llm = _client(conn, fake, daily_budget_usd=1.0, monthly_budget_usd=1000)
    assert _prefilter(llm).value.keep


def test_billed_failures_are_not_requeued(conn: sqlite3.Connection) -> None:
    """Invalid output twice is billed twice; the job queue must not repeat it 4 more times."""
    fake = FakeAnthropic([lambda b: "not json", lambda b: "still not json"])
    with pytest.raises(LLMError) as info:
        _prefilter(_client(conn, fake))
    assert len(fake.requests) == 2
    assert info.value.retry is False

    from clipper.worker import _retryable

    assert not _retryable(info.value)


def test_server_overload_is_retryable(conn: sqlite3.Connection) -> None:
    from clipper.llm.client import LLMTransient

    fake = FakeAnthropic()  # every request -> HTTP 500 (SDK retries, then gives up)
    with pytest.raises(LLMTransient) as info:
        _prefilter(_client(conn, fake))
    assert info.value.retry is True
    assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0
