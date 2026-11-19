"""TestClient for the panel the way the browser talks to it: loopback Host, same-origin,
and the CSRF header that `static/app.js` sends on every state-changing request."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

BASE_URL = "http://127.0.0.1:8765"


def panel_client(app: FastAPI, **kw: Any) -> TestClient:
    client = TestClient(app, base_url=BASE_URL, **kw)
    client.headers["X-CSRF-Token"] = app.state.csrf_token
    return client
