"""`clipper report <session>`: one self-contained HTML file with our own SVG chart."""

from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path
from typing import Any

from clipper.util.fs import atomic_write_text
from clipper.util.timeutil import fmt_duration

W, H, PAD_L, PAD_B, PAD_T = 1200, 320, 48, 28, 12
MAX_POINTS = 1500


def _downsample(points: list[tuple[int, float]], n: int = MAX_POINTS) -> list[tuple[int, float]]:
    """Max-per-bucket downsampling (spikes survive)."""
    if len(points) <= n:
        return points
    step = len(points) / n
    out = []
    for i in range(n):
        bucket = points[int(i * step) : int((i + 1) * step)] or points[-1:]
        out.append(max(bucket, key=lambda p: p[1]))
    return out


def _polyline(
    points: list[tuple[int, float]], t0: int, t1: int, vmin: float, vmax: float, cls: str
) -> str:
    if not points:
        return ""
    span_t = max(t1 - t0, 1)
    span_v = (vmax - vmin) or 1.0
    coords = " ".join(
        f"{PAD_L + (t - t0) / span_t * (W - PAD_L - 8):.1f},"
        f"{PAD_T + (1 - (v - vmin) / span_v) * (H - PAD_T - PAD_B):.1f}"
        for t, v in _downsample(points)
    )
    return f'<polyline class="{cls}" points="{coords}"/>'


