"""clipper command line interface (argparse only)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from clipper import __version__, db
from clipper.config import ConfigError, load_config, load_secrets
from clipper.log import get_logger, setup_logging
from clipper.util.crypto import generate_key


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
    return parser


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

    parser.error(f"bilinmeyen komut: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
