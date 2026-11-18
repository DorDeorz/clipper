"use strict";
// clipper panel. Vanilla JS, no build step. All text from the server goes through
// textContent (never innerHTML), so titles and chat lines cannot inject markup.

const PLATFORMS = ["youtube", "instagram", "x", "tiktok"];
const SOURCE_LABEL = { reactive: "chat", content: "içerik", viewer_clip: "izleyici klibi" };
const REASON_LABEL = {
  not_funny: "komik değil", no_context: "bağlamsız", bad_cut: "kötü kesim",
  duplicate: "tekrar", music: "müzik", sensitive: "hassas", low_quality: "kalitesiz",
  other: "diğer",
};
const VIEW_EMPTY = {
  review: "İncelenecek klip yok.", approved: "Onaylanan klip yok.",
  rejected_user: "Reddettiğin klip yok.", rejected_llm: "LLM'in elediği aday yok.",
  working: "İşlemde aday yok.",
};
const state = { tab: "clips", view: "review", status: null, streamers: [], editing: null,
  rect: null, frame: null };

// ---- helpers ------------------------------------------------------------------------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function call(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(msg, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}

async function act(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg);
    return r;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

const media = (rel) => (rel ? `/media/${rel.split("/").map(encodeURIComponent).join("/")}` : null);
const fmtTime = (s) => {
  if (s === null || s === undefined) return "-";
  s = Math.round(s);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return (hh ? `${hh}:${String(mm).padStart(2, "0")}` : `${mm}`) + `:${String(ss).padStart(2, "0")}`;
};
const table = (el, head, rows) => {
  el.replaceChildren(h("tr", {}, head.map((c) => h("th", {}, c))), ...rows);
};

// ---- tabs ---------------------------------------------------------------------------

const LOADERS = {
  live: loadLive, clips: loadClips, pubs: loadPubs, streamers: loadStreamers,
  accounts: loadAccounts, costs: loadCosts,
};

function showTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll("#tabs button")) {
    b.classList.toggle("active", b.dataset.tab === tab);
  }
  for (const s of document.querySelectorAll("main > section")) s.hidden = s.id !== `tab-${tab}`;
  LOADERS[tab]();
}

async function loadStatus() {
  const s = await call("GET", "/api/status").catch(() => null);
  if (!s) return;
  state.status = s;
  document.getElementById("review-count").textContent = s.review_count || "";
  const q = Object.entries(s.queue).map(([k, v]) => `${k}=${v}`).join("  ");
  document.getElementById("queue").textContent = q ? `Kuyruk: ${q}` : "Kuyruk boş";
  document.getElementById("fc-suggest").hidden = !s.facecam_suggest;
}

// ---- live / signals -----------------------------------------------------------------

async function loadLiveNow() {
  const box = document.getElementById("live-now");
  const live = await call("GET", "/api/live").catch(() => []);
  if (!live.length) {
    box.replaceChildren(h("p", { class: "muted" }, "Şu an kaydedilen canlı yayın yok. Takip açık yayıncılar her dakika kontrol ediliyor."));
    return;
  }
  box.replaceChildren(...live.map((l) => h("article", { class: "card" },
    h("div", { class: "head" }, h("strong", {}, `● ${l.display_name}`), h("span", { class: "muted" }, `${l.platform}/${l.channel}`)),
    h("div", { class: "muted" }, `${l.title || ""} · ${l.category || ""}`),
    h("div", { class: "muted" }, `Session #${l.session_id} · ${l.candidates} aday · başladı ${(l.started_at || "").slice(11, 16)}`),
    h("div", { class: "row" },
      h("button", { onclick: async () => {
        const r = await act(() => call("POST", `/api/live/${l.streamer_id}/mark`));
        if (r) toast(`İşaretlendi (${fmtTime(r.ts)}). Gecikme ayarı için: clipper calibrate ${l.channel}`);
      } }, "Şimdi oldu"),
      h("button", { onclick: () => { document.getElementById("session-select").value = l.session_id; loadSignals(); } }, "Grafiği göster")))));
}