def render_report(conn: sqlite3.Connection, session_id: int, threshold: float) -> str:
    sess = conn.execute(
        "SELECT s.*, st.display_name, st.platform FROM sessions s JOIN streamers st"
        " ON st.id = s.streamer_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if sess is None:
        raise KeyError(session_id)
    rows = conn.execute(
        "SELECT t, msgs, score, audio_db FROM signal_seconds WHERE session_id = ? ORDER BY t",
        (session_id,),
    ).fetchall()
    cands = conn.execute(
        "SELECT * FROM candidates WHERE session_id = ? ORDER BY peak_ts", (session_id,)
    ).fetchall()
    t0 = rows[0]["t"] if rows else 0
    t1 = rows[-1]["t"] if rows else 1
    offline = sess["source"] == "vod"

    def label(t: float) -> str:
        return fmt_duration(t - (0 if offline else t0))

    score_pts = [(r["t"], float(r["score"] or 0)) for r in rows]
    msg_pts = [(r["t"], float(r["msgs"] or 0)) for r in rows]
    smin = min([p[1] for p in score_pts] + [0.0])
    smax = max([p[1] for p in score_pts] + [threshold + 1])
    mmax = max([p[1] for p in msg_pts] + [1.0])

    def x_of(t: float) -> float:
        return PAD_L + (t - t0) / max(t1 - t0, 1) * (W - PAD_L - 8)

    thr_y = PAD_T + (1 - (threshold - smin) / ((smax - smin) or 1)) * (H - PAD_T - PAD_B)
    marks = []
    for i, c in enumerate(cands, start=1):
        xs, xe, xp = x_of(c["win_start"]), x_of(c["win_end"]), x_of(c["peak_ts"])
        marks.append(
            f'<rect class="win" x="{xs:.1f}" y="{PAD_T}" width="{max(xe - xs, 1):.1f}"'
            f' height="{H - PAD_T - PAD_B}"/><line class="peak" x1="{xp:.1f}" x2="{xp:.1f}"'
            f' y1="{PAD_T}" y2="{H - PAD_B}"/><text class="mk" x="{xp + 3:.1f}"'
            f' y="{PAD_T + 12}">{i}</text>'
        )
    ticks = []
    for k in range(7):
        t = t0 + (t1 - t0) * k / 6
        ticks.append(
            f'<text class="tick" x="{x_of(t):.1f}" y="{H - 8}"'
            f' text-anchor="middle">{label(t)}</text>'
        )
    svg = (
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Sinyal grafiği">'
        + _polyline(msg_pts, t0, t1, 0, mmax, "msgs")
        + _polyline(score_pts, t0, t1, smin, smax, "score")
        + f'<line class="thr" x1="{PAD_L}" x2="{W - 8}" y1="{thr_y:.1f}" y2="{thr_y:.1f}"/>'
        + "".join(marks)
        + "".join(ticks)
        + "</svg>"
    )

    cards = []
    for i, c in enumerate(cands, start=1):
        f: dict[str, Any] = json.loads(c["features"])
        z = f.get("z", {})
        zs = " · ".join(f"{k} {v:+.1f}" for k, v in z.items())
        msgs = "".join(
            f"<li><span>{m['count']}×</span> {html.escape(m['text'])}</li>"
            for m in f.get("top_messages", [])
        )
        cards.append(
            f'<section class="card"><h3>#{i} · {label(c["peak_ts"])}'
            f" <small>skor {c['det_score']:.2f}</small></h3>"
            f'<p class="meta">Pencere {label(c["win_start"])} – {label(c["win_end"])}'
            f" ({c['win_end'] - c['win_start']:.0f} sn) · z: {html.escape(zs)}"
            f" · durum: {html.escape(c['status'])}</p><ol>{msgs}</ol></section>"
        )
    title = f"{sess['display_name']} · session #{session_id}"
    sub = " · ".join(
        p
        for p in (
            sess["title"],
            f"{len(rows)} sn sinyal",
            f"{len(cands)} aday",
            f"eşik {threshold}",
        )
        if p
    )
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#fafafa;--fg:#1d1d1f;--muted:#6e6e73;--card:#fff;--line:#e5e5ea;
--score:#d9480f;--msgs:#adb5bd;--win:rgba(25,113,194,.12);--peak:#1971c2}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#f2f2f7;--muted:#a1a1a6;
--card:#1f1f22;--line:#333;--score:#ff8a50;--msgs:#5c5f66;--win:rgba(77,171,247,.15);
--peak:#4dabf7}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1240px;margin:auto}} h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--muted);margin:0 0 16px}}
svg{{width:100%;height:auto;background:var(--card);border:1px solid var(--line);
border-radius:8px}}
.score{{fill:none;stroke:var(--score);stroke-width:1.5}}
.msgs{{fill:none;stroke:var(--msgs);stroke-width:1}}
.thr{{stroke:var(--score);stroke-dasharray:4 4;opacity:.6}}
.win{{fill:var(--win)}} .peak{{stroke:var(--peak);stroke-width:1.5}}
.mk{{fill:var(--peak);font-size:12px;font-weight:600}}
.tick{{fill:var(--muted);font-size:11px}}
.legend{{color:var(--muted);font-size:13px;margin:6px 0 20px}}
.legend b{{display:inline-block;width:14px;height:3px;vertical-align:middle;margin:0 4px 0 12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}}
.card h3{{margin:0 0 4px;font-size:16px}} .card small{{color:var(--muted);font-weight:400}}
.meta{{color:var(--muted);font-size:13px;margin:0 0 8px}}
ol{{margin:0;padding-left:20px;font-size:14px}} li span{{color:var(--muted)}}
</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="sub">{html.escape(sub)}</p>
{svg}
<p class="legend"><b style="background:var(--score)"></b>birleşik skor
<b style="background:var(--msgs)"></b>mesaj/sn
<b style="background:var(--peak)"></b>aday tepe noktası (mavi alan: kesit penceresi)</p>
<div class="grid">{"".join(cards) or "<p>Aday yok.</p>"}</div>
</main></body></html>
"""


def write_report(
    conn: sqlite3.Connection, session_id: int, threshold: float, out: Path | None = None
) -> Path:
    row = conn.execute("SELECT dir FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(session_id)
    path = out or Path(row["dir"]) / "report.html"
    atomic_write_text(path, render_report(conn, session_id, threshold))
    return path
