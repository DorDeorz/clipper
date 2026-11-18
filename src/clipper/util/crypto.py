"""Fernet encryption for secrets stored in the DB (e.g. `oauth_tokens.data_enc`)."""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(Exception):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


class SecretBox:
    def __init__(self, key: str | bytes) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise CryptoError(
                "CLIPPER_SECRET_KEY geçersiz. `uv run clipper keygen` ile yeni anahtar üret."
            ) from exc

    def encrypt(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise CryptoError(
                "Şifreli veri çözülemedi (CLIPPER_SECRET_KEY değişmiş olabilir)."
            ) from exc

    def encrypt_json(self, obj: Any) -> bytes:
        return self.encrypt(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def decrypt_json(self, token: bytes) -> Any:
        return json.loads(self.decrypt(token).decode("utf-8"))
