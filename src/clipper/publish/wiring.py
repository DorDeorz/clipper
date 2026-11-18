"""Builds the OAuth manager and the per-platform publishers from config + `.env`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import httpx

from clipper.config import Config, Secrets
from clipper.publish.base import AccessToken, Publisher
from clipper.publish.instagram import InstagramPublisher
from clipper.publish.oauth import OAuthManager, providers
from clipper.publish.tiktok import TikTokPublisher
from clipper.publish.x import XPublisher
from clipper.publish.youtube import YouTubePublisher
from clipper.util.crypto import SecretBox


@dataclass
class PublishKit:
    oauth: OAuthManager
    publishers: dict[str, Publisher] = field(default_factory=dict)

    def accounts(self, platform: str) -> list[str]:
        if platform not in self.publishers:
            return []
        return self.oauth.accounts(platform)


def build(
    conn: sqlite3.Connection, cfg: Config, secrets: Secrets, client: httpx.AsyncClient
) -> PublishKit:
    key = secrets.clipper_secret_key
    box = SecretBox(key.get_secret_value()) if key is not None else None
    provs = providers(cfg, secrets)
    oauth = OAuthManager(conn, cfg, box, provs, client)

    async def tokens(platform: str, account: str) -> AccessToken:
        return await oauth.access_token(platform, account)

    classes: dict[str, type] = {
        "youtube": YouTubePublisher,
        "instagram": InstagramPublisher,
        "x": XPublisher,
        "tiktok": TikTokPublisher,
    }
    pubs: dict[str, Publisher] = {
        name: cls(cfg.publish, client, tokens) for name, cls in classes.items() if name in provs
    }
    return PublishKit(oauth, pubs)