async function loadLive() {
  await loadLiveNow();
  const sessions = await act(() => call("GET", "/api/sessions"));
  if (!sessions) return;
  const sel = document.getElementById("session-select");
  const prev = sel.value;
  sel.replaceChildren(...sessions.map((s) => h("option", { value: s.id },
    `#${s.id} ${s.streamer} · ${s.title || s.source} (${s.status})`)));
  if (prev && sessions.some((s) => String(s.id) === prev)) sel.value = prev;
  table(document.getElementById("session-table"),
    ["#", "Yayıncı", "Başlık", "Kategori", "Durum", "Başladı", "Aday"],
    sessions.map((s) => h("tr", {}, h("td", {}, s.id), h("td", {}, `${s.streamer} (${s.platform})`),
      h("td", {}, s.title || ""), h("td", {}, s.category || ""), h("td", {}, s.status),
      h("td", {}, (s.started_at || "").replace("T", " ").slice(0, 16)), h("td", {}, s.candidates))));
  await loadSignals();
}

async function loadSignals() {
  const sid = document.getElementById("session-select").value;
  const box = document.getElementById("signal-chart");
  if (!sid) { box.replaceChildren(h("p", { class: "muted" }, "Session yok.")); return; }
  const last = document.getElementById("signal-window").value;
  const q = last === "0" ? "last=0" : `last=${last}`;
  const d = await act(() => call("GET", `/api/sessions/${sid}/signals?${q}`));
  if (!d) return;
  box.replaceChildren(drawSignals(d));
}

function drawSignals(d) {
  const NS = "http://www.w3.org/2000/svg";
  const W = 1000, H = 280, top = 10, mid = 200, bottom = 270;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const mk = (tag, attrs, text) => {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (text) el.textContent = text;
    svg.append(el);
    return el;
  };
  if (!d.t.length) {
    mk("text", { x: 20, y: 40, fill: "#8b93a1" }, "Bu session için sinyal yok (dedektör çalışmadı).");
    return svg;
  }
  const t0 = d.t[0], t1 = d.t[d.t.length - 1] || t0 + 1;
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * W;
  const maxS = Math.max(1, ...d.score);
  const y = (v) => mid - (v / maxS) * (mid - top);
  mk("line", { x1: 0, x2: W, y1: mid, y2: mid, stroke: "#2c313b" });
  for (const c of d.candidates) {
    const src = c.sources.includes("reactive") ? "reactive" : c.sources[0];
    const colour = getComputedStyle(document.documentElement).getPropertyValue(`--${src}`) || "#4f8cff";
    mk("rect", { x: x(c.win_start), y: top, width: Math.max(2, x(c.win_end) - x(c.win_start)),
      height: mid - top, fill: colour.trim(), opacity: 0.15 });
    const mark = mk("line", { x1: x(c.peak_ts), x2: x(c.peak_ts), y1: top, y2: mid,
      stroke: colour.trim(), "stroke-width": 2 });
    const title = document.createElementNS(NS, "title");
    title.textContent = `#${c.id} ${fmtTime(c.peak_ts)} ${c.sources.map((s) => SOURCE_LABEL[s] || s).join("+")} (${c.status})`;
    mark.append(title);
  }
  const pts = d.t.map((t, i) => `${x(t).toFixed(1)},${y(d.score[i]).toFixed(1)}`).join(" ");
  mk("polyline", { points: pts, fill: "none", stroke: "#e6e8ec", "stroke-width": 1.2 });
  // Content scan scores (0-100) as a step line under the chart.
  const yc = (s) => bottom - (s / 100) * (bottom - mid - 14);
  for (const s of d.scans) {
    if (s.score === null || s.score === undefined) continue;
    const ln = mk("line", { x1: x(s.win_start), x2: x(s.win_end), y1: yc(s.score), y2: yc(s.score),
      stroke: getComputedStyle(document.documentElement).getPropertyValue("--content").trim(),
      "stroke-width": 3 });
    const title = document.createElementNS(NS, "title");
    title.textContent = `içerik ${s.score} ${s.kind || ""}`;
    ln.append(title);
  }
  mk("text", { x: 4, y: mid + 14, fill: "#8b93a1", "font-size": 11 }, "içerik taraması");
  mk("text", { x: 4, y: H - 2, fill: "#8b93a1", "font-size": 11 }, fmtTime(t0));
  mk("text", { x: W - 40, y: H - 2, fill: "#8b93a1", "font-size": 11 }, fmtTime(t1));
  return svg;
}

