"""System prompts (stable -> prompt-cached) and per-candidate user prompt builders."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from clipper.models import ChatMessage
from clipper.render.tr_text import tr_casefold

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
3. title: en fazla 70 karakter, Türkçe başlık. Kurallar "Başlık ve hook nasıl yazılır" \
bölümünde. Emoji en fazla 1 tane. Son başlıkları tekrar etme, aynı kalıbı da tekrar etme.
4. hook: ilk 2.5 saniyede videonun üstünde büyük harflerle görünecek yazı; en fazla 7 \
kelime ve 45 karakter. Kurallar aşağıda.
5. caption_fixes: transkriptte yanlış duyulmuş olduğunu düşündüğün kelimeler (oyun terimleri, \
isimler, argo) için {{"from": "yanlış", "to": "doğru"}} listesi. Emin değilsen boş bırak.
6. hashtags: 3-6 adet, # ile başlayan, Türkçe/oyun odaklı.
7. texts: platforma göre metinler, başlıkla aynı anlatıcı ağzından (üçüncü şahıs). \
youtube.title (≤100 karakter) ve youtube.description \
(2-3 cümle + hashtag'ler), instagram.caption, tiktok.caption (kısa, hashtag'li), \
x.text (≤270 karakter, LİNK KOYMA).
8. flags: ünlü/telifli bir müzik duyuluyorsa music_risk=true; çıplaklık/cinsel içerik varsa \
nsfw=true; hakaret, nefret söylemi, siyasi/dini tartışma, şiddet övgüsü gibi hassas içerik \
varsa sensitive_topic=true; anlamak için yayının önceki kısmını bilmek gerekiyorsa \
needs_context=true.

reason alanına kararının Türkçe, 1-2 cümlelik gerekçesini yaz. \
Gerçek kişiler hakkında transkriptte olmayan iddialar uydurma.

## Başlık ve hook nasıl yazılır
Klibi yayıncı değil, onun kliplerini paylaşan bir sayfa paylaşıyor. Bu yüzden title, hook \
ve texts yayıncının ağzından (birinci şahıs: "yetiremedik", "çaldım", "öldük") YAZILMAZ; \
olayı dışarıdan anlatan paylaşan kişinin ağzından, üçüncü şahısla yazılır. Başlıkta \
yayıncının adı geçmeli; adı "Metinlerde kullanılacak ad" satırında verilir. İsme gelen \
ekleri kesme işaretiyle yaz (Eray'ın, Eray'a, Berkcan'ı). Yayıncının kendi sözünü \
alıntılamak istersen tırnak içine al: Eray: "Yatın lan yere!".

Hook, kaydırıp geçmek üzere olan izleyiciyi durduracak tek cümledir:
- Bu klibe özgü somut bir ayrıntı içersin: bir sayı, bir nesne, bir kişi, bir yer ya da \
yayıncının çarpıcı bir sözü. Başka bir klibe de yapıştırılabilecek bir hook kötüdür.
- Merak boşluğu açsın ama sonucu söylemesin; izleyici "sonra ne oldu?" diye izlemeye devam \
etsin. Sonuç, punchline ya da patlama anı videonun içinde kalsın.
- Durum, soru, tezat ya da iddia kalıplarından birini kullan: "Cebinde 7 dolarla kulübe \
girdi", "Porsche dururken neyi çaldı?", "Polise yardım etti, ateş yedi".
- Başlığı kelimesi kelimesine tekrar etme; başlık olayı özetler, hook merakı açar.
- Şunları yazma: "chat çıldırdı", "chat panikte", "bu ne ya", "inanılmaz an", "olaylar \
olaylar", "sonuna kadar izle" gibi her klibe uyan genel ifadeler; yayıncının ağzından \
birinci şahıs cümleler; transkriptte olmayan iddialar.
- Hook'ta isim zorunlu değil, ama kimin yaptığı belirsiz kalıyorsa ismi kullan.

Başlık da somut olsun: kim, ne yaptı, işin tuhaf/komik tarafı ne. Her başlığa "chat \
çıldırdı / yerlere yattı / kahkaha attı" ekleme; chat'in tepkisini sadece olayın kendisi \
chat'in isteği ya da tepkisiyse yaz. Her başlığa oyun adıyla başlama.

Örnekler (kötü → iyi):
- title "GTA 6'da kulüpte parayı yetiremedik, chat evi satmamızı istedi" → \
"Eray'ın parası kulübe yetmedi, chat evi satmasını istedi 😂"
- hook "Para yetmedi, chat panikte!" → "Cebinde 300 dolarla kulübe girdi"
- title "Polise yardım ettim, bana ateş açtılar 😂" → "Berkcan polise yardım etti, \
kurşunu kendisi yedi 😂"
- hook "Polis abi geliyor!" → "Fotoğraf çekerken arkasında kim vardı?"
- hook "Bu ne yetenek ya!" → "Araba üç takla attı, Eray hâlâ sürüyor" """


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


