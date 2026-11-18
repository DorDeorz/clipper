"""System prompts (stable -> prompt-cached) and per-candidate user prompt builders."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from clipper.models import ChatMessage

PREFILTER_SYSTEM = """\
Sen Türkçe canlı yayınlardan kısa dikey klip (YouTube Shorts, Instagram Reels, TikTok, X) \
çıkaran bir ekibin ön eleme editörüsün. Sana otomatik bir dedektörün chat ve ses \
hareketlenmesinden bulduğu bir "aday an" veriliyor: yayıncı, kategori, o anın transkripti, \
chat'te en sık yazılanlar ve dedektör özellikleri.

Görevin hızlı karar vermek: bu an, bağlamını bilmeyen bir izleyici için kısa bir klip olarak \
izlenmeye değer mi?

Değerli olanlar: gerçekten komik bir an, şaşırtıcı bir olay, oyunda çok iyi ya da çok kötü \
bir hamle, güçlü bir duygusal tepki, akılda kalan bir söz, chat'in topluca tepki verdiği net \
bir olay.
Değersiz olanlar: sadece chat'in spam yaptığı ama ekranda/seste bir şey olmayan anlar, \
reklam/sponsor okuması, bağış/abone teşekkürü rutini, yayının başı/sonu, uzun sessizlik, \
anlamak için çok fazla önceki bağlam gereken anlar, sadece müzik çalan anlar.

Puanlama (rough_score, 0-100): 0-30 kesinlikle değersiz, 30-60 zayıf, 60-80 iyi, 80+ çok iyi. \
Transkript boş ya da çok kısaysa ve chat tepkisi belirsizse düşük puan ver. \
reason alanına Türkçe, tek cümlelik gerekçe yaz. keep=true sadece puan 50 ve üzerindeyse."""

JUDGE_SYSTEM = """\
Sen Türkçe canlı yayın kliplerinden dikey kısa video hazırlayan deneyimli bir editörsün. \
Sana bir ham kesit veriliyor (genelde 60-90 sn). Ham kesitin başı 0. saniyedir; tüm zamanlar \
bu başlangıca göre saniye cinsindendir. Elindekiler:
- Kelime zamanlarıyla transkript ([saniye] metin)
- Chat'in zaman çizelgesi (saniye: mesaj sayısı | örnek mesajlar)
- Ses enerjisi profili (saniye başına dBFS)
- Yayıncının son yayınlanmış klip başlıkları (tekrar etme)

Görevlerin:
1. Klibi değerlendir: tek başına, bağlam bilmeden izlenince anlaşılır ve ilgi çekici mi? \
score 0-100 ver (85+ otomatik yayınlanabilecek kadar iyi, 65 altı yayınlanmaz). \
keep=false ise diğer alanları yine doldur ama kısa tut.
2. Kesin başlangıç (start) ve bitişi (end) seç. Kurallar: \
olayın kurulumunu izleyiciye verecek kadar erken başla ama gereksiz bekleme koyma; \
başlangıç cümle ortasında OLMAMALI, bir cümle başına hizala; \
olayın sonucunu ve tepkiyi göster, tepki bitince kes; \
süre {min_len:g}-{max_len:g} saniye arasında olmalı. \
Chat tepkisi olaydan birkaç saniye sonra gelir; olayın kendisini transkript ve sesten bul.
3. title: en fazla 70 karakter, Türkçe, merak uyandıran (clickbait olabilir) ama YALAN \
OLMAYAN bir başlık. Emoji en fazla 1 tane. Son başlıkları tekrar etme.
4. hook: ilk 2.5 saniyede ekranda görünecek, en fazla 6 kelimelik kısa bir cümle.
5. caption_fixes: transkriptte yanlış duyulmuş olduğunu düşündüğün kelimeler (oyun terimleri, \
isimler, argo) için {{"from": "yanlış", "to": "doğru"}} listesi. Emin değilsen boş bırak.
6. hashtags: 3-6 adet, # ile başlayan, Türkçe/oyun odaklı.
7. texts: platforma göre metinler. youtube.title (≤100 karakter) ve youtube.description \
(2-3 cümle + hashtag'ler), instagram.caption, tiktok.caption (kısa, hashtag'li), \
x.text (≤270 karakter, LİNK KOYMA).
8. flags: ünlü/telifli bir müzik duyuluyorsa music_risk=true; çıplaklık/cinsel içerik varsa \
nsfw=true; hakaret, nefret söylemi, siyasi/dini tartışma, şiddet övgüsü gibi hassas içerik \
varsa sensitive_topic=true; anlamak için yayının önceki kısmını bilmek gerekiyorsa \
needs_context=true.

