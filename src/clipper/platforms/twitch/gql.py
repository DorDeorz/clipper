"""Anonymous Twitch GQL client (unofficial; isolated here on purpose).

Uses the public Client-ID of the twitch.tv website, no account. Supports raw
queries and persisted queries; batching (list bodies) for multiple operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from clipper.config import TwitchConfig
from clipper.log import get_logger

log = get_logger("twitch.gql")


class TwitchGQLError(Exception):
    """Unofficial API failed or changed shape. Message is user-facing (Turkish)."""


@dataclass(frozen=True, slots=True)
class GQLOperation:
    name: str
    variables: dict[str, Any]
    query: str | None = None  # raw query; used when no persisted hash is configured

    def body(self, cfg: TwitchConfig) -> dict[str, Any]:
        body: dict[str, Any] = {"operationName": self.name, "variables": self.variables}
        sha = cfg.persisted_queries.get(self.name)
        if sha:
            body["extensions"] = {"persistedQuery": {"version": 1, "sha256Hash": sha}}
        elif self.query is not None:
            body["query"] = self.query
        else:
            raise TwitchGQLError(
                f"Twitch GQL '{self.name}' için ne sorgu ne persisted hash var (config: twitch)."
            )
        return body


class TwitchGQL:
    def __init__(self, client: httpx.AsyncClient, cfg: TwitchConfig) -> None:
        self._client = client
        self._cfg = cfg

    async def run(self, op: GQLOperation) -> dict[str, Any]:
        return (await self.batch([op]))[0]

    async def batch(self, ops: list[GQLOperation]) -> list[dict[str, Any]]:
        """Send operations in one request; returns each operation's `data`."""
        payload = [op.body(self._cfg) for op in ops]
        try:
            resp = await self._client.post(
                self._cfg.gql_url,
                json=payload,
                headers={"Client-ID": self._cfg.client_id},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise TwitchGQLError(f"Twitch GQL'e bağlanılamadı: {exc}") from exc
        if resp.status_code != 200:
            raise TwitchGQLError(
                f"Twitch GQL HTTP {resp.status_code}. Client-ID veya uç nokta değişmiş olabilir"
                " (config: twitch.client_id / twitch.gql_url)."
            )
        try:
            results = resp.json()
        except ValueError as exc:
            raise TwitchGQLError("Twitch GQL geçersiz JSON döndürdü.") from exc
        if isinstance(results, dict):
            results = [results]
        if not isinstance(results, list) or len(results) != len(ops):
            raise TwitchGQLError("Twitch GQL beklenmeyen cevap biçimi döndürdü.")
        out: list[dict[str, Any]] = []
        for op, res in zip(ops, results, strict=True):
            errors = res.get("errors") if isinstance(res, dict) else None
            if errors:
                msg = "; ".join(str(e.get("message", e)) for e in errors)
                hint = ""
                if "PersistedQueryNotFound" in msg:
                    hint = (
                        " Persisted query hash'i değişmiş"
                        f" (config: twitch.persisted_queries.{op.name})."
                    )
                elif "integrity" in msg.lower():
                    hint = (
                        " Twitch bu sorgu için 'integrity' istiyor;"
                        " anonim erişim kapanmış olabilir."
                    )
                raise TwitchGQLError(f"Twitch GQL '{op.name}' hatası: {msg}.{hint}")
            data = res.get("data") if isinstance(res, dict) else None
            if not isinstance(data, dict):
                raise TwitchGQLError(f"Twitch GQL '{op.name}' veri döndürmedi.")
            out.append(data)
        return out