// ---- clips --------------------------------------------------------------------------

async function loadClips() {
  for (const b of document.querySelectorAll("#clip-views button")) {
    b.classList.toggle("active", b.dataset.view === state.view);
  }
  const list = document.getElementById("clip-list");
  const clips = await act(() => call("GET", `/api/clips?view=${state.view}`));
  if (!clips) return;
  if (!clips.length) { list.replaceChildren(h("p", { class: "muted" }, VIEW_EMPTY[state.view])); return; }
  list.replaceChildren(...clips.map(clipCard));
}

function badges(c) {
  const out = c.sources.map((s) => h("span", { class: `badge src-${s}` }, SOURCE_LABEL[s] || s));
  for (const [k, v] of Object.entries(c.flags || {})) {
    if (v && k !== "needs_context") out.push(h("span", { class: "badge flag" }, k));
  }
  return out;
}

function clipCard(c) {
  const isLlmReject = state.view === "rejected_llm";
  const editable = c.status === "rendered" && c.review === "pending";
  const title = h("input", { type: "text", value: c.title || "", maxlength: 100 });
  const hook = h("input", { type: "text", value: c.hook || "" });
  const tags = h("input", { type: "text", value: (c.hashtags || []).join(" ") });
  const start = h("input", { type: "number", step: "0.5", value: c.start ?? "" });
  const end = h("input", { type: "number", step: "0.5", value: c.end ?? "" });
  const len = h("span", { class: "muted" });
  const updLen = () => {
    const a = parseFloat(start.value), b = parseFloat(end.value);
    len.textContent = Number.isFinite(a) && Number.isFinite(b) ? `${(b - a).toFixed(1)} sn` : "";
  };
  start.addEventListener("input", updLen);
  end.addEventListener("input", updLen);
  updLen();
  const nudge = (inp, d) => h("button", { type: "button", onclick: () => {
    inp.value = (parseFloat(inp.value || "0") + d).toFixed(1); updLen();
  } }, d > 0 ? `+${d}` : `${d}`);

  const save = async () => {
    const body = { title: title.value, hook: hook.value,
      hashtags: tags.value.split(/[\s,]+/).filter(Boolean) };
    if (start.value !== "") body.start = parseFloat(start.value);
    if (end.value !== "") body.end = parseFloat(end.value);
    const r = await act(() => call("PUT", `/api/clips/${c.clip_id}`, body));
    if (r) { toast(r.rendering ? "Kaydedildi, yeniden render kuyrukta." : "Kaydedildi."); loadClips(); }
  };

  const chosen = new Set(c.publish_targets.length ? c.publish_targets : PLATFORMS);
  const platformBoxes = PLATFORMS.map((p) => {
    const box = h("input", { type: "checkbox", checked: chosen.has(p),
      onchange: (e) => (e.target.checked ? chosen.add(p) : chosen.delete(p)) });
    return h("label", { class: "check" }, box, p);
  });

  const video = c.video
    ? h("video", { src: media(c.video), poster: media(c.thumb), controls: true, preload: "metadata", playsinline: true })
    : c.raw_video
      ? h("video", { src: media(c.raw_video), controls: true, preload: "metadata", playsinline: true })
      : h("p", { class: "muted" }, "Video henüz yok.");

  const actions = [];
  if (editable) {
    actions.push(
      h("div", { class: "row" }, h("button", { onclick: save }, "Kaydet"),
        h("button", { onclick: async () => {
          if (await act(() => call("POST", `/api/clips/${c.clip_id}/rerender`), "Yeniden render kuyrukta.")) loadClips();
        } }, "Yeniden render")),
      h("div", { class: "row" }, ...platformBoxes),
      h("div", { class: "row" }, h("button", { class: "ok", onclick: async () => {
        await save();
        const r = await act(() => call("POST", `/api/clips/${c.clip_id}/approve`, { platforms: [...chosen] }));
        if (r) { toast(r.decision === "edited" ? "Onaylandı (düzenlenmiş)." : "Onaylandı."); loadClips(); loadStatus(); }
      } }, "Onayla")),
      h("div", { class: "reasons" }, h("span", { class: "muted" }, "Reddet:"),
        ...(state.status?.reject_reasons || Object.keys(REASON_LABEL)).map((r) =>
          h("button", { class: "bad", onclick: async () => {
            if (await act(() => call("POST", `/api/clips/${c.clip_id}/reject`, { reason: r }), "Reddedildi.")) {
              loadClips(); loadStatus();
            }
          } }, REASON_LABEL[r] || r))),
    );
  }
  if (isLlmReject) {
    actions.push(h("div", { class: "row" }, h("button", { class: "ok", onclick: async () => {
      if (await act(() => call("POST", `/api/clips/${c.clip_id}/rescue`), "Kurtarıldı, render kuyrukta.")) loadClips();
    } }, "Bu aslında iyiydi (kurtar)")));
  }

  const content = c.content
    ? h("p", { class: "muted" }, `İçerik: ${c.content.kind || "?"} (${c.content.score ?? "-"}) · ${c.content.reason || ""}`)
    : null;
  const viewers = c.viewer_clips.length
    ? h("p", { class: "muted" }, `İzleyici klibi: ${c.viewer_clips.map((v) => v.title || v.slug).join(", ")}`)
    : null;
  const score = c.llm_score ?? c.prefilter_score;

  return h("article", { class: "card" },
    h("div", { class: "head" },
      h("div", {}, h("strong", {}, c.streamer), " ", h("span", { class: "muted" }, `@ ${fmtTime(c.peak_ts)} · #${c.candidate_id}`)),
      h("span", { class: "score", title: "LLM skoru" }, score ?? "-")),
    h("div", {}, badges(c)),
    c.permission_ok ? null : h("p", { class: "warn" }, "Bu yayıncı için izin işaretli değil: klip yayınlanmaz."),
    video,
    c.reason ? h("p", {}, (c.reject_stage ? `[${c.reject_stage}] ` : "") + c.reason) : null,
    content, viewers,
    c.top_messages.length ? h("ul", { class: "msgs" }, c.top_messages.map((m) => h("li", {}, typeof m === "string" ? m : m.text || JSON.stringify(m)))) : null,
    h("label", {}, "Başlık", title),
    h("label", {}, "Hook (ilk 2.5 sn)", hook),
    h("label", {}, "Hashtag'ler", tags),
    h("div", { class: "trim" }, "Başla", nudge(start, -1), start, nudge(start, 1),
      "Bitir", nudge(end, -1), end, nudge(end, 1), len),
    ...actions);
}

