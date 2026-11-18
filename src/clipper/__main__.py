"""clipper command line interface (argparse only)."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from clipper import __version__, chatlog, db, ingest
from clipper.config import Config, ConfigError, Secrets, load_config, load_secrets
from clipper.detect.pipeline import DetectError
from clipper.log import get_logger, setup_logging
from clipper.media import MediaError
from clipper.platforms.kick.api import KickError
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
    src.add_argument("--kick-vod", metavar="UUID", help="Kick VOD uuid'si (kick-vods ile bul)")
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
    p_vod.add_argument(
        "--no-detect", action="store_true", help="session oluşturduktan sonra dedektörü çalıştırma"
    )

    p_det = sub.add_parser("detect", help="session üzerinde dedektörü çalıştır")
    p_det.add_argument("session_id", type=int)
    p_det.add_argument("--force", action="store_true", help="mevcut adayları silip yeniden")
    p_det.add_argument("--no-audio", action="store_true", help="ses sinyalini kullanma")

    p_rep = sub.add_parser("report", help="session için HTML sinyal raporu üret")
    p_rep.add_argument("session_id", type=int)
    p_rep.add_argument("--out", type=Path, help="çıktı dosyası (varsayılan: session klasörü)")

    p_work = sub.add_parser(
        "work", help="kuyruktaki işleri (kesim, transkript, LLM) bitene kadar çalıştır"
    )
    p_work.add_argument(
        "--pool",
        choices=["cpu", "gpu", "io"],
        action="append",
        help="sadece bu havuz(lar) (varsayılan: hepsi)",
    )

    p_clips = sub.add_parser("clips", help="session kliplerini ve LLM kararlarını listele")
    p_clips.add_argument("session_id", type=int)
    p_clips.add_argument("--all", action="store_true", help="reddedilenleri de göster")

    p_kv = sub.add_parser("kick-vods", help="Kick kanalının bitmiş VOD'larını listele")
    p_kv.add_argument("slug", help="kanal adı (kick.com/<slug>)")

    p_tr = sub.add_parser("test-render", help="örnek videodan 9:16 render denemesi")
    p_tr.add_argument(
        "--layout", choices=["stack", "blur", "center"], action="append", help="(varsayılan: üçü)"
    )
    p_tr.add_argument("--out", type=Path, help="çıktı klasörü (varsayılan: data/test-render)")

    p_fc = sub.add_parser("facecam", help="yayıncının facecam konumunu Claude ile öner")
    p_fc.add_argument("streamer", help="yayıncı adı ya da kanal")
    p_fc.add_argument("--apply", action="store_true", help="öneriyi kaydet ve düzeni stack yap")

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
                if args.kick_vod:
                    result = await ingest.ingest_kick_vod(
                        conn, cfg, client, args.kick_vod, say=_say
                    )
                elif args.twitch_vod:
                    result = await ingest.ingest_twitch_vod(
                        conn, cfg, client, args.twitch_vod, say=_say
                    )
                else:
                    result = await ingest.ingest_youtube_vod(
                        conn, cfg, client, args.youtube, say=_say
                    )
        print(
            f"Session #{result.session_id} hazır: {fmt_duration(result.duration)} video,"
            f" {result.chat_count} chat mesajı ({result.session_dir})"
        )
        if not args.no_detect:
            rc = await _detect_and_report(conn, cfg, result.session_id, False, True)
            from clipper.content.batch import enqueue_content_path

            if enqueue_content_path(conn, cfg, result.session_id):
                print(
                    "İçerik yolu kuyruğa alındı: tam transkript + içerik taraması"
                    " `clipper work` ile çalışır."
                )
            return rc
    finally:
        conn.close()
    return 0


async def _detect_and_report(
    conn: sqlite3.Connection, cfg: Config, session_id: int, force: bool, with_audio: bool
) -> int:
    from clipper.detect.pipeline import analyze_session
    from clipper.detect.report import write_report

    res = await analyze_session(conn, cfg, session_id, force=force, with_audio=with_audio)
    path = write_report(conn, session_id, cfg.detector.threshold)
    print(f"Dedektör: {len(res.candidate_ids)} aday ({fmt_duration(res.seconds)} tarandı)")
    print(f"Rapor: {path}")
    return 0


async def _run_work(args: argparse.Namespace, cfg: Config, secrets: Secrets) -> int:
    from clipper.stages import Stages
    from clipper.transcribe.whisper import Transcriber
    from clipper.worker import run_until_idle

    pools = tuple(args.pool or ("cpu", "gpu", "io"))
    conn = db.open_db(cfg.db_path)
    transcriber = Transcriber(cfg.transcribe, cfg.model_dir) if "gpu" in pools else None
    llm = None
    if secrets.anthropic_api_key is not None:
        import anthropic

        from clipper.llm.client import LLMClient

        llm = LLMClient(
            conn,
            cfg.llm,
            anthropic.AsyncAnthropic(api_key=secrets.anthropic_api_key.get_secret_value()),
        )
    elif "io" in pools:
        _say("Uyarı: ANTHROPIC_API_KEY yok; LLM işleri (prefilter/judge) çalıştırılmayacak.")
        pools = tuple(p for p in pools if p != "io")
    stages = Stages(conn, cfg, transcriber=transcriber, llm=llm)
    try:
        stats = await run_until_idle(conn, stages.handle, pools=pools)  # type: ignore[arg-type]
    finally:
        if transcriber is not None:
            transcriber.close()
        conn.close()
    detail = ", ".join(f"{k}={v}" for k, v in sorted(stats.by_type.items())) or "-"
    print(f"İşler bitti: {stats.done} başarılı ({detail}), {stats.failed} hata, {stats.dead} ölü")
    from clipper.llm.client import month_spend

    conn = db.open_db(cfg.db_path)
    try:
        print(f"Bu ayki LLM harcaması: ${month_spend(conn):.4f}")
    finally:
        conn.close()
    return 0 if stats.dead == 0 else 1


def _print_clips(conn: sqlite3.Connection, session_id: int, show_all: bool) -> int:
    rows = conn.execute(
        "SELECT c.id, c.status, c.det_score, c.peak_ts, k.llm_score, k.title, k.start_rel,"
        " k.end_rel, k.llm_json FROM candidates c LEFT JOIN clips k ON k.candidate_id = c.id"
        " WHERE c.session_id = ? ORDER BY COALESCE(k.llm_score, -1) DESC, c.det_score DESC",
        (session_id,),
    ).fetchall()
    if not rows:
        print(f"session #{session_id} için aday yok")
        return 1
    import json as _json

    for r in rows:
        if not show_all and r["status"] == "rejected":
            continue
        data = _json.loads(r["llm_json"]) if r["llm_json"] else {}
        reason = (data.get("judge") or data.get("prefilter") or {}).get("reason", "")
        span = f"{r['start_rel']:.1f}-{r['end_rel']:.1f}s" if r["start_rel"] is not None else "-"
        score = r["llm_score"] if r["llm_score"] is not None else "-"
        print(
            f"#{r['id']:<5} {r['status']:<12} det={r['det_score']:.2f} llm={score!s:<4}"
            f" {span:<12} {r['title'] or ''}"
        )
        if reason:
            print(f"        {reason}")
    total = conn.execute("SELECT COUNT(*), COALESCE(SUM(usd), 0) FROM llm_usage").fetchone()
    print(f"LLM çağrısı: {total[0]}, toplam maliyet ${total[1]:.4f}")
    return 0


async def _kick_vods(cfg: Config, slug: str) -> int:
    async with httpx.AsyncClient() as client:
        videos = await ingest.make_kick_api(cfg, client).videos(slug)
    done = [v for v in videos if not v.get("is_live") and v.get("duration")]
    if not done:
        print(f"{slug}: herkese açık bitmiş VOD yok")
        return 1
    for v in done:
        uuid = (v.get("video") or {}).get("uuid", "?")
        cat = ((v.get("categories") or [{}])[0] or {}).get("name", "")
        print(
            f"{uuid}  {fmt_duration(float(v['duration']) / 1000):>8}  {v.get('start_time', '')}"
            f"  {cat}  {v.get('session_title') or ''}"
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

    if args.command in ("detect", "report"):
        from clipper.detect.report import write_report

        conn = db.open_db(cfg.db_path)
        try:
            if args.command == "detect":
                return asyncio.run(
                    _detect_and_report(conn, cfg, args.session_id, args.force, not args.no_audio)
                )
            print(f"Rapor: {write_report(conn, args.session_id, cfg.detector.threshold, args.out)}")
            return 0
        except (DetectError, KeyError, chatlog.ChatFormatError) as exc:
            msg = f"session #{exc.args[0]} bulunamadı" if isinstance(exc, KeyError) else exc
            print(f"Hata: {msg}", file=sys.stderr)
            return 1
        finally:
            conn.close()

    if args.command == "work":
        from clipper.llm.client import LLMError

        try:
            return asyncio.run(_run_work(args, cfg, secrets))
        except LLMError as exc:
            print(f"Hata: {exc}", file=sys.stderr)
            return 1

    if args.command == "kick-vods":
        try:
            return asyncio.run(_kick_vods(cfg, args.slug))
        except (KickError, httpx.HTTPError) as exc:
            print(f"Hata: {exc}", file=sys.stderr)
            return 1

    if args.command == "clips":
        conn = db.open_db(cfg.db_path)
        try:
            return _print_clips(conn, args.session_id, args.all)
        finally:
            conn.close()

    if args.command == "test-render":
        from clipper.cli_render import test_render

        out = args.out or cfg.paths.data_dir / "test-render"
        try:
            return asyncio.run(test_render(cfg, args.layout or ["stack", "blur", "center"], out))
        except MediaError as exc:
            print(f"Hata: {exc}", file=sys.stderr)
            return 1

    if args.command == "facecam":
        from clipper.cli_render import facecam
        from clipper.llm.client import LLMError

        try:
            return asyncio.run(facecam(cfg, secrets, args.streamer, args.apply))
        except (LLMError, MediaError) as exc:
            print(f"Hata: {exc}", file=sys.stderr)
            return 1

    if args.command == "vod":
        try:
            return asyncio.run(_run_vod(args, cfg))
        except (
            ingest.IngestError,
            chatlog.ChatFormatError,
            MediaError,
            TwitchGQLError,
            KickError,
            YouTubeParseError,
            YtDlpError,
            httpx.HTTPError,
            DetectError,
        ) as exc:
            log.error("vod failed", extra={"error": str(exc)})
            print(f"Hata: {exc}", file=sys.stderr)
            return 1

    parser.error(f"bilinmeyen komut: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
