"""`clipper eval synthetic` (TESTPLAN seviye 3): S01–S18 through the whole pipeline.

record/cut -> detect -> content path (fake transcript) -> prefilter/judge -> render ->
publish planning (accounts are fake, nothing is uploaded). LLM answers come from
`llm_mode`: "oracle" (built-in deterministic fake, offline, $0), "record" (real Claude,
responses saved) or "replay" (saved responses only; a missing one fails the run).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import anthropic
import httpx2

from clipper import db
from clipper.config import Config, LLMConfig, PathsConfig, RenderConfig, TranscribeConfig
from clipper.content.batch import enqueue_content_path
from clipper.detect.pipeline import analyze_session
from clipper.evals.detect_eval import Check, _candidates, compare
from clipper.evals.scenarios import SCENARIOS, Scenario, build, load_session
from clipper.log import get_logger
from clipper.util.proc import no_window

log = get_logger("eval.synthetic")
LLMMode = Literal["oracle", "record", "replay"]
STORY_WORDS = ("market", "kasiyer", "amca", "müdür")


# ---- oracle LLM -------------------------------------------------------------------------


def _user_text(body: dict[str, Any]) -> str:
    out = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out += [b.get("text", "") for b in c if isinstance(b, dict)]
    return "\n".join(out)


def oracle_answer(body: dict[str, Any]) -> dict[str, Any]:
    """Deterministic answer by output schema: content scan scores story talk high, the
    prefilter keeps everything, the judge keeps everything and flags song lyrics."""
    props = set(body["output_config"]["format"]["schema"].get("properties", {}))
    text = _user_text(body).lower()
    if "best_start" in props:
        story = sum(w in text for w in STORY_WORDS) >= 2
        return {
            "score": 86 if story else 20,
            "kind": "story" if story else "none",
            "reason": "oracle",
            "best_start": 0.0,
            "best_end": 600.0,
        }
    if "rough_score" in props:
        return {"keep": True, "rough_score": 70, "reason": "oracle"}
    music = "la la" in text
    return {
        "keep": True,
        "score": 80,
        "reason": "oracle",
        "start": 5.0,
        "end": 45.0,
        "title": "Sentetik klip",
        "hook": "izle",
        "caption_fixes": [],
        "hashtags": ["#test"],
        "texts": {
            "youtube": {"title": "Sentetik klip", "description": "test"},
            "instagram": {"caption": "test"},
            "tiktok": {"caption": "test"},
            "x": {"text": "test"},
        },
        "flags": {
            "music_risk": music,
            "nsfw": False,
            "sensitive_topic": False,
            "needs_context": False,
        },
    }


def oracle_client() -> anthropic.AsyncAnthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        msg = {
            "id": "msg_oracle",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [
                {"type": "text", "text": json.dumps(oracle_answer(body), ensure_ascii=False)}
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
        return httpx2.Response(200, json=msg)

    return anthropic.AsyncAnthropic(
        api_key="oracle",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


# ---- checks on rendered clips -----------------------------------------------------------


def _ffprobe_json(ffprobe: str, path: Path) -> dict[str, Any]:
    res = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
        **no_window(),
    )
    return json.loads(res.stdout)


def _mean_rgb(ffmpeg: str, path: Path, at: float, crop: str | None = None) -> tuple[float, ...]:
    """Average colour of one frame (optionally a `w:h:x:y` crop) via a 1x1 scale."""
    vf = (f"crop={crop}," if crop else "") + "scale=1:1:flags=area"
    res = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            f"{at:.2f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture_output=True,
        check=True,
        **no_window(),
    )
    return tuple(float(b) for b in res.stdout[:3])


def check_clip(cfg: Config, s: Scenario, final: Path, clip_dir: Path) -> list[Check]:
    """RND-01..04 for one clip, plus the scenario's render expectations (S13, S14, S15)."""
    out: list[Check] = []
    info = _ffprobe_json(cfg.paths.ffprobe, final)
    v = next(x for x in info["streams"] if x["codec_type"] == "video")
    a = next((x for x in info["streams"] if x["codec_type"] == "audio"), None)
    fmt_ok = (
        (v["codec_name"], v.get("pix_fmt"), v["width"], v["height"])
        == ("h264", "yuv420p", 1080, 1920)
        and a is not None
        and a["codec_name"] == "aac"
        and (a["sample_rate"] == "48000")
    )
    out.append(
        Check(
            "RND-01 biçim",
            fmt_ok,
            f"{v['codec_name']} {v.get('pix_fmt')} "
            f"{v['width']}x{v['height']}, ses {a and a['codec_name']}",
        )
    )
    dur = float(info["format"]["duration"])
    rmin, rmax = cfg.render.min_len, cfg.render.max_len
    out.append(Check("RND-02 süre", rmin - 0.2 <= dur <= rmax + 0.2, f"{dur:.1f} sn"))
    ass = (
        (clip_dir / "clip.ass").read_text(encoding="utf-8")
        if (clip_dir / "clip.ass").is_file()
        else ""
    )
    lines = [ln.split(",,", 1)[1] for ln in ass.splitlines() if ln.startswith("Dialogue")]
    plain = [re.sub(r"\{[^}]*\}", "", ln) for ln in lines]
    out.append(
        Check(
            "RND-04 satır ≤ 18",
            all(len(p) <= 18 * 2 + 1 for p in plain),
            f"{max((len(p) for p in plain), default=0)} karakter",
        )
    )
    exp = s.expected
    if exp.caption_text:
        joined = " ".join(plain)
        out.append(
            Check(
                "S14 Türkçe büyük harf",
                exp.caption_text in joined or all(w in joined for w in exp.caption_text.split()),
                joined[:120],
            )
        )
    if exp.no_ad_frames:
        reds = [_mean_rgb(cfg.paths.ffmpeg, final, t) for t in _sample_times(dur)]
        red = [c for c in reds if c[0] > 150 and c[1] < 80 and c[2] < 80]
        out.append(Check("S13 reklam karesi yok", not red, f"{len(red)}/{len(reds)} kırmızı kare"))
    if exp.facecam_top:
        top = _mean_rgb(cfg.paths.ffmpeg, final, dur / 2, "1080:600:0:0")
        bottom = _mean_rgb(cfg.paths.ffmpeg, final, dur / 2, "1080:600:0:1320")
        ok = top[2] > 150 and top[0] < 80 and not (bottom[2] > 150 and bottom[0] < 80)
        out.append(Check("S15 facecam üstte", ok, f"üst {top}, alt {bottom}"))
    return out