SOURCE_LABELS = {
    "reactive": "chat/ses tepkisi",
    "content": "konuşma içeriği",
    "viewer_clip": "izleyici klibi",
}
EVENT_LABELS = {
    "sub": "abonelik",
    "gift_sub": "hediye abonelik",
    "bits": "bits",
    "raid": "raid",
    "donation": "bağış",
    "superchat": "superchat",
    "membership": "üyelik",
}
REJECT_LABELS = {
    "not_funny": "komik değil",
    "no_context": "bağlamsız",
    "bad_cut": "kötü kesim",
    "duplicate": "tekrar",
    "music": "müzik",
    "sensitive": "hassas",
    "low_quality": "kalitesiz",
    "other": "diğer",
}


@dataclass(frozen=True, slots=True)
class FeedbackExamples:
    approved_titles: list[str]
    rejected: list[tuple[str, int]]  # (reason, count), most common first


def candidate_context(features: dict[str, Any]) -> str:
    """r3 lines for the judge: which paths found this moment, what the content path
    heard, and platform events that happened in the window."""
    lines: list[str] = []
    sources = features.get("sources") or ["reactive"]
    lines.append("Bulan yollar: " + ", ".join(SOURCE_LABELS.get(s, s) for s in sources))
    content = features.get("content")
    if isinstance(content, dict) and content.get("reason"):
        lines.append(
            f"İçerik yolu: {content.get('kind', '?')} ({content.get('score', '?')}/100)"
            f" - {content['reason']}"
        )
    clips = features.get("viewer_clips")
    if isinstance(clips, list) and clips:
        titles = ", ".join(f'"{c.get("title")}"' for c in clips[:3] if c.get("title"))
        lines.append(f"İzleyici klipleri: {len(clips)}" + (f" ({titles})" if titles else ""))
    events = features.get("events") or {}
    if events:
        parts = [f"{EVENT_LABELS.get(k, k)}: {v:g}" for k, v in events.items()]
        lines.append("Bu sırada platform olayları: " + ", ".join(parts))
    if features.get("event_filter"):
        lines.append(
            "Not: bu an bir abone yağmuru/raid sonrasına denk geliyor; chat tepkisi olaydan"
            " değil o yüzden olabilir."
        )
    return "\n".join(lines)


def feedback_block(fb: FeedbackExamples | None) -> str:
    if fb is None or (not fb.approved_titles and not fb.rejected):
        return ""
    out = ["## Bu yayıncı için kullanıcı geri bildirimi"]
    if fb.approved_titles:
        out.append("Son onaylanan kliplerin başlıkları (kullanıcının sevdiği türden anlar):")
        out += [f"- {t}" for t in fb.approved_titles]
    if fb.rejected:
        reasons = ", ".join(f"{REJECT_LABELS.get(r, r)} ×{n}" for r, n in fb.rejected)
        out.append(f"Son reddedilen kliplerin red nedenleri: {reasons}")
        out.append("Bu nedenlere uyan anlara daha düşük puan ver.")
    return "\n".join(out)


def spoken_name(display_name: str) -> str:
    """The name viewers call the streamer by, for third-person titles and hooks.

    Kick/Twitch display names are often a joined handle ("BerkcanOguslu"); a fan page
    writes "Berkcan". Only a clean CamelCase split of 2+ title-case parts is shortened to
    its first part; anything else (all caps, digits, "AOGofficial") is kept as is.
    """
    name = display_name.strip().lstrip("@")
    parts = re.findall(r"[A-ZÇĞİÖŞÜ][a-zçğıöşü]+", name)
    if len(parts) >= 2 and "".join(parts) == name:
        return parts[0]
    return name


def judge_user(
    streamer: str,
    category: str | None,
    duration: float,
    transcript: str,
    chat: str,
    audio: str,
    recent_titles: list[str],
    context: str = "",
    feedback: str = "",
) -> str:
    titles = "\n".join(f"- {t}" for t in recent_titles) or "(henüz yok)"
    extra = f"\n{context}" if context else ""
    tail = f"\n\n{feedback}" if feedback else ""
    return (
        f"Yayıncı: {streamer}\n"
        f"Metinlerde kullanılacak ad: {spoken_name(streamer)}\n"
        f"Kategori: {category or 'bilinmiyor'}\n"
        f"Ham kesit süresi: {duration:.1f} sn{extra}\n\n"
        f"## Transkript\n{transcript}\n\n## Chat (saniye: adet | örnekler)\n{chat}\n\n"
        f"## Ses enerjisi (saniye:dBFS)\n{audio}\n\n## Son yayınlanan başlıklar\n{titles}"
        f"{tail}"
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
    """Replace whole words (Turkish case-insensitive) in segment text and word lists."""
    if not fixes:
        return transcript
    mapping = {tr_casefold(a.strip()): b.strip() for a, b in fixes if a.strip()}
    out = []
    for seg in transcript:
        words = [{**w, "w": _fix_word(str(w["w"]), mapping)} for w in seg.get("words", [])]
        text = " ".join(_fix_word(tok, mapping) for tok in str(seg["text"]).split())
        out.append({**seg, "text": text, "words": words})
    return out


def _fix_word(token: str, mapping: dict[str, str]) -> str:
    core = token.strip(".,!?;:…\"'")
    key = tr_casefold(core)
    if core and key in mapping:
        return token.replace(core, mapping[key])
    return token