// ---- publications / accounts / costs ------------------------------------------------

async function loadPubs() {
  const rows = await act(() => call("GET", "/api/publications"));
  if (!rows) return;
  table(document.getElementById("pub-table"),
    ["#", "Klip", "Yayıncı", "Platform", "Durum", "Planlanan", "Link", "Hata"],
    rows.length ? rows.map((p) => h("tr", {}, h("td", {}, p.id), h("td", {}, p.title || p.clip_id),
      h("td", {}, p.streamer), h("td", {}, `${p.platform} / ${p.account}`), h("td", {}, p.status),
      h("td", {}, p.scheduled_at || ""),
      h("td", {}, p.url ? h("a", { href: p.url, target: "_blank", rel: "noopener" }, "aç") : ""),
      h("td", {}, p.error || "")))
      : [h("tr", {}, h("td", { colspan: 8, class: "muted" }, "Henüz yayın yok."))]);
}

async function loadAccounts() {
  const rows = await act(() => call("GET", "/api/accounts"));
  if (!rows) return;
  table(document.getElementById("account-table"), ["Platform", "Hesap", "Güncellendi"],
    rows.length ? rows.map((a) => h("tr", {}, h("td", {}, a.platform), h("td", {}, a.account), h("td", {}, a.updated_at)))
      : [h("tr", {}, h("td", { colspan: 3, class: "muted" }, "Bağlı hesap yok."))]);
}

