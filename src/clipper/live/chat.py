"""What every live chat client talks to, and the reconnect loop they share.

Clients report wall-clock times; the session runner converts them to session time.
While a client is disconnected it reports a `gap`; the detector does not trigger inside
a gap (no chat is not the same as a quiet chat).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from clipper.detect.events import LiveEvent
from clipper.log import get_logger
from clipper.models import ChatMessage

log = get_logger("live.chat")


class ChatSink(Protocol):
    def message(self, msg: ChatMessage) -> None: ...

    def event(self, ev: LiveEvent) -> None: ...

    def gap(self, start: float, end: float) -> None: ...


class ChatClientError(Exception):
    """User-facing (Turkish). `fatal` = do not reconnect (e.g. wrong config)."""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class WS(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[WS]]


async def default_connect(url: str) -> WS:
    import websockets

    return await websockets.connect(url, ping_interval=None, max_size=2**20)  # type: ignore[return-value]


Session = Callable[[ChatSink, asyncio.Event], Awaitable[None]]


async def reconnecting(
    name: str,
    connect_once: Session,
    sink: ChatSink,
    stop: asyncio.Event,
    *,
    max_backoff: float = 60.0,
    clock: Callable[[], float] = time.time,
) -> None:
    """Run `connect_once` until `stop`; on any drop wait 1, 2, 4 … `max_backoff` s (with
    jitter) and reconnect. The time between the drop and the next connection is a gap."""
    backoff = 1.0
    down_since: float | None = None
    while not stop.is_set():
        started = clock()
        try:
            if down_since is not None:
                sink.gap(down_since, started)
                down_since = None
            await connect_once(sink, stop)
            if stop.is_set():
                return
            raise ChatClientError("bağlantı kapandı")
        except asyncio.CancelledError:
            raise
        except ChatClientError as exc:
            if exc.fatal:
                log.error("chat fatal", extra={"client": name, "error": str(exc)})
                raise
            err = str(exc)
        except Exception as exc:  # network errors of any library
            err = f"{type(exc).__name__}: {exc}"
        down_since = clock()
        if down_since - started > 60:
            backoff = 1.0  # it was up for a while: start over
        log.warning("chat reconnect", extra={"client": name, "error": err, "wait": backoff})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), backoff * (0.8 + 0.4 * random.random()))
        backoff = min(backoff * 2, max_backoff)
