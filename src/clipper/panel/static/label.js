"use strict";
// Labeling screen. Talks only to /api/label: it never receives the system's candidates.
const $ = (id) => document.getElementById(id);
const v = $("v");
const st = { duration: 0, labels: [], start: null, end: null, sel: -1, types: [] };

function fmt(t) {
  t = Math.max(0, Math.floor(t || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function total() { return st.duration || v.duration || 1; }

function drawTimeline() {
  const tl = $("timeline");
  tl.replaceChildren();
  const add = (a, b, cls) => {
    const m = el("div", undefined, cls);
    m.style.left = (100 * a / total()) + "%";
    m.style.width = (100 * Math.max(0, b - a) / total()) + "%";
    tl.append(m);
  };
  st.labels.forEach((x) => add(x.video_start, x.video_end, "mark"));
  if (st.start !== null) add(st.start, st.end ?? v.currentTime, "mark pending");
  const head = el("div", undefined, "head");
  head.style.left = (100 * v.currentTime / total()) + "%";
  tl.append(head);
}

function drawList() {
  const list = $("list");
  list.replaceChildren();
  $("count").textContent = "(" + st.labels.length + ")";
  st.labels.forEach((x, i) => {
    const row = el("div", undefined, "item" + (i === st.sel ? " sel" : ""));
    row.append(el("div", fmt(x.video_start) + " → " + fmt(x.video_end) + " · " + x.type + " · kalite " + x.quality));
    if (x.note) row.append(el("div", x.note, "muted"));
    const go = el("button", "Git");
    go.onclick = () => { v.currentTime = x.video_start; st.sel = i; drawList(); };
    const del = el("button", "Sil", "bad");
    del.onclick = () => removeAt(i);
    row.append(go, " ", del);
    list.append(row);
  });
}

async function save() {
  const s = $("save-state");
  s.className = "muted"; s.textContent = "kaydediliyor…";
  try {
    const r = await fetch("/api/label", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ labels: st.labels }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    s.className = "saved"; s.textContent = "kaydedildi (" + data.count + ")";
  } catch (e) {
    s.className = "err"; s.textContent = "kaydedilemedi: " + e.message;
  }
}

function removeAt(i) {
  st.labels.splice(i, 1);
  st.sel = -1;
  drawList(); drawTimeline(); save();
}

function setStart() { st.start = v.currentTime; st.end = null; drawTimeline(); }
function setEnd() { if (st.start !== null) { st.end = Math.max(v.currentTime, st.start + 1); drawTimeline(); } }

function addLabel() {
  if (st.start === null) { $("save-state").textContent = "önce başlangıç (I) işaretle"; return; }
  const end = st.end ?? v.currentTime;
  if (end <= st.start) { $("save-state").textContent = "bitiş başlangıçtan sonra olmalı"; return; }
  st.labels.push({ video_start: +st.start.toFixed(2), video_end: +end.toFixed(2),
    type: $("type").value, quality: +$("quality").value, note: $("note").value.trim() });
  st.labels.sort((a, b) => a.video_start - b.video_start);
  st.start = st.end = null; $("note").value = "";
  drawList(); drawTimeline(); save();
}

document.addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) {
    if (e.key === "Enter") { e.preventDefault(); addLabel(); }
    return;
  }
  const k = e.key.toLowerCase();
  if (k === " ") { e.preventDefault(); v.paused ? v.play() : v.pause(); }
  else if (k === "arrowleft") v.currentTime = Math.max(0, v.currentTime - 5);
  else if (k === "arrowright") v.currentTime = v.currentTime + 5;
  else if (k === "i") setStart();
  else if (k === "o") setEnd();
  else if (k === "1" || k === "2" || k === "3") $("quality").value = k;
  else if (k === "enter") addLabel();
  else if ((k === "delete" || k === "backspace") && st.sel >= 0) removeAt(st.sel);
  else return;
  e.preventDefault();
});

$("timeline").addEventListener("click", (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  v.currentTime = total() * (e.clientX - r.left) / r.width;
});
$("rate").onchange = () => { v.playbackRate = +$("rate").value; };
v.addEventListener("loadedmetadata", () => { v.playbackRate = +$("rate").value; drawTimeline(); });
v.addEventListener("timeupdate", () => { $("clock").textContent = fmt(v.currentTime) + " / " + fmt(total()); drawTimeline(); });
$("b-start").onclick = setStart;
$("b-end").onclick = setEnd;
$("b-add").onclick = addLabel;

(async () => {
  const data = await fetch("/api/label").then((r) => r.json());
  st.duration = data.duration; st.labels = data.labels; st.types = data.types;
  for (const t of data.types) { const o = el("option", t); o.value = t; $("type").append(o); }
  const s = data.session;
  $("meta").textContent = "#" + s.id + " · " + (s.streamer || "") + " · " + (s.title || "") +
    (data.labeler ? " · etiketleyen: " + data.labeler : "") + " · dosya: " + data.file;
  drawList(); drawTimeline();
})();
