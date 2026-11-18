"""clipper command line interface (argparse only)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from clipper import __version__, chatlog, db, ingest
from clipper.config import Config, ConfigError, load_config, load_secrets
from clipper.log import get_logger, setup_logging
from clipper.media import MediaError
from clipper.platforms.twitch.gql import TwitchGQLError
from clipper.platforms.youtube.innertube import YouTubeParseError
from clipper.platforms.youtube.vod import YtDlpError
from clipper.util.crypto import generate_key
from clipper.util.timeutil import fmt_duration, parse_iso


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipper", description="Yayın klip otomasyonu.")
    parser.add_argument("--version", action="version", version=f"clipper {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="config dosyası (varsayılan: ./config.yaml)",
    )
    parser.add_argument(
        "--env", type=Path, default=Path(".env"), help=".env dosyası (varsayılan: ./.env)"
    )
    sub = parser.add_subparsers(dest="command", metavar="KOMUT")

    sub.add_parser("doctor", help="ortam kontrolü (ffmpeg, GPU, anahtarlar, disk)")

    p_db = sub.add_parser("db", help="veritabanı işlemleri")
    db_sub = p_db.add_subparsers(dest="db_command", metavar="İŞLEM")
    db_sub.add_parser("migrate", help="bekleyen migration'ları uygula")
    db_sub.add_parser("version", help="şema sürümünü göster")

    sub.add_parser("keygen", help="yeni CLIPPER_SECRET_KEY üret")

    p_vod = sub.add_parser("vod", help="VOD / yerel dosyadan session oluştur")
    src = p_vod.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", type=Path, help="yerel video dosyası")
    src.add_argument("--twitch-vod", metavar="ID", help="Twitch VOD id'si (anonim indirir)")
    src.add_argument("--youtube", metavar="VIDEO_ID", help="YouTube video id'si (yt-dlp + chat)")
    p_vod.add_argument(
        "--chat", type=Path, help="chat dosyası (.jsonl / .jsonl.gz / TwitchDownloader .json)"
    )
    p_vod.add_argument("--streamer", help="yayıncı adı (--video ile zorunlu)")
    p_vod.add_argument("--platform", choices=["twitch", "kick", "youtube"], default="twitch")
    p_vod.add_argument("--channel", help="kanal adı/slug (varsayılan: --streamer)")
    p_vod.add_argument(
        "--video-start",
        metavar="ZAMAN",
        help="videonun başladığı an (unix sn veya ISO-8601); mutlak zamanlı chat'i hizalamak için",
    )
    p_vod.add_argument("--title", help="session başlığı")

    p_chat = sub.add_parser("chat-import", help="chat dosyasını ortak jsonl biçimine çevir")
    p_chat.add_argument("input", type=Path, help="TwitchDownloader .json veya .jsonl")
    p_chat.add_argument("output", type=Path, help="çıktı .jsonl veya .jsonl.gz")
    return parser


def _parse_time(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return parse_iso(value)


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _run_vod(args: argparse.Namespace, cfg: Config) -> int:
    conn = db.open_db(cfg.db_path)
    try:
        if args.video is not None:
            if not args.streamer:
                print("Hata: --video ile --streamer gerekli.", file=sys.stderr)
                return 2
            result = await ingest.ingest_local(
                conn,
                cfg.paths.data_dir,
                args.video,
                args.chat,
                args.streamer,
                platform=args.platform,
                channel=args.channel,
                video_start=_parse_time(args.video_start) if args.video_start else None,
                title=args.title,
                ffprobe=cfg.paths.ffprobe,
            )
        else:
            async with httpx.AsyncClient(http2=False) as client:
                if args.twitch_vod:
                    result = await ingest.ingest_twitch_vod(
                        conn, cfg, client, args.twitch_vod, say=_say
                    )
                else:
                    result = await ingest.ingest_youtube_vod(
                        conn, cfg, client, args.youtube, say=_say
                    )
    finally:
        conn.close()
    print(
        f"Session #{result.session_id} hazır: {fmt_duration(result.duration)} video,"
        f" {result.chat_count} chat mesajı ({result.session_dir})"
    )
    return 0


def _ensure_utf8_output() -> None:
    """Turkish text and ✔/✘ marks must not crash on Windows code pages (e.g. cp1252 pipes)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if reconfigure is not None and encoding != "utf8":
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "keygen":
        print(f"CLIPPER_SECRET_KEY={generate_key()}")
        print(
            "Bu satırı .env dosyana ekle. Anahtarı kaybedersen kayıtlı hesaplar yeniden "
            "bağlanmalı.",
            file=sys.stderr,
        )
        return 0

    try:
        cfg = load_config(args.config)
        secrets = load_secrets(args.env)
    except ConfigError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_dir)
    log = get_logger("cli")
    log.info("command", extra={"command": args.command})

    if args.command == "doctor":
        from clipper.doctor import doctor

        return doctor(cfg, secrets)

    if args.command == "db":
        conn = db.connect(cfg.db_path)
        try:
            if args.db_command == "migrate":
                before = db.schema_version(conn)
                after = db.migrate(conn)
                print(f"Şema v{before} → v{after} ({cfg.db_path})")
            else:
                print(f"Şema sürümü: v{db.schema_version(conn)} ({cfg.db_path})")
        finally:
            conn.close()
        return 0

    if args.command == "chat-import":
        try:
            n = chatlog.write_jsonl(args.output, chatlog.load_any(args.input))
        except (chatlog.ChatFormatError, OSError) as exc:
            print(f"Hata: {exc}", file=sys.stderr)
            return 1
        print(f"{n} mesaj yazıldı: {args.output}")
        return 0

    if args.command == "vod":
        try:
            return asyncio.run(_run_vod(args, cfg))
        except (
            ingest.IngestError,
            chatlog.ChatFormatError,
            MediaError,
            TwitchGQLError,
            YouTubeParseError,
            YtDlpError,
            httpx.HTTPError,
        ) as exc:
            log.error("vod failed", extra={"error": str(exc)})
            print(f"Hata: {exc}", file=sys.stderr)
            return 1

    parser.error(f"bilinmeyen komut: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
