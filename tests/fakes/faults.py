"""Fault injection for httpx MockTransport handlers (TESTPLAN 1: 429, 5xx, token expiry,
dropped connections)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

Handler = Callable[[httpx.Request], httpx.Response]
FAULTS = ("429", "500", "503", "401", "403", "drop")


@dataclass
class Faulty:
    """Wraps a handler. `plan` maps the n-th matching request (1-based) to a fault; only
    requests for which `match` is true are counted."""

    inner: Handler
    plan: dict[int, str] = field(default_factory=dict)
    match: Callable[[httpx.Request], bool] = lambda _r: True
    retry_after: str = "1"
    seen: int = 0
    injected: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.match(request):
            self.seen += 1
            fault = self.plan.get(self.seen)
            if fault is not None:
                self.injected.append(fault)
                if fault == "drop":
                    raise httpx.ReadError("connection dropped (fault)", request=request)
                headers = {"Retry-After": self.retry_after} if fault == "429" else {}
                return httpx.Response(int(fault), headers=headers, json={"error": fault})
        return self.inner(request)
