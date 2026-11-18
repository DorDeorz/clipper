"""`clipper learn`: dataset, training, time-split report, versioned suggestions,
apply / undo (ROADMAP 4.9 Kademe 2).

Target: approved / edited = 1, rejected = 0 (human decisions in `feedback`). Candidates
the LLM dropped and nobody reviewed are extra negatives at half weight.

Two models: `pre` sees only detector-side features (it can run before any LLM call:
the optional pre-LLM gate) and `full` also sees the LLM scores (used to suggest
`min_keep_score` / `auto_publish_min_score`). Suggestions are never applied
automatically: the panel's "Öneriyi uygula" writes them, "Geri al" restores the
previous values.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from clipper import settings
from clipper.config import Config
from clipper.feedback import features_snapshot
from clipper.learn.model import LogReg, fit, log_loss, precision_at_k, roc_auc
from clipper.log import get_logger

log = get_logger("learn")

Z_KEYS = ("rate", "uniq", "emote", "audio", "speech", "vkw")
PRE_NAMES = [
    *(f"z_{k}" for k in Z_KEYS),
    "det_score",
    "content_score",
    "has_content",
    "viewer_clip",
    "n_sources",
    "events",
    "window_len",
]
FULL_NAMES = [*PRE_NAMES, "prefilter_score", "judge_score", "has_judge"]
LLM_REJECT_WEIGHT = 0.5


class LearnError(Exception):
    pass


class NotEnoughData(LearnError):
    pass


@dataclass
class Row:
    clip_id: int
    streamer_id: int
    ts: float
    label: int
    weight: float
    human: bool
    snap: dict[str, Any]


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def pre_vector(snap: dict[str, Any]) -> list[float]:
    feats = snap.get("features") or {}
    z = feats.get("z") or {}
    sources = snap.get("sources") or ["reactive"]
    win = snap.get("window") or [0.0, 0.0]
    content = snap.get("content_score")
    return [
        *(_f(z.get(k)) for k in Z_KEYS),
        _f(snap.get("det_score")),
        _f(content),
        1.0 if "content" in sources else 0.0,
        1.0 if "viewer_clip" in sources or feats.get("viewer_clips") else 0.0,
        float(len(sources)),
        1.0 if feats.get("events") else 0.0,
        _f(win[1]) - _f(win[0]),
    ]


def full_vector(snap: dict[str, Any]) -> list[float]:
    judge = snap.get("judge_score")
    return [
        *pre_vector(snap),
        _f(snap.get("prefilter_score"), 50.0),
        _f(judge, 50.0),
        0.0 if judge is None else 1.0,
    ]


def _iso_ts(s: str | None) -> float:
    try:
        return datetime.fromisoformat(s or "").timestamp()
    except ValueError:
        return 0.0


def dataset(conn: sqlite3.Connection, streamer_id: int | None = None) -> list[Row]:
    rows: list[Row] = []
    seen: set[int] = set()
    # Latest human decision per clip.
    for r in conn.execute(
        "SELECT f.clip_id, f.decision, f.features_snapshot, f.created_at, s.streamer_id"
        " FROM feedback f JOIN clips k ON k.id = f.clip_id"
        " JOIN candidates c ON c.id = k.candidate_id JOIN sessions s ON s.id = c.session_id"
        " WHERE f.id IN (SELECT MAX(id) FROM feedback GROUP BY clip_id)"
    ).fetchall():
        if streamer_id is not None and r["streamer_id"] != streamer_id:
            continue
        seen.add(r["clip_id"])
        rows.append(
            Row(
                r["clip_id"],
                r["streamer_id"],
                float(r["created_at"]),
                0 if r["decision"] == "rejected" else 1,
                1.0,
                True,
                json.loads(r["features_snapshot"]),
            )
        )
    for r in conn.execute(
        "SELECT k.id AS clip_id, c.created_at, s.streamer_id FROM clips k"
        " JOIN candidates c ON c.id = k.candidate_id JOIN sessions s ON s.id = c.session_id"
        " WHERE c.status = 'rejected'"
    ).fetchall():
        if r["clip_id"] in seen or (streamer_id is not None and r["streamer_id"] != streamer_id):
            continue
        rows.append(
            Row(
                r["clip_id"],
                r["streamer_id"],
                _iso_ts(r["created_at"]),
                0,
                LLM_REJECT_WEIGHT,
                False,
                features_snapshot(conn, r["clip_id"]),
            )
        )
    rows.sort(key=lambda x: x.ts)
    return rows


def _arrays(rows: list[Row], full: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    vec = full_vector if full else pre_vector
    x = np.asarray([vec(r.snap) for r in rows], dtype=float)
    y = np.asarray([r.label for r in rows], dtype=float)
    w = np.asarray([r.weight for r in rows], dtype=float)
    return x, y, w


def _metrics(model: LogReg, rows: list[Row], full: bool) -> dict[str, Any]:
    human = [r for r in rows if r.human]
    if not human:
        return {"n": 0}
    x, y, _ = _arrays(human, full)
    p = model.predict(x)
    det = np.asarray([_f(r.snap.get("det_score")) for r in human])
    k = max(1, min(10, len(human) // 3))
    out = {
        "n": len(human),
        "positive_rate": round(float(y.mean()), 3),
        "auc": _r(roc_auc(y, p)),
        "auc_det_score": _r(roc_auc(y, det)),
        f"precision_at_{k}": _r(precision_at_k(y, p, k)),
        f"precision_at_{k}_det_score": _r(precision_at_k(y, det, k)),
        "log_loss": round(log_loss(y, p), 4),
    }
    if full:
        judge = np.asarray([_f(r.snap.get("judge_score"), 50.0) for r in human])
        out["auc_judge_score"] = _r(roc_auc(y, judge))
    return out


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 4)


def _train(
    rows: list[Row], cfg: Config, prior: dict[str, LogReg] | None = None
) -> tuple[dict[str, LogReg], dict[str, Any]]:
    """Fit pre + full on the older part, report on the newest `test_fraction` of human
    decisions, then refit on everything."""
    lc = cfg.learn
    human_idx = [i for i, r in enumerate(rows) if r.human]
    cut = human_idx[int(len(human_idx) * (1 - lc.test_fraction))] if human_idx else len(rows)
    train, test = rows[:cut], rows[cut:]
    models: dict[str, LogReg] = {}
    report: dict[str, Any] = {"train": sum(r.human for r in train), "test": len(test)}
    for name, full in (("pre", False), ("full", True)):
        names = FULL_NAMES if full else PRE_NAMES
        kw: dict[str, Any] = {"l2": lc.l2}
        if prior is not None:
            kw.update(prior=prior[name], prior_strength=lc.shrinkage)
        if train and len({r.label for r in train}) > 1:
            x, y, w = _arrays(train, full)
            report[name] = _metrics(fit(x, y, names, sample_weight=w, **kw), test, full)
        x, y, w = _arrays(rows, full)
        models[name] = fit(x, y, names, sample_weight=w, **kw)
    return models, report


def suggest_weights(model: LogReg, current: dict[str, float]) -> dict[str, float]:
    raw = model.raw_weights()
    w = {k: max(0.0, raw[f"z_{k}"]) for k in Z_KEYS}
    total = sum(w.values())
    if total <= 0:
        return dict(current)
    scale = sum(current.get(k, 0.0) for k in Z_KEYS) / total
    return {k: round(v * scale, 3) for k, v in w.items()}


def suggest_threshold(rows: list[Row], weights: dict[str, float], current: float) -> float:
    pos = [r for r in rows if r.human and r.label == 1]
    if len(pos) < 10:
        return current
    scores = [
        sum(weights[k] * _f(((r.snap.get("features") or {}).get("z") or {}).get(k)) for k in Z_KEYS)
        for r in pos
    ]
    return round(float(np.clip(np.percentile(scores, 10), 1.5, 6.0)), 2)


def suggest_score_floor(rows: list[Row], precision: float, current: int) -> int:
    """Lowest judge score where the approval rate of everything ≥ it reaches `precision`."""
    pts = [(int(_f(r.snap.get("judge_score"), -1)), r.label) for r in rows if r.human]
    pts = [p for p in pts if p[0] >= 0]
    for s in range(40, 100):
        above = [lab for sc, lab in pts if sc >= s]
        if len(above) >= 10 and sum(above) / len(above) >= precision:
            return s
    return current


def train_all(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    rows = dataset(conn)
    n_human = sum(r.human for r in rows)
    if n_human < cfg.learn.min_decisions:
        raise NotEnoughData(
            f"yetersiz veri: {n_human} karar var, en az {cfg.learn.min_decisions} gerekiyor"
        )
    if len({r.label for r in rows}) < 2:
        raise NotEnoughData("yetersiz veri: sadece tek tür karar (hep onay ya da hep red) var")
    glob, report = _train(rows, cfg)
    current = cfg.detector.weights.model_dump()
    weights = suggest_weights(glob["pre"], current)
    result: dict[str, Any] = {
        "created_at": time.time(),
        "decisions": n_human,
        "rows": len(rows),
        "global": {
            "models": {k: m.to_json() for k, m in glob.items()},
            "report": report,
            "suggest": {
                "weights": weights,
                "threshold": suggest_threshold(rows, weights, cfg.detector.threshold),
                "min_keep_score": suggest_score_floor(rows, 0.5, cfg.llm.min_keep_score),
                "auto_publish_min_score": suggest_score_floor(
                    rows, 0.9, cfg.publish.auto_publish_min_score
                ),
            },
            "current": {
                "weights": current,
                "threshold": cfg.detector.threshold,
                "min_keep_score": cfg.llm.min_keep_score,
                "auto_publish_min_score": cfg.publish.auto_publish_min_score,
            },
        },
        "streamers": {},
    }
    by_streamer: dict[int, list[Row]] = {}
    for r in rows:
        by_streamer.setdefault(r.streamer_id, []).append(r)
    for sid, srows in by_streamer.items():
        n = sum(r.human for r in srows)
        if n < cfg.learn.min_streamer_decisions or len({r.label for r in srows}) < 2:
            continue
        models, rep = _train(srows, cfg, prior=glob)
        w = suggest_weights(models["pre"], current)
        result["streamers"][str(sid)] = {
            "decisions": n,
            "models": {k: m.to_json() for k, m in models.items()},
            "report": rep,
            "suggest": {
                "weights": w,
                "threshold": suggest_threshold(srows, w, cfg.detector.threshold),
            },
        }
    return result


# ---- versions -----------------------------------------------------------------------------


@dataclass
class Store:
    root: Path
    versions: list[int] = field(default_factory=list)

    @classmethod
    def at(cls, cfg: Config) -> Store:
        root = cfg.model_dir / "learned"
        vs = sorted(
            int(p.stem[1:]) for p in root.glob("v*.json") if p.stem[1:].isdigit()
        ) if root.is_dir() else []  # fmt: skip
        return cls(root, vs)

    def path(self, version: int) -> Path:
        return self.root / f"v{version}.json"

    def save(self, result: dict[str, Any]) -> int:
        version = (self.versions[-1] + 1) if self.versions else 1
        self.root.mkdir(parents=True, exist_ok=True)
        result = {"version": version, **result}
        tmp = self.path(version).with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path(version))
        self.versions.append(version)
        return version

    def load(self, version: int) -> dict[str, Any]:
        p = self.path(version)
        if not p.is_file():
            raise LearnError(f"model sürümü v{version} yok")
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data


def learn(conn: sqlite3.Connection, cfg: Config) -> tuple[int, dict[str, Any]]:
    result = train_all(conn, cfg)
    version = Store.at(cfg).save(result)
    log.info("model trained", extra={"version": version, "decisions": result["decisions"]})
    return version, result


def summaries(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    store = Store.at(cfg)
    applied = settings.get(conn, settings.LEARN_APPLIED)
    out = []
    for v in reversed(store.versions[-10:]):
        d = store.load(v)
        out.append(
            {
                "version": v,
                "created_at": d.get("created_at"),
                "decisions": d.get("decisions"),
                "report": d["global"]["report"],
                "suggest": d["global"]["suggest"],
                "current": d["global"]["current"],
                "streamers": {
                    k: {"decisions": s["decisions"], "suggest": s["suggest"], "report": s["report"]}
                    for k, s in d.get("streamers", {}).items()
                },
            }
        )
    decisions = conn.execute("SELECT COUNT(DISTINCT clip_id) FROM feedback").fetchone()[0]
    return {
        "versions": out,
        "applied": applied.get("version") if applied else None,
        "decisions": decisions,
        "min_decisions": cfg.learn.min_decisions,
    }


def apply(conn: sqlite3.Connection, cfg: Config, version: int) -> None:
    """Write the suggestions: detector weights/threshold into each streamer's overrides
    (its own model if it has one, else the global), score floors into `settings`."""
    data = Store.at(cfg).load(version)
    previous = settings.get(conn, settings.LEARN_APPLIED)
    if previous is not None:
        undo(conn)
    before: dict[str, Any] = {
        "overrides": {},
        "min_keep_score": settings.get(conn, settings.MIN_KEEP_SCORE),
        "auto_publish_min_score": settings.get(conn, settings.AUTO_PUBLISH_MIN_SCORE),
    }
    g = data["global"]["suggest"]
    for st in conn.execute("SELECT id, detector_overrides FROM streamers").fetchall():
        before["overrides"][str(st["id"])] = st["detector_overrides"]
        over = json.loads(st["detector_overrides"]) if st["detector_overrides"] else {}
        sug = (data.get("streamers", {}).get(str(st["id"])) or {}).get("suggest") or g
        over["weights"] = {**cfg.detector.weights.model_dump(), **sug["weights"]}
        over["threshold"] = sug["threshold"]
        conn.execute(
            "UPDATE streamers SET detector_overrides = ? WHERE id = ?",
            (json.dumps(over, ensure_ascii=False), st["id"]),
        )
    settings.put(conn, settings.MIN_KEEP_SCORE, g["min_keep_score"])
    settings.put(conn, settings.AUTO_PUBLISH_MIN_SCORE, g["auto_publish_min_score"])
    settings.put(
        conn,
        settings.LEARN_APPLIED,
        {"version": version, "applied_at": time.time(), "before": before},
    )
    log.info("learned model applied", extra={"version": version})


def undo(conn: sqlite3.Connection) -> int | None:
    applied = settings.get(conn, settings.LEARN_APPLIED)
    if applied is None:
        return None
    before = applied["before"]
    for sid, raw in before["overrides"].items():
        conn.execute("UPDATE streamers SET detector_overrides = ? WHERE id = ?", (raw, int(sid)))
    for key, name in (
        (settings.MIN_KEEP_SCORE, "min_keep_score"),
        (settings.AUTO_PUBLISH_MIN_SCORE, "auto_publish_min_score"),
    ):
        if before.get(name) is None:
            settings.delete(conn, key)
        else:
            settings.put(conn, key, before[name])
    settings.delete(conn, settings.LEARN_APPLIED)
    log.info("learned model undone", extra={"version": applied["version"]})
    return int(applied["version"])


# ---- pre-LLM gate -------------------------------------------------------------------------


def gate_model(conn: sqlite3.Connection, cfg: Config, streamer_id: int) -> LogReg | None:
    applied = settings.get(conn, settings.LEARN_APPLIED)
    if applied is None:
        return None
    try:
        data = Store.at(cfg).load(int(applied["version"]))
    except LearnError:
        return None
    own = data.get("streamers", {}).get(str(streamer_id))
    return LogReg.from_json((own or data["global"])["models"]["pre"])


def gate_probability(model: LogReg, snap: dict[str, Any]) -> float:
    return float(model.predict(np.asarray([pre_vector(snap)], dtype=float))[0])