async function loadCosts() {
  const c = await act(() => call("GET", "/api/costs"));
  if (c) {
    document.getElementById("month-usd").textContent = `$${c.month_usd.toFixed(4)}`;
    table(document.getElementById("cost-days"), ["Gün", "USD", "Çağrı"],
      c.days.map((d) => h("tr", {}, h("td", {}, d.day), h("td", {}, d.usd), h("td", {}, d.calls))));
    table(document.getElementById("cost-purposes"), ["Amaç", "Model", "USD", "Çağrı"],
      c.purposes.map((p) => h("tr", {}, h("td", {}, p.purpose), h("td", {}, p.model), h("td", {}, p.usd), h("td", {}, p.calls))));
  }
  const logs = await act(() => call("GET", "/api/logs"));
  if (logs) {
    document.getElementById("log-lines").textContent = logs.slice().reverse()
      .map((l) => `${(l.ts || "").slice(0, 19)} ${l.level || ""} ${l.logger || ""} ${l.msg || ""}`)
      .join("\n");
  }
}

// ---- streamers + facecam picker -----------------------------------------------------

async function loadStreamers() {
  const rows = await act(() => call("GET", "/api/streamers"));
  if (!rows) return;
  state.streamers = rows;
  table(document.getElementById("streamer-table"), ["Ad", "Platform", "Düzen", "Facecam", "İzin", "Oto"],
    rows.map((s) => h("tr", { class: state.editing === s.id ? "sel" : null, onclick: () => editStreamer(s) },
      h("td", {}, s.display_name), h("td", {}, `${s.platform}/${s.channel}`), h("td", {}, s.layout),
      h("td", {}, s.facecam_rect ? "var" : "-"), h("td", {}, s.permission_ok ? "✔" : "✘"),
      h("td", {}, s.auto_publish ? "✔" : "-"))));
}

function editStreamer(s) {
  const f = document.getElementById("streamer-form");
  f.hidden = false;
  state.editing = s ? s.id : null;
  state.rect = s && s.facecam_rect ? { ...s.facecam_rect } : null;
  state.frame = null;
  document.getElementById("streamer-form-title").textContent = s ? `Yayıncı: ${s.display_name}` : "Yeni yayıncı";
  f.display_name.value = s?.display_name || "";
  f.platform.value = s?.platform || "twitch";
  f.channel.value = s?.channel || "";
  f.layout.value = s?.layout || "blur";
  f.enabled.checked = s ? !!s.enabled : true;
  f.permission_ok.checked = !!s?.permission_ok;
  f.permission_note.value = s?.permission_note || "";
  f.auto_publish.checked = !!s?.auto_publish;
  f.keywords.value = (s?.keywords || []).join(", ");
  document.getElementById("fc-frame").disabled = !s;
  document.getElementById("fc-info").textContent = state.rect ? rectText(state.rect) : "";
  drawCanvas();
  loadStreamers();
}

const rectText = (r) => `x=${r.x.toFixed(3)} y=${r.y.toFixed(3)} w=${r.w.toFixed(3)} h=${r.h.toFixed(3)}`;

function drawCanvas() {
  const cv = document.getElementById("fc-canvas");
  const ctx = cv.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, cv.width, cv.height);
  if (state.frame) ctx.drawImage(state.frame, 0, 0, cv.width, cv.height);
  else {
    ctx.fillStyle = "#8b93a1";
    ctx.font = "14px system-ui";
    ctx.fillText("“Kare getir” ile son kayıttan bir kare yükle.", 20, 30);
  }
  if (state.rect) {
    const r = state.rect;
    ctx.strokeStyle = "#e5534b";
    ctx.lineWidth = 3;
    ctx.strokeRect(r.x * cv.width, r.y * cv.height, r.w * cv.width, r.h * cv.height);
  }
}

