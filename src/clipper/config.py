"""Configuration: pydantic models, YAML loading and a tiny `.env` parser."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Strict):
    data_dir: Path = Path("./data")
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    fonts_dir: Path = Path("./assets/fonts")


class MonitoringConfig(_Strict):
    """r2: monitoring (live status, chat, clips, VOD chat) works without accounts by default.

    `anonymous` uses Twitch anonymous IRC/GQL, YouTube innertube and Kick Pusher.
    `official` falls back to Twitch Helix / YouTube Data API (needs keys).
    """

    mode: Literal["anonymous", "official"] = "anonymous"


class TwitchConfig(_Strict):
    """Unofficial Twitch web endpoints. Kept in config so they can be fixed without code."""

    gql_url: str = "https://gql.twitch.tv/gql"
    # Public Client-ID of the twitch.tv website (no account needed).
    client_id: str = "kimne78kx3ncx6brgo4mv6wki5h1ko"
    usher_vod_url: str = "https://usher.ttvnw.net/vod/{vod_id}.m3u8"
    gql_retries: int = 4
    gql_retry_base_seconds: float = 1.0
    usher_live_url: str = "https://usher.ttvnw.net/api/channel/hls/{login}.m3u8"
    # Persisted query hashes (sha256). Empty string = send the raw query instead.
    persisted_queries: dict[str, str] = Field(
        default_factory=lambda: {
            "VideoCommentsByOffsetOrCursor": (
                "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
            ),
        }
    )
    download_concurrency: int = 6


class KickConfig(_Strict):
    """Unofficial kick.com web API (no account). Paths are kept here so a change on Kick's
    side can be fixed without code. Verified live on 2026-11-18."""

    base_url: str = "https://kick.com"
    channel_path: str = "/api/v2/channels/{slug}"
    videos_path: str = "/api/v2/channels/{slug}/videos"
    video_path: str = "/api/v1/video/{uuid}"
    messages_path: str = "/api/v2/channels/{channel_id}/messages"
    # `httpx` worked from a home connection. `curl_cffi` (browser TLS fingerprint) is an
    # optional fallback if Cloudflare starts blocking plain clients; needs `curl_cffi`.
    http_backend: Literal["httpx", "curl_cffi"] = "httpx"
    impersonate: str = "chrome"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/131.0.0.0 Safari/537.36"
    )
    accept_language: str = "tr-TR,tr;q=0.9,en;q=0.8"
    # Chat replay returns ~5 s of messages after `start_time`; we step through the VOD.
    chat_window_seconds: float = 5.0
    # A window with at least this many messages may be truncated; then the next request
    # starts at the last message's second instead of skipping a whole window.
    chat_page_hint: int = 20
    request_interval_seconds: float = 0.2
    retries: int = 4
    download_concurrency: int = 6


class YouTubeConfig(_Strict):
    """Unofficial YouTube innertube parameters."""

    base_url: str = "https://www.youtube.com"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/131.0.0.0 Safari/537.36"
    )
    accept_language: str = "tr-TR,tr;q=0.9,en;q=0.8"
    # Pre-accepted consent cookie for EU/TR consent walls.
    consent_cookie: str = "SOCS=CAI"
    yt_dlp: str = "yt-dlp"
    vod_format: str = "bv*[height<=1080]+ba/b[height<=1080]/b"


class PanelConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 8765


class RecorderConfig(_Strict):
    buffer_minutes: int = 15
    quality: str = "1080p60,1080p,936p60,720p60,best"

    @property
    def quality_order(self) -> list[str]:
        return [q.strip() for q in self.quality.split(",") if q.strip()]


class DetectorWeights(_Strict):
    rate: float = 0.40
    uniq: float = 0.25
    emote: float = 0.20
    audio: float = 0.15
    # r3: from the transcript (0 effect while no transcript exists for that time)
    speech: float = 0.10
    vkw: float = 0.10


class DetectorConfig(_Strict):
    bin_seconds: int = 1
    burst_window: int = 5
    baseline_halflife: float = 300
    warmup_seconds: int = 180
    threshold: float = 3.0
    min_rate: float = 0.5
    min_ratio: float = 1.6
    cooldown: int = 45
    lag_seconds: float = 20
    pre_roll: float = 40
    post_roll: float = 25
    weights: DetectorWeights = Field(default_factory=DetectorWeights)
    # z-score denominators never go below these (window sums; audio in dB).
    floors: DetectorWeights = Field(
        default_factory=lambda: DetectorWeights(
            rate=1.0, uniq=1.0, emote=1.0, audio=3.0, speech=2.0, vkw=1.0
        )
    )
    peak_window: int = 10  # seconds after a trigger to search for the peak
    viewer_clip_bonus: float = 1.5
    viewer_clip_radius: float = 30


class LiveTranscribeConfig(_Strict):
    """r3: live transcript for the content path (ROADMAP 4.3b)."""

    enabled: bool = True
    model: str = "large-v3-turbo"
    chunk_seconds: float = 20
    overlap_seconds: float = 4
    beam_size: int = 1
    max_lag_seconds: float = 60


class ContentScanConfig(_Strict):
    """r3: content path, Haiku scans the transcript every minute (ROADMAP 4.3b)."""

    enabled: bool = True
    model: str = "claude-haiku-5-5"
    every_seconds: float = 60
    window_seconds: float = 90
    min_score: int = 70
    skip_if_silent_ratio: float = 0.7
    pad_seconds: float = 10
    max_candidate_seconds: float = 180
    chat_samples: int = 15


class EventsConfig(_Strict):
    """r3: platform event filter (ROADMAP 4.3 item 8b)."""

    sub_train_count: int = 5
    sub_train_window: float = 5
    suppress_after_sub_train: float = 20
    suppress_threshold_boost: float = 1.5
    raid_suppress_seconds: float = 60
    thanks_words: list[str] = Field(
        default_factory=lambda: [
            "hoş geldin", "hoşgeldin", "welcome", "sub", "abone", "teşekkürler", "thanks",
            "ty", "gifted", "hediye",
        ]
    )  # fmt: skip
    thanks_ratio: float = 0.5
    thanks_emote_factor: float = 0.3


class ViewerClipsConfig(_Strict):
    enabled: bool = True
    as_candidate: bool = True
    bonus: float = 1.5
    pad_seconds: float = 10


class MergeConfig(_Strict):
    radius_seconds: float = 20


class TranscribeConfig(_Strict):
    model: str = "large-v3"
    device: str = "auto"
    compute_type: str = "auto"
    language: str = "tr"
    beam_size: int = 5


class LLMConfig(_Strict):
    prefilter_model: str = "claude-haiku-5-5"
    judge_model: str = "claude-sonnet-5-5"
    max_candidates_per_hour: int = 12
    min_keep_score: int = 65
    monthly_budget_usd: float = 30
    prefilter_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    judge_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Server-side refusal fallback (beta) for models that support it.
    refusal_fallback: bool = True


class RenderConfig(_Strict):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    encoder: Literal["auto", "h264_nvenc", "libx264"] = "auto"
    loudness_lufs: float = -14
    min_len: float = 12
    max_len: float = 59


class PublishConfig(_Strict):
    min_gap_minutes: dict[str, int] = Field(
        default_factory=lambda: {"youtube": 90, "instagram": 120, "x": 60, "tiktok": 120}
    )
    daily_cap: dict[str, int] = Field(
        default_factory=lambda: {"youtube": 8, "instagram": 6, "x": 10, "tiktok": 6}
    )
    auto_publish_min_score: int = 85


class StreamerConfig(_Strict):
    display_name: str
    platform: Literal["twitch", "kick", "youtube"]
    channel: str
    enabled: bool = True
    layout: Literal["stack", "blur", "center"] = "stack"
    keywords: dict[str, float] = Field(default_factory=dict)
    detector_overrides: dict[str, Any] = Field(default_factory=dict)
    publish_targets: list[str] = Field(default_factory=list)
    auto_publish: bool = False


class Secrets(BaseModel):
    """Secrets read from the environment / `.env`. Never logged."""

    anthropic_api_key: SecretStr | None = None
    clipper_secret_key: SecretStr | None = None
    twitch_client_id: SecretStr | None = None
    twitch_client_secret: SecretStr | None = None
    youtube_api_key: SecretStr | None = None
    panel_password: SecretStr | None = None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Secrets:
        values = {name: env[name.upper()] for name in cls.model_fields if env.get(name.upper())}
        return cls(**values)


class Config(_Strict):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    panel: PanelConfig = Field(default_factory=PanelConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    twitch: TwitchConfig = Field(default_factory=TwitchConfig)
    kick: KickConfig = Field(default_factory=KickConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    recorder: RecorderConfig = Field(default_factory=RecorderConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    live_transcribe: LiveTranscribeConfig = Field(default_factory=LiveTranscribeConfig)
    content_scan: ContentScanConfig = Field(default_factory=ContentScanConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    viewer_clips: ViewerClipsConfig = Field(default_factory=ViewerClipsConfig)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    streamers: list[StreamerConfig] = Field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.paths.data_dir / "clipper.db"

    @property
    def log_dir(self) -> Path:
        return self.paths.data_dir / "logs"

    @property
    def model_dir(self) -> Path:
        return self.paths.data_dir / "models"


class ConfigError(Exception):
    """Raised with a Turkish, user-facing message when config cannot be loaded."""


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse a minimal `.env` file.

    Supports `KEY=value`, optional `export ` prefix, `#` comments, and single/double
    quoted values (double quotes understand `\\n`, `\\"` and `\\\\`).
    """
    result: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            raise ConfigError(f".env satır {lineno} okunamadı: {raw!r}")
        value = value.strip()
        if value[:1] in {'"', "'"}:
            quote = value[0]
            end = _find_closing_quote(value, quote)
            if end < 0:
                raise ConfigError(f".env satır {lineno}: kapanmayan tırnak")
            inner = value[1:end]
            if quote == '"':
                inner = (
                    inner.replace("\\\\", "\x00")
                    .replace("\\n", "\n")
                    .replace('\\"', '"')
                    .replace("\x00", "\\")
                )
            value = inner
        else:
            hash_at = value.find(" #")
            if hash_at >= 0:
                value = value[:hash_at].rstrip()
        result[key] = value
    return result


def _find_closing_quote(value: str, quote: str) -> int:
    i = 1
    while i < len(value):
        ch = value[i]
        if ch == "\\" and quote == '"':
            i += 2
            continue
        if ch == quote:
            return i
        i += 1
    return -1


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_dotenv(path.read_text(encoding="utf-8"))


def load_config(path: Path | None = None) -> Config:
    """Load `config.yaml` (or defaults if the file is absent)."""
    if path is None or not path.is_file():
        return Config()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} YAML olarak okunamadı: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} bir YAML sözlüğü olmalı")
    try:
        cfg = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path} geçersiz:\n{exc}") from exc
    if not cfg.paths.data_dir.is_absolute():
        cfg.paths.data_dir = (path.parent / cfg.paths.data_dir).resolve()
    return cfg


def load_secrets(dotenv_path: Path | None = None) -> Secrets:
    """Environment variables override values from `.env`."""
    env: dict[str, str] = {}
    if dotenv_path is not None:
        env.update(load_dotenv(dotenv_path))
    env.update({k: v for k, v in os.environ.items() if v})
    return Secrets.from_env(env)