reason alanına kararının Türkçe, 1-2 cümlelik gerekçesini yaz. \
Gerçek kişiler hakkında transkriptte olmayan iddialar uydurma."""


def judge_system(min_len: float, max_len: float) -> str:
    return JUDGE_SYSTEM.format(min_len=min_len, max_len=max_len)


@dataclass(frozen=True, slots=True)
class Word:
    w: str
    s: float
    e: float


def transcript_lines(transcript: list[dict[str, Any]]) -> str:
    lines = [f"[{seg['start']:.1f}] {str(seg['text']).strip()}" for seg in transcript]
    return "\n".join(lines) if lines else "(konuşma yok)"


def transcript_text(transcript: list[dict[str, Any]]) -> str:
    return " ".join(str(seg["text"]).strip() for seg in transcript).strip()


def chat_timeline(
    messages: Iterable[ChatMessage],
    t0: float,
    t1: float,
    samples_per_second: int = 3,
    max_chars: int = 6000,
) -> str:
    per_sec: dict[int, list[str]] = defaultdict(list)
    for m in messages:
        if t0 <= m.ts <= t1:
            per_sec[int(m.ts - t0)].append(" ".join(m.text.split())[:60])
    lines: list[str] = []
    total = 0
    for sec in sorted(per_sec):
        msgs = per_sec[sec]
        sample = " / ".join(dict.fromkeys(msgs))
        sample = " / ".join(sample.split(" / ")[:samples_per_second])
        line = f"{sec}: {len(msgs)} | {sample}"
        total += len(line) + 1
        if total > max_chars:
            lines.append("... (kısaltıldı)")
            break
        lines.append(line)
    return "\n".join(lines) if lines else "(chat yok)"


def audio_profile(db_by_sec: dict[int, float], t0: float, t1: float, step: int = 2) -> str:
    parts = []
    for off in range(0, int(t1 - t0) + 1, step):
        v = db_by_sec.get(int(t0) + off)
        if v is not None:
            parts.append(f"{off}:{round(v)}")
    return " ".join(parts) if parts else "(ses verisi yok)"


def prefilter_user(
    streamer: str,
    category: str | None,
    transcript: str,
    top_messages: list[dict[str, Any]],
    features: dict[str, Any],
) -> str:
    top = "\n".join(f"- {m['count']}× {m['text']}" for m in top_messages[:20]) or "(yok)"
    z = features.get("z", {})
    feats = ", ".join(f"{k}={v:+.1f}" for k, v in z.items())
    return (
        f"Yayıncı: {streamer}\nKategori: {category or 'bilinmiyor'}\n"
        f"Dedektör z-skorları: {feats} (birleşik skor {features.get('score', '?')})\n\n"
        f"Chat'te en sık yazılanlar:\n{top}\n\nTranskript:\n{transcript or '(konuşma yok)'}"
    )


def judge_user(
    streamer: str,
    category: str | None,
    duration: float,
    transcript: str,
    chat: str,
    audio: str,
    recent_titles: list[str],
) -> str:
    titles = "\n".join(f"- {t}" for t in recent_titles) or "(henüz yok)"
    return (
        f"Yayıncı: {streamer}\nKategori: {category or 'bilinmiyor'}\n"
        f"Ham kesit süresi: {duration:.1f} sn\n\n"
        f"## Transkript\n{transcript}\n\n## Chat (saniye: adet | örnekler)\n{chat}\n\n"
        f"## Ses enerjisi (saniye:dBFS)\n{audio}\n\n## Son yayınlanan başlıklar\n{titles}"
    )


def snap_to_words(
    start: float,
    end: float,
    words: list[Word],
    duration: float,
    min_len: float,
    max_len: float,
    pre: float = 0.15,
    post: float = 0.3,
) -> tuple[float, float]:
    """Snap LLM start/end to word boundaries (start: nearest word start - pre;
    end: nearest word end + post), then enforce [min_len, max_len] and [0, duration]."""
    if words:
        s_word = min(words, key=lambda w: abs(w.s - start))
        e_word = min(words, key=lambda w: abs(w.e - end))
        start = s_word.s - pre
        end = e_word.e + post
    start = max(0.0, start)
    end = min(duration, max(end, start))
    if end - start > max_len:
        end = start + max_len
    if end - start < min_len:
        end = min(duration, start + min_len)
        if end - start < min_len:
            start = max(0.0, end - min_len)
    return round(start, 3), round(end, 3)


def words_from_transcript(transcript: list[dict[str, Any]]) -> list[Word]:
    return [
        Word(str(w["w"]), float(w["s"]), float(w["e"]))
        for seg in transcript
        for w in seg.get("words", [])
    ]


def apply_caption_fixes(
    transcript: list[dict[str, Any]], fixes: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Replace whole words (case-sensitive) in segment text and word lists."""
    if not fixes:
        return transcript
    mapping = dict(fixes)
    out = []
    for seg in transcript:
        words = [{**w, "w": _fix_word(str(w["w"]), mapping)} for w in seg.get("words", [])]
        text = " ".join(_fix_word(tok, mapping) for tok in str(seg["text"]).split())
        out.append({**seg, "text": text, "words": words})
    return out


def _fix_word(token: str, mapping: dict[str, str]) -> str:
    core = token.strip(".,!?;:…\"'")
    if core in mapping:
        return token.replace(core, mapping[core])
    return token
