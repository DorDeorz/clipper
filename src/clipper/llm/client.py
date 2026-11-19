"""Claude client: structured JSON output, prompt caching, one repair retry, cost tracking
in `llm_usage`, and spending limits.

Spending guards (all checked before every API call):
- `llm.monthly_budget_usd` and `llm.daily_budget_usd` are hard stops: once reached, no
  request goes out (`LLMBudgetExceeded`) until the next month / day (UTC).
- At `llm.downgrade_at` x monthly budget the judge falls back to the cheaper prefilter model.
- Failures that already cost money (invalid output twice, max_tokens, bad request) are not
  retried by the job queue (`retry = False`); only connection errors are.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from clipper.config import LLMConfig
from clipper.llm.schemas import strict_schema
from clipper.log import get_logger
from clipper.util.fs import atomic_write_text

log = get_logger("llm")
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class Price:
    """USD per 1M tokens."""

    input: float
    output: float
    cache_read: float

    @property
    def cache_write(self) -> float:  # 5-minute cache writes cost 1.25x input
        return self.input * 1.25


# Verified against the Claude API model table (2026-11). Haiku 5.5 prices are for prompts
# up to 100K tokens; our prompts are far below that.
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.10),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20),
    "claude-sonnet-5": Price(2.00, 10.00, 0.20),
    "claude-opus-4-8": Price(5.00, 25.00, 0.50),
}
UNKNOWN_PRICE = Price(5.00, 25.00, 0.50)  # conservative
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """User-facing (Turkish). Not retried by the job queue: the request may already have
    been billed, and repeating it would usually fail the same way."""

    retry = False


class LLMTransient(LLMError):
    """Network trouble before Claude answered (nothing billed): safe to retry later."""

    retry = True


class LLMBudgetExceeded(LLMError):
    """A spending limit is reached; no request was sent."""


class LLMCacheMiss(Exception):
    """Replay mode: the request is not in the cache. Deliberately not an LLMError, so the
    pipeline does not swallow it as an ordinary failed candidate."""


def request_key(kwargs: dict[str, Any], fallback: bool) -> str:
    body = json.dumps({**kwargs, "_fallback": fallback}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _dump_response(resp: Any, model: str) -> dict[str, Any]:
    u = resp.usage
    details = getattr(resp, "stop_details", None)
    return {
        "model": str(getattr(resp, "model", model) or model),
        "stop_reason": resp.stop_reason,
        "stop_category": getattr(details, "category", None) if details else None,
        "text": next((b.text for b in resp.content if getattr(b, "type", "") == "text"), ""),
        "usage": {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        },
    }


def _load_response(d: dict[str, Any]) -> Any:
    cat = d.get("stop_category")
    return SimpleNamespace(
        model=d["model"],
        stop_reason=d["stop_reason"],
        stop_details=SimpleNamespace(category=cat) if cat else None,
        content=[SimpleNamespace(type="text", text=d["text"])],
        usage=SimpleNamespace(**d["usage"]),
    )


class LLMRefused(LLMError):
    pass


@dataclass(frozen=True, slots=True)
class LLMResult[T]:
    value: T
    model: str
    usd: float
    input_tokens: int
    output_tokens: int
    cache_read: int
    attempts: int


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int
) -> float:
    p = PRICES.get(model, UNKNOWN_PRICE)
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read * p.cache_read
        + cache_write * p.cache_write
    ) / 1_000_000


def _spend_since(conn: sqlite3.Connection, start: float) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) FROM llm_usage WHERE ts >= ?", (start,)
    ).fetchone()
    return float(row[0])


def month_spend(conn: sqlite3.Connection, now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time(), UTC)
    return _spend_since(conn, datetime(dt.year, dt.month, 1, tzinfo=UTC).timestamp())


def day_spend(conn: sqlite3.Connection, now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time(), UTC)
    return _spend_since(conn, datetime(dt.year, dt.month, dt.day, tzinfo=UTC).timestamp())


class LLMClient:
    def __init__(
        self, conn: sqlite3.Connection, cfg: LLMConfig, client: anthropic.AsyncAnthropic
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.client = client

    def over_budget(self) -> bool:
        """Past the downgrade point of the monthly budget (judge uses the cheap model)."""
        return month_spend(self.conn) >= self.cfg.monthly_budget_usd * self.cfg.downgrade_at

    def check_budget(self) -> None:
        """Raise before a request if a hard spending limit is reached."""
        month = month_spend(self.conn)
        if month >= self.cfg.monthly_budget_usd:
            raise LLMBudgetExceeded(
                f"Aylık LLM bütçesi doldu (${month:.2f} / ${self.cfg.monthly_budget_usd:.2f})."
                " Yeni Claude isteği gönderilmiyor; config'te llm.monthly_budget_usd ile"
                " değiştirilebilir."
            )
        day = day_spend(self.conn)
        if day >= self.cfg.daily_budget_usd:
            raise LLMBudgetExceeded(
                f"Günlük LLM bütçesi doldu (${day:.2f} / ${self.cfg.daily_budget_usd:.2f})."
                " Yarın (UTC) devam edilecek; config'te llm.daily_budget_usd ile"
                " değiştirilebilir."
            )

    def judge_model(self) -> tuple[str, bool]:
        """Judge model, downgraded to the prefilter model near the monthly budget.
        Returns (model, downgraded)."""
        if self.over_budget():
            log.warning(
                "aylık LLM bütçesinin %%%d'i harcandı; judge %s modeline düşürüldü",
                round(self.cfg.downgrade_at * 100),
                self.cfg.prefilter_model,
            )
            return self.cfg.prefilter_model, True
        return self.cfg.judge_model, False

    async def structured(
        self,
        *,
        purpose: str,
        model: str,
        system: str,
        user: str | list[dict[str, Any]],
        schema: type[T],
        effort: str,
        max_tokens: int = 16000,
    ) -> LLMResult[T]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        total = {"usd": 0.0, "in": 0, "out": 0, "cr": 0}
        last_error = ""
        for attempt in (1, 2):
            text, served_by = await self._call(
                purpose, model, system, messages, schema, effort, max_tokens, total
            )
            try:
                value = schema.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)[:1500]
                log.warning("LLM output invalid", extra={"purpose": purpose, "attempt": attempt})
                messages += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            "Cevabın şemaya uymadı ya da kurallara aykırı. Hata:\n"
                            f"{last_error}\nLütfen düzeltilmiş JSON'u yeniden ver."
                        ),
                    },
                ]
                continue
            return LLMResult(
                value, served_by, total["usd"], total["in"], total["out"], total["cr"], attempt
            )
        raise LLMError(f"LLM iki denemede de geçerli çıktı vermedi: {last_error[:300]}")

    async def _call(
        self,
        purpose: str,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        effort: str,
        max_tokens: int,
        total: dict[str, Any],
    ) -> tuple[str, str]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": strict_schema(schema)},
            },
        }
        fallback = self.cfg.refusal_fallback and model in FALLBACK_MODELS
        cache_file: Path | None = None
        resp: Any = None
        replayed = False
        if self.cfg.cache_mode != "off":
            cache_file = Path(self.cfg.cache_dir) / f"{request_key(kwargs, fallback)}.json"
            if cache_file.is_file():
                replayed = True
                resp = _load_response(json.loads(cache_file.read_text(encoding="utf-8")))
            elif self.cfg.cache_mode == "replay":
                raise LLMCacheMiss(
                    f"LLM replay: önbellekte yok ({purpose}, {cache_file.name}). Önce"
                    " llm.cache_mode=record ile koş."
                )
        if resp is None:
            self.check_budget()
        try:
            if resp is not None:
                pass
            elif fallback:
                resp = await self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic API anahtarı geçersiz (ANTHROPIC_API_KEY).") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Model bulunamadı: {model} (config: llm.*_model).") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Claude isteği reddetti: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMTransient("Claude API'ye bağlanılamadı.") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                # The SDK already retried these (max_retries); the job queue tries later.
                raise LLMTransient(f"Claude API şu an yoğun ({exc.status_code}).") from exc
            raise LLMError(f"Claude API hatası ({exc.status_code}): {exc.message}") from exc

        if cache_file is not None and not cache_file.is_file():
            atomic_write_text(
                cache_file, json.dumps(_dump_response(resp, model), ensure_ascii=False, indent=1)
            )
        served_by = str(getattr(resp, "model", model) or model)
        u = resp.usage
        cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        usd = cost_usd(served_by, u.input_tokens, u.output_tokens, cache_read, cache_write)
        if replayed:
            usd = 0.0  # served from the record/replay cache: no API spend
        total["usd"] += usd
        total["in"] += u.input_tokens
        total["out"] += u.output_tokens
        total["cr"] += cache_read
        self.conn.execute(
            "INSERT INTO llm_usage (ts, model, purpose, input_tokens, output_tokens,"
            " cache_read, usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                served_by,
                purpose,
                u.input_tokens,
                u.output_tokens,
                cache_read,
                round(usd, 6),
            ),
        )
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            raise LLMRefused(f"Claude bu adayı değerlendirmeyi reddetti ({cat or 'kategori yok'}).")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Claude cevabı max_tokens sınırında kesildi.")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        return text, served_by
