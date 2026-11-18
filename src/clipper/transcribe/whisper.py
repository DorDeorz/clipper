"""faster-whisper transcription. The model loads once and runs on a single worker thread
(VRAM is the limit); CTranslate2 releases the GIL, so the event loop stays free."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from clipper.config import TranscribeConfig
from clipper.log import get_logger
from clipper.util.fs import atomic_write_text

log = get_logger("whisper")


class TranscribeError(Exception):
    pass


class _Model(Protocol):
    def transcribe(self, audio: str, **kwargs: Any) -> tuple[Any, Any]: ...


def resolve_device(cfg: TranscribeConfig) -> tuple[str, str]:
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

    async def transcribe(self, wav: Path, out_json: Path) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(self._pool, self._transcribe_sync, wav)
        except TranscribeError:
            raise
        except Exception as exc:
            raise TranscribeError(f"transkript başarısız: {exc}") from exc
        atomic_write_text(out_json, json.dumps(result, ensure_ascii=False))
        return result

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
