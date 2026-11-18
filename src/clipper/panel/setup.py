"""Panel "Kurulum" screen: `.env` keys, secret key generation, doctor checks.

Values of secrets are never sent to the browser; only whether each key is set. Writing
keeps the rest of `.env` (comments, unknown keys) intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper.config import Config, Secrets
from clipper.util.crypto import CryptoError, SecretBox, generate_key


@dataclass(frozen=True)
class EnvKey:
    name: str
    label: str
    group: str
    help: str
    needs_restart: bool = True


ENV_KEYS: tuple[EnvKey, ...] = (
    EnvKey("ANTHROPIC_API_KEY", "Anthropic API anahtarı", "Temel",
           "console.anthropic.com → API Keys. Klip puanlama ve başlıklar için gerekli."),
    EnvKey("CLIPPER_SECRET_KEY", "Gizli anahtar", "Temel",
           "Bağlı hesap token'larını şifreler. 'Üret' butonuyla oluştur."),
    EnvKey("YOUTUBE_CLIENT_ID", "YouTube client ID", "YouTube",
           "Google Cloud → OAuth client (Desktop app)."),
    EnvKey("YOUTUBE_CLIENT_SECRET", "YouTube client secret", "YouTube", ""),
    EnvKey("META_APP_ID", "Meta app ID", "Instagram",
           "developers.facebook.com → Facebook Login for Business."),
    EnvKey("META_APP_SECRET", "Meta app secret", "Instagram", ""),
    EnvKey("X_CLIENT_ID", "X client ID", "X", "developer.x.com → OAuth 2.0."),
    EnvKey("X_CLIENT_SECRET", "X client secret", "X", ""),
    EnvKey("TIKTOK_CLIENT_KEY", "TikTok client key", "TikTok",
           "developers.tiktok.com → Login Kit (Desktop) + Content Posting API."),
    EnvKey("TIKTOK_CLIENT_SECRET", "TikTok client secret", "TikTok", ""),
    EnvKey("TWITCH_CLIENT_ID", "Twitch client ID (opsiyonel)", "Yedek", "Sadece resmi API yedeği."),
    EnvKey("TWITCH_CLIENT_SECRET", "Twitch client secret (opsiyonel)", "Yedek", ""),
    EnvKey("YOUTUBE_API_KEY", "YouTube API key (opsiyonel)", "Yedek", "Sadece resmi API yedeği."),
    EnvKey("PANEL_PASSWORD", "Panel parolası (opsiyonel)", "Yedek",
           "Sadece panel 127.0.0.1 dışına açılacaksa."),
)  # fmt: skip
KEYS = {k.name: k for k in ENV_KEYS}
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class SetupError(Exception):
    pass


def env_status(secrets: Secrets) -> list[dict[str, Any]]:
    out = []
    for k in ENV_KEYS:
        value = getattr(secrets, k.name.lower(), None)
        out.append({"name": k.name, "label": k.label, "group": k.group, "help": k.help,
                    "set": value is not None})  # fmt: skip
    return out


def _clean(name: str, value: str) -> str:
    if name not in KEYS:
        raise SetupError(f"bilinmeyen anahtar: {name}")
    value = value.strip()
    if any(c in value for c in "\r\n\x00"):
        raise SetupError(f"{name}: değer tek satır olmalı")
    if name == "ANTHROPIC_API_KEY" and value and not value.startswith("sk-ant-"):
        raise SetupError("Anthropic anahtarı 'sk-ant-' ile başlamalı")
    if name == "CLIPPER_SECRET_KEY" and value:
        try:
            SecretBox(value)
        except CryptoError as exc:
            raise SetupError(str(exc)) from exc
    return value


def update_env(env_path: Path, updates: dict[str, str]) -> list[str]:
    """Set keys in `.env` (empty value = leave as is). Returns the changed key names."""
    clean = {n: _clean(n, v) for n, v in updates.items()}
    clean = {n: v for n, v in clean.items() if v}
    if not clean:
        return []
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    done: set[str] = set()
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(1) in clean:
            lines[i] = f"{m.group(1)}={clean[m.group(1)]}"
            done.add(m.group(1))
    lines += [f"{n}={v}" for n, v in clean.items() if n not in done]
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(env_path)
    return sorted(clean)


def generate_secret_key(env_path: Path, secrets: Secrets, connected_accounts: int) -> bool:
    """Create CLIPPER_SECRET_KEY if missing. Never replaces a key that already encrypts
    connected accounts (they would become unreadable)."""
    if secrets.clipper_secret_key is not None:
        if connected_accounts:
            raise SetupError(
                "gizli anahtar zaten var ve bağlı hesaplar onunla şifreli; değiştirilemez"
            )
        return False
    update_env(env_path, {"CLIPPER_SECRET_KEY": generate_key()})
    return True


async def checks(cfg: Config, secrets: Secrets) -> list[dict[str, str]]:
    from clipper.doctor import run_checks

    return [
        {"name": c.name, "status": c.status.value, "detail": c.detail}
        for c in await run_checks(cfg, secrets)
    ]
