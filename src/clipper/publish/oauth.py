"""OAuth2 authorization code (+ PKCE) for publishing accounts (ROADMAP 4.7).

The panel's "Hesap bağla" opens `start()`'s URL; the platform redirects the browser
to `http://127.0.0.1:<panel port>/callback/<platform>`, and `callback()` exchanges the
code. Tokens are Fernet-encrypted (`CLIPPER_SECRET_KEY`) into `oauth_tokens`; they
are never logged. `access_token()` refreshes shortly before expiry.

Endpoints (checked against the official docs, 2026-11):
- Google (Desktop app): accounts.google.com/o/oauth2/v2/auth, oauth2.googleapis.com/token
- X: x.com/i/oauth2/authorize, api.x.com/2/oauth2/token (Basic auth for confidential
  clients, `client_id` in the body for public ones)
- TikTok (Login Kit desktop): www.tiktok.com/v2/auth/authorize/ with `client_key`,
  comma-separated scopes and a *hex* SHA256 code challenge; open.tiktokapis.com/v2/oauth/token/
- Meta (Facebook Login for Business, needed for Instagram resumable upload): manual
  code flow (www.facebook.com/<v>/dialog/oauth → GET graph.facebook.com/<v>/oauth/access_token),
  then the long-lived exchange and the Page token from /me/accounts.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as _secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from clipper.config import Config, Secrets
from clipper.log import get_logger
from clipper.publish.base import AccessToken, AuthError, PublishError, error_text, send
from clipper.util.crypto import SecretBox

log = get_logger("oauth")

PLATFORMS = ("youtube", "instagram", "x", "tiktok")
STATE_TTL = 900.0
REFRESH_MARGIN = 120.0
Clock = Callable[[], float]
# (client, provider, token response) -> (account label or None, extra fields to store)
Identify = Callable[[httpx.AsyncClient, "Provider", dict[str, Any]], Awaitable[dict[str, Any]]]


class OAuthError(Exception):
    pass


@dataclass(frozen=True)
class Provider:
    platform: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str | None
    scopes: tuple[str, ...]
    scope_sep: str = " "
    client_id_param: str = "client_id"
    pkce: Literal["S256", "S256hex", None] = "S256"
    client_auth: Literal["body", "basic"] = "body"
    token_method: Literal["POST", "GET"] = "POST"
    extra_params: dict[str, str] = field(default_factory=dict)
    identify: Identify | None = None


def _s(v: Any) -> str | None:
    return v.get_secret_value() if v is not None else None


def redirect_uri(cfg: Config, platform: str) -> str:
    return f"http://127.0.0.1:{cfg.panel.port}/callback/{platform}"


async def _identify_instagram(
    client: httpx.AsyncClient, p: Provider, tok: dict[str, Any]
) -> dict[str, Any]:
    """Short-lived user token → long-lived user token → Page token + IG business id."""
    base = p.token_url.rsplit("/oauth/", 1)[0]
    resp = await send(
        client,
        "GET",
        p.token_url,
        platform="instagram",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": p.client_id,
            "client_secret": p.client_secret or "",
            "fb_exchange_token": tok["access_token"],
        },
    )
    user_token = resp.json()["access_token"]
    resp = await send(
        client,
        "GET",
        f"{base}/me/accounts",
        platform="instagram",
        params={
            "fields": "id,name,access_token,instagram_business_account",
            "access_token": user_token,
        },
    )
    pages = [pg for pg in resp.json().get("data", []) if pg.get("instagram_business_account")]
    if not pages:
        raise OAuthError(
            "Bu Facebook hesabının yönettiği sayfalarda bağlı Instagram profesyonel hesap yok"
        )
    page = pages[0]
    return {
        "account": page.get("name") or page["id"],
        "access_token": page["access_token"],
        "refresh_token": None,
        "expires_in": None,  # long-lived Page tokens have no fixed expiry
        "extra": {
            "ig_user_id": page["instagram_business_account"]["id"],
            "page_id": page["id"],
            "pages": [pg.get("name") for pg in pages],
        },
    }


async def _identify_x(
    client: httpx.AsyncClient, p: Provider, tok: dict[str, Any]
) -> dict[str, Any]:
    resp = await send(
        client,
        "GET",
        "https://api.x.com/2/users/me",
        platform="x",
        headers={"Authorization": f"Bearer {tok['access_token']}"},
    )
    data = resp.json().get("data") or {}
    return {"account": data.get("username"), "extra": {"user_id": data.get("id")}}


async def _identify_tiktok(
    client: httpx.AsyncClient, p: Provider, tok: dict[str, Any]
) -> dict[str, Any]:
    return {"extra": {"open_id": tok.get("open_id")}}


def providers(cfg: Config, secrets: Secrets) -> dict[str, Provider]:
    """Providers whose client credentials are present in `.env`."""
    out: dict[str, Provider] = {}
    if secrets.youtube_client_id is not None:
        out["youtube"] = Provider(
            "youtube",
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
            _s(secrets.youtube_client_id) or "",
            _s(secrets.youtube_client_secret),
            ("https://www.googleapis.com/auth/youtube.upload",),
            # A refresh token every time, also on re-connect.
            extra_params={"access_type": "offline", "prompt": "consent"},
        )
    if secrets.meta_app_id is not None:
        v = cfg.publish.graph_api_version
        out["instagram"] = Provider(
            "instagram",
            f"https://www.facebook.com/{v}/dialog/oauth",
            f"https://graph.facebook.com/{v}/oauth/access_token",
            _s(secrets.meta_app_id) or "",
            _s(secrets.meta_app_secret),
            (
                "instagram_basic",
                "instagram_content_publish",
                "pages_show_list",
                "pages_read_engagement",
            ),
            scope_sep=",",
            pkce=None,
            token_method="GET",
            identify=_identify_instagram,
        )
    if secrets.x_client_id is not None:
        out["x"] = Provider(
            "x",
            "https://x.com/i/oauth2/authorize",
            "https://api.x.com/2/oauth2/token",
            _s(secrets.x_client_id) or "",
            _s(secrets.x_client_secret),
            ("tweet.read", "tweet.write", "users.read", "media.write", "offline.access"),
            client_auth="basic" if secrets.x_client_secret is not None else "body",
            identify=_identify_x,
        )
    if secrets.tiktok_client_key is not None:
        scopes = ["user.info.basic", "video.upload"]
        if cfg.publish.tiktok_mode == "direct" and cfg.publish.tiktok_audit_passed:
            scopes.append("video.publish")
        out["tiktok"] = Provider(
            "tiktok",
            "https://www.tiktok.com/v2/auth/authorize/",
            "https://open.tiktokapis.com/v2/oauth/token/",
            _s(secrets.tiktok_client_key) or "",
            _s(secrets.tiktok_client_secret),
            tuple(scopes),
            scope_sep=",",
            client_id_param="client_key",
            pkce="S256hex",
            identify=_identify_tiktok,
        )
    return out


def code_challenge(verifier: str, method: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    if method == "S256hex":
        return digest.hex()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class _Pending:
    platform: str
    account: str | None
    verifier: str
    created: float


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


class OAuthManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        box: SecretBox | None,
        provs: dict[str, Provider],
        client: httpx.AsyncClient,
        *,
        clock: Clock = time.time,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.box = box
        self.providers = provs
        self.client = client
        self.clock = clock
        self._pending: dict[str, _Pending] = {}

    # ---- storage ---------------------------------------------------------------------

    def _box(self) -> SecretBox:
        if self.box is None:
            raise OAuthError(
                "CLIPPER_SECRET_KEY yok; `uv run clipper keygen` ile üretip .env'e ekle"
            )
        return self.box

    def save(self, platform: str, account: str, data: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO oauth_tokens (platform, account, data_enc, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(platform, account) DO UPDATE SET data_enc = excluded.data_enc,"
            " updated_at = excluded.updated_at",
            (platform, account, self._box().encrypt_json(data), _iso(self.clock())),
        )

    def load(self, platform: str, account: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT data_enc FROM oauth_tokens WHERE platform = ? AND account = ?",
            (platform, account),
        ).fetchone()
        if row is None:
            raise AuthError(f"{platform}: '{account}' hesabı bağlı değil")
        data: dict[str, Any] = self._box().decrypt_json(row[0])
        return data

    def accounts(self, platform: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT account FROM oauth_tokens WHERE platform = ? ORDER BY updated_at", (platform,)
        ).fetchall()
        return [r[0] for r in rows]

    def remove(self, platform: str, account: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM oauth_tokens WHERE platform = ? AND account = ?", (platform, account)
        )
        return cur.rowcount > 0

    # ---- flow ------------------------------------------------------------------------

    def _provider(self, platform: str) -> Provider:
        p = self.providers.get(platform)
        if p is None:
            raise OAuthError(f"{platform} için uygulama bilgileri .env'de yok")
        return p

    def start(self, platform: str, account: str | None = None) -> str:
        p = self._provider(platform)
        self._box()
        now = self.clock()
        self._pending = {k: v for k, v in self._pending.items() if now - v.created < STATE_TTL}
        state = _secrets.token_urlsafe(24)
        verifier = _secrets.token_urlsafe(48)  # 64 chars of [A-Za-z0-9_-]
        self._pending[state] = _Pending(platform, account or None, verifier, now)
        params = {
            p.client_id_param: p.client_id,
            "redirect_uri": redirect_uri(self.cfg, platform),
            "response_type": "code",
            "scope": p.scope_sep.join(p.scopes),
            "state": state,
            **p.extra_params,
        }
        if p.pkce is not None:
            params["code_challenge"] = code_challenge(verifier, p.pkce)
            params["code_challenge_method"] = "S256"
        return f"{p.authorize_url}?{urlencode(params)}"

    async def _token_request(self, p: Provider, data: dict[str, str]) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        body = dict(data)
        if p.client_auth == "basic" and p.client_secret:
            raw = f"{p.client_id}:{p.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            body[p.client_id_param] = p.client_id
            if p.client_secret:
                body["client_secret"] = p.client_secret
        try:
            if p.token_method == "GET":
                resp = await send(
                    self.client,
                    "GET",
                    p.token_url,
                    platform=p.platform,
                    params=body,
                    headers=headers,
                    retries=2,
                )
            else:
                resp = await send(
                    self.client,
                    "POST",
                    p.token_url,
                    platform=p.platform,
                    data=body,
                    headers=headers,
                    retries=2,
                )
        except AuthError as exc:
            raise OAuthError(str(exc)) from exc
        except PublishError as exc:
            raise OAuthError(f"{p.platform}: token alınamadı ({exc})") from exc
        tok: dict[str, Any] = resp.json()
        if "access_token" not in tok:
            # TikTok answers 200 with an error object.
            raise OAuthError(f"{p.platform}: token alınamadı ({error_text(resp)})")
        return tok

    async def callback(
        self, platform: str, code: str | None, state: str | None, error: str | None = None
    ) -> str:
        """Finish the flow; returns the stored account label."""
        if error:
            raise OAuthError(f"{platform}: yetki verilmedi ({error})")
        pend = self._pending.pop(state or "", None)
        if pend is None or pend.platform != platform:
            raise OAuthError("geçersiz ya da süresi dolmuş istek; panelden tekrar 'Hesap bağla'")
        if not code:
            raise OAuthError("yetki kodu gelmedi")
        p = self._provider(platform)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(self.cfg, platform),
        }
        if p.pkce is not None:
            data["code_verifier"] = pend.verifier
        if p.token_method == "GET":
            data.pop("grant_type")  # Facebook's code exchange takes no grant_type
        tok = await self._token_request(p, data)
        info = await p.identify(self.client, p, tok) if p.identify else {}
        merged = {**tok, **{k: v for k, v in info.items() if k not in ("account", "extra")}}
        account = pend.account or info.get("account") or "main"
        self.save(platform, str(account), self._record(merged, info.get("extra") or {}))
        log.info("account connected", extra={"platform": platform, "account": account})
        return str(account)

    def _record(self, tok: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        exp = tok.get("expires_in")
        return {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "expires_at": self.clock() + float(exp) if exp else None,
            "scope": tok.get("scope"),
            "extra": extra,
        }

    async def access_token(self, platform: str, account: str) -> AccessToken:
        data = self.load(platform, account)
        exp = data.get("expires_at")
        if exp is None or exp - REFRESH_MARGIN > self.clock():
            return AccessToken(data["access_token"], data.get("extra") or {})
        if not data.get("refresh_token"):
            raise AuthError(f"{platform}: '{account}' token süresi doldu; hesabı yeniden bağla")
        p = self._provider(platform)
        try:
            tok = await self._token_request(
                p, {"grant_type": "refresh_token", "refresh_token": data["refresh_token"]}
            )
        except OAuthError as exc:
            raise AuthError(f"{exc}; hesabı yeniden bağla") from exc
        # Google keeps the old refresh token; X and TikTok rotate it.
        tok.setdefault("refresh_token", data["refresh_token"])
        rec = self._record(tok, data.get("extra") or {})
        self.save(platform, account, rec)
        log.info("token refreshed", extra={"platform": platform, "account": account})
        return AccessToken(rec["access_token"], rec["extra"])
