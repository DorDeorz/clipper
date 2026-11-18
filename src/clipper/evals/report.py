"""Eval reports (TESTPLAN 1): `eval/reports/<date>_<kind>.json` + a self-contained HTML."""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from clipper.util.fs import atomic_write_text

CSS = """body{font-family:system-ui,sans-serif;margin:24px;background:#fafafa;color:#222}
table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border:1px solid #ddd;
padding:6px 8px;text-align:left;vertical-align:top;font-size:14px}th{background:#eee}
.ok{color:#137333;font-weight:600}.bad{color:#b3261e;font-weight:600}
@media (prefers-color-scheme:dark){body{background:#1b1b1b;color:#eee}th{background:#333}
td,th{border-color:#444}.ok{color:#81c995}.bad{color:#f28b82}}"""


def write(
    reports_dir: Path, kind: str, summary: dict[str, Any], rows: list[dict[str, Any]]
) -> Path:
    stamp = time.strftime("%Y-%m-%d_%H%M")
    base = reports_dir / f"{stamp}_{kind}"
    data = {"summary": summary, "rows": rows, "created": time.time()}
    atomic_write_text(base.with_suffix(".json"), json.dumps(data, ensure_ascii=False, indent=1))
    atomic_write_text(base.with_suffix(".html"), render_html(kind, summary, rows))
    return base.with_suffix(".html")


def render_html(kind: str, summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    e = html.escape
    head = "".join(f"<tr><th>{e(str(k))}</th><td>{e(str(v))}</td></tr>" for k, v in summary.items())
    body = []
    for r in rows:
        bad = [c for c in r.get("checks", []) if not c["ok"]]
        cls = "ok" if r.get("ok") else "bad"
        detail = "<br>".join(f"{e(c['name'])}: {e(c['detail'])}" for c in bad) or "—"
        if r.get("error"):
            detail = f"hata: {e(r['error'])}<br>" + detail
        body.append(
            f"<tr><td>{e(r['id'])}</td><td>{e(r.get('title', ''))}</td>"
            f"<td class={cls}>{'GEÇTİ' if r.get('ok') else 'KALDI'}</td>"
            f"<td>{len(r.get('candidates', []))}</td><td>{len(r.get('clips', []))}</td>"
            f"<td>{r.get('seconds', '')}</td><td>{detail}</td></tr>"
        )
    return (
        f"<!doctype html><meta charset=utf-8><title>clipper eval {e(kind)}</title>"
        f"<style>{CSS}</style><h1>clipper eval: {e(kind)}</h1><table>{head}</table>"
        "<table><tr><th>ID</th><th>Senaryo</th><th>Sonuç</th><th>Aday</th><th>Klip</th>"
        f"<th>Süre (sn)</th><th>Başarısız kontroller</th></tr>{''.join(body)}</table>"
    )