def _sample_times(duration: float, n: int = 8) -> list[float]:
    return [duration * (i + 0.5) / n for i in range(n)]


# ---- one scenario -----------------------------------------------------------------------


@dataclass
class ScenarioResult:
    id: str
    title: str
    seconds: float
    candidates: list[dict[str, Any]]
    clips: list[dict[str, Any]]
    publications: int
    jobs: dict[str, int]
    checks: list[Check] = field(default_factory=list)
    usd: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)


def scenario_config(base: Config, s: Scenario, work: Path, built: Path, llm: LLMConfig) -> Config:
    return base.model_copy(
        update={
            "paths": base.paths.model_copy(update={"data_dir": work / "data"}),
            "transcribe": TranscribeConfig(
                backend="fake", fake_path=built / "fake_transcript.jsonl"
            ),
            "llm": llm,
        }
    )


async def run_scenario(
    s: Scenario,
    base: Config,
    out_dir: Path,
    *,
    llm_mode: LLMMode = "oracle",
    llm_cache: Path | None = None,
    api_key: str | None = None,
) -> ScenarioResult:
    from clipper.llm.client import LLMClient
    from clipper.publish.scheduler import maybe_auto_publish
    from clipper.stages import Stages
    from clipper.transcribe.whisper import Transcriber
    from clipper.worker import run_until_idle

    t_start = time.monotonic()
    built = build(s, out_dir / "inputs", video=True, ffmpeg=base.paths.ffmpeg)
    work = out_dir / "runs" / s.id
    if (work / "data").exists():
        shutil.rmtree(work / "data")
    llm_cfg = base.llm.model_copy(
        update={
            "cache_mode": "off" if llm_mode == "oracle" else llm_mode,
            "cache_dir": llm_cache or base.llm.cache_dir,
        }
    )
    cfg = scenario_config(base, s, work, built, llm_cfg)
    conn = db.open_db(cfg.db_path)
    res = ScenarioResult(s.id, s.title, 0.0, [], [], 0, {})
    transcriber = Transcriber(cfg.transcribe, cfg.model_dir)
    try:
        sid = load_session(conn, cfg.paths.data_dir, s, built, with_transcript=False)
        await analyze_session(conn, cfg, sid, with_audio=True)
        if s.transcript:
            enqueue_content_path(conn, cfg, sid)
        if llm_mode == "oracle":
            client = oracle_client()
        elif llm_mode == "replay":
            client = anthropic.AsyncAnthropic(api_key=api_key or "replay-only")
        else:
            if not api_key:
                raise RuntimeError("record modu için ANTHROPIC_API_KEY gerekli")
            client = anthropic.AsyncAnthropic(api_key=api_key)
        llm = LLMClient(conn, cfg.llm, client)

        def accounts(_platform: str) -> list[str]:
            return ["sentetik_test"]

        stages = Stages(
            conn,
            cfg,
            transcriber=transcriber,
            llm=llm,
            on_rendered=lambda cid: maybe_auto_publish(conn, cfg, cid, accounts),
        )
        await run_until_idle(conn, stages.handle, pools=("cpu", "gpu", "io"))
        res.candidates = _candidates(conn, sid)
        res.checks += compare(s, res.candidates)
        res.clips = _clips(conn, sid)
        for c in res.clips:
            if c["final"]:
                res.checks += [
                    Check(f"klip #{c['id']} {x.name}", x.ok, x.detail)
                    for x in check_clip(cfg, s, Path(c["final"]), Path(c["final"]).parent)
                ]
        rendered = [c for c in res.clips if c["final"]]
        if s.expected.count:
            res.checks.append(
                Check("E2E-02 klip render edildi", len(rendered) >= 1, f"{len(rendered)} klip")
            )
        res.publications = conn.execute(
            "SELECT COUNT(*) FROM publications p JOIN clips k ON k.id = p.clip_id"
            " JOIN candidates c ON c.id = k.candidate_id WHERE c.session_id = ?",
            (sid,),
        ).fetchone()[0]
        if s.expected.no_publish:
            res.checks.append(
                Check(
                    "E2E-04 yayın işi yok", res.publications == 0, f"{res.publications} yayın kaydı"
                )
            )
        if s.expected.publish_warning:
            flagged = any(c["music_risk"] for c in res.clips)
            res.checks.append(Check("S17 müzik uyarısı", flagged, "flags.music_risk"))
        res.jobs = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT status, COUNT(*) FROM jobs WHERE pool != 'pub' GROUP BY status"
            )
        }
        left = res.jobs.get("queued", 0) + res.jobs.get("running", 0)
        res.checks.append(
            Check("E2E-05 kuyruk boş", left == 0 and not res.jobs.get("dead"), str(res.jobs))
        )
        res.usd = float(conn.execute("SELECT COALESCE(SUM(usd), 0) FROM llm_usage").fetchone()[0])
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        log.warning("scenario failed", extra={"scenario": s.id, "error": res.error})
    finally:
        transcriber.close()
        conn.close()
    res.seconds = round(time.monotonic() - t_start, 1)
    return res