function setupCanvas() {
  const cv = document.getElementById("fc-canvas");
  let drag = null;
  const pos = (e) => {
    const b = cv.getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)),
      y: Math.min(1, Math.max(0, (e.clientY - b.top) / b.height)) };
  };
  cv.addEventListener("pointerdown", (e) => { drag = pos(e); cv.setPointerCapture(e.pointerId); });
  cv.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const p = pos(e);
    state.rect = { x: Math.min(drag.x, p.x), y: Math.min(drag.y, p.y),
      w: Math.abs(p.x - drag.x), h: Math.abs(p.y - drag.y) };
    drawCanvas();
  });
  cv.addEventListener("pointerup", () => {
    drag = null;
    if (state.rect && (state.rect.w < 0.02 || state.rect.h < 0.02)) state.rect = null;
    document.getElementById("fc-info").textContent = state.rect ? rectText(state.rect) : "";
    drawCanvas();
  });
  const loadFrame = (rel) => new Promise((resolve) => {
    const img = new Image();
    img.onload = () => { state.frame = img; drawCanvas(); resolve(); };
    img.src = `${media(rel)}?t=${Date.now()}`;
  });
  document.getElementById("fc-frame").addEventListener("click", async () => {
    if (!state.editing) return;
    const r = await act(() => call("POST", `/api/streamers/${state.editing}/frame`));
    if (r) await loadFrame(r.frame);
  });
  document.getElementById("fc-suggest").addEventListener("click", async () => {
    if (!state.editing) return;
    toast("Claude kareleri inceliyor…");
    const r = await act(() => call("POST", `/api/streamers/${state.editing}/facecam/suggest`));
    if (!r) return;
    if (!r.found) { toast(`Facecam bulunamadı: ${(r.reasons || []).join("; ")}`); return; }
    state.rect = r.rect;
    document.getElementById("fc-info").textContent = `${rectText(r.rect)} (güven ${r.confidence}, $${r.usd.toFixed(4)})`;
    if (!state.frame) {
      const fr = await act(() => call("POST", `/api/streamers/${state.editing}/frame`));
      if (fr) await loadFrame(fr.frame);
    }
    drawCanvas();
  });
  document.getElementById("fc-clear").addEventListener("click", () => {
    state.rect = null;
    document.getElementById("fc-info").textContent = "facecam yok";
    drawCanvas();
  });
}

async function saveStreamer(e) {
  e.preventDefault();
  const f = e.target;
  const body = {
    display_name: f.display_name.value, platform: f.platform.value, channel: f.channel.value,
    layout: f.layout.value, enabled: f.enabled.checked, permission_ok: f.permission_ok.checked,
    permission_note: f.permission_note.value || null, auto_publish: f.auto_publish.checked,
    keywords: f.keywords.value.split(",").map((s) => s.trim()).filter(Boolean),
    facecam_rect: state.rect,
  };
  const r = state.editing
    ? await act(() => call("PUT", `/api/streamers/${state.editing}`, body), "Kaydedildi.")
    : await act(() => call("POST", "/api/streamers", body), "Yayıncı eklendi.");
  if (r) { state.editing = r.id; loadStreamers(); }
}

// ---- boot ---------------------------------------------------------------------------

document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.querySelectorAll("#clip-views button").forEach((b) => b.addEventListener("click", () => {
  state.view = b.dataset.view; loadClips();
}));
document.getElementById("session-select").addEventListener("change", loadSignals);
document.getElementById("signal-window").addEventListener("change", loadSignals);
document.getElementById("streamer-new").addEventListener("click", () => editStreamer(null));
document.getElementById("streamer-cancel").addEventListener("click", () => {
  document.getElementById("streamer-form").hidden = true; state.editing = null; loadStreamers();
});
document.getElementById("streamer-form").addEventListener("submit", saveStreamer);
setupCanvas();
loadStatus().then(() => showTab(state.tab));
setInterval(loadStatus, 10000);
setInterval(() => { if (state.tab === "live") { loadLiveNow(); loadSignals(); } }, 15000);
