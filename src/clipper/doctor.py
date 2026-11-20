"""`clipper doctor`: environment health report (green / yellow / red)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from clipper import db
from clipper.config import Config, Secrets
from clipper.ffmpeg_probe import nvenc_works, probe_ffmpeg, run_tool, which
from clipper.util.crypto import CryptoError, SecretBox
from clipper.util.fs import disk_usage, human_bytes

MIN_FREE_BYTES = 20 * 1024**3
DISK_FULL_RATIO = 0.85


class Status(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str


async def run_checks(cfg: Config, secrets: Secrets) -> list[Check]:
    checks: list[Check] = []
    checks.append(
        Check("Python", Status.OK, sys.version.split()[0])
        if sys.version_info[:2] == (3, 12)
        else Check("Python", Status.WARN, f"{sys.version.split()[0]} (3.12 bekleniyor)")
    )

    info = await probe_ffmpeg(cfg.paths.ffmpeg, cfg.paths.ffprobe)
    if info.ffmpeg is None or info.error:
        checks.append(Check("ffmpeg", Status.FAIL, info.error or "bulunamadı"))
    else:
        checks.append(Check("ffmpeg", Status.OK, f"{info.version} ({info.ffmpeg})"))
    if info.ffprobe is None:
        checks.append(Check("ffprobe", Status.FAIL, f"bulunamadı ({cfg.paths.ffprobe})"))
    else:
        checks.append(Check("ffprobe", Status.OK, f"{info.ffprobe_version} ({info.ffprobe})"))

    if info.ffmpeg is not None and not info.error:
        checks.append(
            Check("libass (altyazı)", Status.OK, "subtitles/ass filtreleri var")
            if info.has_libass
            else Check("libass (altyazı)", Status.FAIL, "ffmpeg libass olmadan derlenmiş")
        )
        if info.has_nvenc and await nvenc_works(info.ffmpeg):
            checks.append(Check("NVENC", Status.OK, "h264_nvenc çalışıyor"))
        elif info.has_libx264:
            why = "derlemede yok" if not info.has_nvenc else "GPU/sürücü kullanılamıyor"
            checks.append(Check("NVENC", Status.WARN, f"{why}; libx264 (CPU) kullanılacak"))
        else:
            checks.append(Check("NVENC", Status.FAIL, "ne h264_nvenc ne libx264 var"))

    checks.append(_check_fonts(cfg.paths.fonts_dir))
    checks.append(await _check_cuda())
    checks.append(_check_whisper())

    checks.append(
        Check("ANTHROPIC_API_KEY", Status.OK, "tanımlı")
        if secrets.anthropic_api_key
        else Check("ANTHROPIC_API_KEY", Status.WARN, "tanımlı değil (LLM işleri çalışmaz)")
    )
    checks.append(_check_secret_key(secrets))
    checks.append(_check_disk(cfg.paths.data_dir))
    checks.append(_check_db(cfg.db_path))
    return checks


async def _check_cuda() -> Check:
    smi = which("nvidia-smi")
    if smi is None:
        return Check("CUDA / GPU", Status.WARN, "nvidia-smi yok; whisper CPU modunda çalışır")
    try:
        res = await run_tool(
            str(smi),
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader",
            timeout=15,
        )
    except (OSError, TimeoutError) as exc:
        return Check("CUDA / GPU", Status.WARN, f"nvidia-smi çalışmadı: {exc}")
    line = res.stdout.strip().splitlines()[0] if res.stdout.strip() else ""
    if res.returncode != 0 or not line:
        return Check("CUDA / GPU", Status.WARN, "GPU bulunamadı; whisper CPU modunda çalışır")
    return Check("CUDA / GPU", Status.OK, line)


def _check_whisper() -> Check:
    if importlib.util.find_spec("faster_whisper") is None:
        return Check("faster-whisper", Status.WARN, "kurulu değil (Faz 3'te eklenecek)")
    from clipper.transcribe.cuda_libs import missing_cuda_dlls

    missing = missing_cuda_dlls()
    if missing and which("nvidia-smi") is not None:
        return Check(
            "faster-whisper",
            Status.FAIL,
            f"GPU var ama CUDA kütüphaneleri eksik ({', '.join(missing)}); `uv sync` çalıştır",
        )
    return Check("faster-whisper", Status.OK, "kurulu")


def _check_secret_key(secrets: Secrets) -> Check:
    name = "CLIPPER_SECRET_KEY"
    if secrets.clipper_secret_key is None:
        return Check(name, Status.WARN, "tanımlı değil; `uv run clipper keygen` ile üret")
    try:
        SecretBox(secrets.clipper_secret_key.get_secret_value())
    except CryptoError as exc:
        return Check(name, Status.FAIL, str(exc))
    return Check(name, Status.OK, "geçerli Fernet anahtarı")


def _check_disk(data_dir: Path) -> Check:
    try:
        usage = disk_usage(data_dir)
    except OSError as exc:
        return Check("Disk", Status.FAIL, f"okunamadı: {exc}")
    detail = (
        f"boş {human_bytes(usage.free)} / {human_bytes(usage.total)}"
        f" (%{usage.used_ratio * 100:.0f} dolu)"
    )
    if usage.used_ratio >= DISK_FULL_RATIO:
        return Check("Disk", Status.FAIL, detail + " - %85 üstü")
    if usage.free < MIN_FREE_BYTES:
        return Check("Disk", Status.WARN, detail + " - 20 GB altı")
    return Check("Disk", Status.OK, detail)


def _check_db(db_path: Path) -> Check:
    try:
        conn = db.open_db(db_path)
        version = db.schema_version(conn)
        conn.close()
    except Exception as exc:
        return Check("Veritabanı", Status.FAIL, f"{db_path}: {exc}")
    return Check("Veritabanı", Status.OK, f"{db_path} (şema v{version})")


_SYMBOL = {Status.OK: "✔", Status.WARN: "!", Status.FAIL: "✘"}
_COLOR = {Status.OK: "\033[32m", Status.WARN: "\033[33m", Status.FAIL: "\033[31m"}


def _use_color(stream: object) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ


def format_report(checks: list[Check], color: bool) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        mark = _SYMBOL[c.status]
        if color:
            mark = f"{_COLOR[c.status]}{mark}\033[0m"
        lines.append(f" {mark} {c.name.ljust(width)}  {c.detail}")
    fails = sum(c.status is Status.FAIL for c in checks)
    warns = sum(c.status is Status.WARN for c in checks)
    if fails:
        lines.append(f"\n{fails} kritik sorun, {warns} uyarı.")
    elif warns:
        lines.append(f"\nKritik sorun yok, {warns} uyarı.")
    else:
        lines.append("\nHer şey yeşil.")
    return "\n".join(lines)


def _check_fonts(fonts_dir: Path) -> Check:
    fonts = sorted(p.name for p in fonts_dir.glob("*.[ot]tf")) if fonts_dir.is_dir() else []
    if fonts:
        return Check("Altyazı fontu", Status.OK, ", ".join(fonts[:3]))
    return Check(
        "Altyazı fontu",
        Status.WARN,
        f"{fonts_dir} boş; sistem fontu kullanılacak (assets/fonts/README.md)",
    )


def doctor(cfg: Config, secrets: Secrets) -> int:
    checks = asyncio.run(run_checks(cfg, secrets))
    print(format_report(checks, color=_use_color(sys.stdout)))
    return 1 if any(c.status is Status.FAIL for c in checks) else 0