def _clips(conn: sqlite3.Connection, session_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT k.id, k.final_path, k.llm_score, k.llm_json, c.status FROM clips k"
        " JOIN candidates c ON c.id = k.candidate_id WHERE c.session_id = ?",
        (session_id,),
    ).fetchall()
    out = []
    for r in rows:
        data = json.loads(r["llm_json"]) if r["llm_json"] else {}
        flags = data.get("judge", {}).get("flags", {})
        out.append(
            {
                "id": r["id"],
                "final": r["final_path"],
                "score": r["llm_score"],
                "status": r["status"],
                "music_risk": bool(flags.get("music_risk")),
            }
        )
    return out


# ---- whole run + report -----------------------------------------------------------------


async def run_all(
    base: Config,
    out_dir: Path,
    *,
    only: Sequence[str] = (),
    llm_mode: LLMMode = "oracle",
    llm_cache: Path | None = None,
    api_key: str | None = None,
    say: Callable[[str], None] = print,
    monitor_interval: float = 10.0,
) -> list[ScenarioResult]:
    from clipper.evals.monitor import ResourceMonitor

    results = []
    current: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = out_dir / "metrics.csv"
    metrics.unlink(missing_ok=True)
    monitor = ResourceMonitor(
        metrics,
        disk=out_dir,
        db=lambda: current[-1] if current else None,
        interval=monitor_interval,
    )
    async with monitor:
        for s in SCENARIOS:
            if only and s.id not in only:
                continue
            # Clear the previous run's data before the monitor looks at this DB (on
            # Windows an open read handle would make run_scenario's rmtree fail).
            shutil.rmtree(out_dir / "runs" / s.id / "data", ignore_errors=True)
            current.append(out_dir / "runs" / s.id / "data" / "clipper.db")
            results.append(await _run_one(s, base, out_dir, llm_mode, llm_cache, api_key, say))
    return results


async def _run_one(
    s: Scenario,
    base: Config,
    out_dir: Path,
    llm_mode: LLMMode,
    llm_cache: Path | None,
    api_key: str | None,
    say: Callable[[str], None],
) -> ScenarioResult:
    say(f"{s.id} {s.title}...")
    r = await run_scenario(
        s, base, out_dir, llm_mode=llm_mode, llm_cache=llm_cache, api_key=api_key
    )
    bad = [c for c in r.checks if not c.ok]
    say(
        f"  {'GEÇTİ' if r.ok else 'KALDI'} ({r.seconds} sn, {len(r.clips)} klip)"
        + (f" hata: {r.error}" if r.error else "")
        + "".join(f"\n    - {c.name}: {c.detail}" for c in bad)
    )
    return r


def write_report(
    results: list[ScenarioResult],
    reports_dir: Path,
    llm_mode: str,
    metrics: Path | None = None,
) -> Path:
    from clipper.evals.monitor import summarize
    from clipper.evals.report import write

    rows = [
        {
            **{k: v for k, v in asdict(r).items() if k != "checks"},
            "ok": r.ok,
            "checks": [asdict(c) for c in r.checks],
        }
        for r in results
    ]
    summary = {
        "kind": "synthetic",
        "llm_mode": llm_mode,
        "passed": sum(r.ok for r in results),
        "total": len(results),
        "usd": round(sum(r.usd for r in results), 4),
        **({f"kaynak_{k}": v for k, v in summarize(metrics).items()} if metrics else {}),
    }
    return write(reports_dir, "synthetic", summary, rows)


def default_base(cfg: Config) -> Config:
    """The user's config with the parts that would touch real accounts neutralised."""
    return cfg.model_copy(update={"render": RenderConfig(**cfg.render.model_dump())})


__all__ = ["PathsConfig", "run_all", "run_scenario", "write_report"]
