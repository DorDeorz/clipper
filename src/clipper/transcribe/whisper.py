"""faster-whisper transcription. The model loads once and runs on a single worker thread
(VRAM is the limit); CTranslate2 releases the GIL, so the event loop stays free."""

from __future__ import annotations

import asyncio
import hashlib
import json
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from clipper.config import TranscribeConfig
from clipper.log import get_logger
from clipper.transcribe.cuda_libs import ensure_cuda_dlls
from clipper.util.fs import atomic_write_text

log = get_logger("whisper")


class TranscribeError(Exception):
    pass


class _Model(Protocol):
    def transcribe(self, audio: str, **kwargs: Any) -> tuple[Any, Any]: ...


def resolve_device(cfg: TranscribeConfig) -> tuple[str, str]:
    ensure_cuda_dlls()
    device = cfg.device
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = cfg.compute_type
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def _default_loader(cfg: TranscribeConfig, model_dir: Path) -> _Model:
    ensure_cuda_dlls()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise TranscribeError("faster-whisper kurulu değil (uv sync).") from exc
    device, compute = resolve_device(cfg)
    log.info(
        "loading whisper", extra={"model": cfg.model, "device": device, "compute_type": compute}
    )
    return WhisperModel(
        cfg.model, device=device, compute_type=compute, download_root=str(model_dir)
    )


class Transcriber:
    def __init__(
        self,
        cfg: TranscribeConfig,
        model_dir: Path,
        loader: Callable[[TranscribeConfig, Path], _Model] | None = None,
    ) -> None:
        self.cfg = cfg
        self.model_dir = model_dir
        self._loader = loader or _default_loader
        self._model: _Model | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")

    def _transcribe_sync(self, wav: Path) -> list[dict[str, Any]]:
        if self._model is None:
            self._model = self._loader(self.cfg, self.model_dir)
        segments, _info = self._model.transcribe(
            str(wav),
            language=self.cfg.language,
            word_timestamps=True,
            vad_filter=True,
            beam_size=self.cfg.beam_size,
            condition_on_previous_text=False,
        )
        out: list[dict[str, Any]] = []
        for seg in segments:  # generator: decoding happens while iterating
            words = [
                {
                    "w": w.word.strip(),
                    "s": round(w.start, 3),
                    "e": round(w.end, 3),
                    "p": round(w.probability, 3),
                }
                for w in (seg.words or [])
                if w.word.strip()
            ]
            out.append(
                {
                    "start": round(seg.start, 3),
                    "end": round(seg.end, 3),
                    "text": seg.text.strip(),
                    "words": words,
                }
            )
        return out

    async def transcribe(
        self, wav: Path, out_json: Path, *, origin: float | None = None
    ) -> list[dict[str, Any]]:
        """Segments with times relative to the WAV. `origin` is the session time of the
        WAV's 0 (needed only by the fake backend)."""
        if self.cfg.backend == "fake":
            result = fake_transcript(self.cfg, wav, origin)
        else:
            cached = self._cache_file(wav)
            if cached is not None and cached.is_file():
                result = json.loads(cached.read_text(encoding="utf-8"))
            else:
                loop = asyncio.get_running_loop()
                try:
                    result = await loop.run_in_executor(self._pool, self._transcribe_sync, wav)
                except TranscribeError:
                    raise
                except Exception as exc:
                    raise TranscribeError(f"transkript başarısız: {exc}") from exc
                if cached is not None:
                    atomic_write_text(cached, json.dumps(result, ensure_ascii=False))
        atomic_write_text(out_json, json.dumps(result, ensure_ascii=False))
        return result

    def _cache_file(self, wav: Path) -> Path | None:
        if self.cfg.cache_dir is None:
            return None
        h = hashlib.sha256()
        with wav.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        key = f"{self.cfg.model}|{self.cfg.language}|{self.cfg.beam_size}|{h.hexdigest()}"
        name = hashlib.sha256(key.encode()).hexdigest()
        return Path(self.cfg.cache_dir) / f"{name}.json"

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _wav_seconds(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (OSError, wave.Error, EOFError):
        return float("inf")


def fake_transcript(cfg: TranscribeConfig, wav: Path, origin: float | None) -> list[dict[str, Any]]:
    """TESTPLAN fake backend: lines from `fake_path` overlapping the WAV, shifted to WAV
    time, with evenly spaced word timestamps."""
    if cfg.fake_path is None or not Path(cfg.fake_path).is_file():
        raise TranscribeError("transcribe.backend=fake ama transcribe.fake_path dosyası yok")
    if origin is None:
        raise TranscribeError("sahte transkript için ses başlangıç zamanı (origin) gerekli")
    end = origin + _wav_seconds(wav)
    out: list[dict[str, Any]] = []
    for raw in Path(cfg.fake_path).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        line = json.loads(raw)
        s, e, text = float(line["start"]), float(line["end"]), str(line["text"])
        if e <= origin or s >= end:
            continue
        tokens = text.split()
        step = (e - s) / max(len(tokens), 1)
        words = []
        for i, tok in enumerate(tokens):
            ws, we = s + i * step, s + (i + 1) * step
            if we <= origin or ws >= end:
                continue
            words.append(
                {"w": tok, "s": round(max(ws, origin) - origin, 3),
                 "e": round(min(we, end) - origin, 3), "p": 1.0}
            )  # fmt: skip
        if not words:
            continue
        out.append(
            {"start": words[0]["s"], "end": words[-1]["e"],
             "text": " ".join(str(w["w"]) for w in words), "words": words}
        )  # fmt: skip
    return out
