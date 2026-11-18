# ROADMAP — Yayın Klip Otomasyonu ("clipper")

> Bu dosya Claude Code'un sistemi **baştan sona** geliştirmesi için yazıldı.
> Aşamaları sırayla uygula; her aşamanın "Bitti sayılması için" maddeleri sağlanmadan bir sonrakine geçme.
> İlerledikçe kutucukları (`[ ]` → `[x]`) işaretle.

> **Revizyon r2 (2026-11-18): Anonim izleme.** İzleme tarafı (chat, canlı mı, izleyici sayısı, klip sinyali, kayıt, VOD chat) varsayılan olarak **hiçbir hesap/API key olmadan** çalışacak. Twitch: anonim IRC + anonim GQL. YouTube: innertube (web sitesinin iç API'si). Kick: Pusher. Resmi API'ler (Twitch Helix, YouTube Data API) sadece **opsiyonel yedek**. Değişen bölümler: 2, 4.1, 6, Faz 1, Faz 6. Faz 0'ı etkilemez; sadece `config.py`'de `monitoring.mode: anonymous | official` alanı olsun (varsayılan `anonymous`).

> **Revizyon r3 (2026-11-18): Çok sinyalli tespit.** Tek başına chat hızı yetersiz: sakin ama iyi anları (hikâye, laf, atışma) kaçırıyor. Yeni tasarım üç kaynaktan aday üretiyor:
> 1. **Reaktif yol**: chat + ses + izleyici klipleri (mevcut dedektör, olay filtresiyle).
> 2. **İçerik yolu (YENİ)**: canlı transkript, Haiku 5.5 ile dakikada bir taranıyor.
> 3. **İzleyici klibi yolu**: Twitch'te birisi klip açtıysa doğrudan aday.
>
> Son karar Sonnet 5.5'te. Onay/red geri bildirimi ilk günden kaydediliyor. Görüntü kareleri, bağlam özeti ve öğrenilmiş ağırlıklar **v2 (Faz 8, opsiyonel)**.
>
> Değişen bölümler: 3.2, 3.4 (yeni migration), 3.5, 4.3, yeni 4.3b, 4.5, 7, Faz 2, 3, 5, 6, 8. Faz 0 bitmişse şema değişiklikleri **yeni bir migration** olarak eklenecek, eski migration düzenlenmeyecek.

---

## 0. Sistem ne yapacak (tek paragraf)

Seçilen yayıncıların (Twitch / Kick / YouTube) canlı yayınlarını izler. Yayını kendi kodumuzla diske kaydeder (son ~15 dakikalık halka tampon). Aynı anda chat'i dinler ve **chat hızı / tekil kullanıcı / emote-anahtar kelime / ses yüksekliği** sinyallerinden "önemli an" adaylarını çıkarır. Adayın etrafındaki videoyu keser, **faster-whisper** ile Türkçe transkript çıkarır. **Claude** (Haiku 5.5 ön eleme → Sonnet 5.5 / Opus 5.5 nihai karar) klibi değerlendirir, başlangıç-bitişi kırpar, başlık / hook / açıklama / hashtag yazar. **ffmpeg** ile 1080x1920 dikey video render eder: facecam + oyun düzeni, kelime kelime animasyonlu altyazı. Klip yerel web panelde onaylanır (ya da skor eşiğiyle otomatik onaylanır). Sonra **kendi yazdığımız API istemcileriyle** YouTube Shorts, Instagram Reels, X ve TikTok'a yüklenir.

**Tasarım ilkesi:** Mümkün olan her şey bizim kodumuz. Dış bağımlılıklar sadece aşağıdaki tabloda, gerekçesiyle var. Celery, Redis, Postgres, Upload-Post, Postiz, OpusClip vb. **yok**.

---

## 1. Kaçınılmaz dış bağımlılıklar (tam liste)

| Bağımlılık | Neden kaçınılmaz | Not |
|---|---|---|
| Python 3.12 | Ana dil | Ortam yönetimi `uv` ile |
| **ffmpeg** (libass + NVENC destekli build) | Video kesme / render / ses analizi | Windows: gyan.dev "full" build. Linux: distro paketi veya BtbN static build |
| NVIDIA sürücü + CUDA 12 + cuDNN 9 | faster-whisper GPU hızı | GPU yoksa CPU modu (yavaş, `small`/`medium` model) |
| `faster-whisper` + model ağırlıkları | Türkçe transkript, kelime zaman damgası | `large-v3` (GPU) / `medium` (CPU) |
| `anthropic` SDK | Claude API | İstersek ham httpx ile de yazılabilir; SDK retry/streaming için tutuluyor |
| `httpx` | Tüm HTTP istemcileri (platform API'leri, HLS indirme) | `httpx.MockTransport` testlerde de kullanılacak, ayrıca mock kütüphanesi gerekmez |
| `websockets` | Twitch IRC-WS, Kick Pusher WS | |
| `curl_cffi` | Kick'in Cloudflare TLS-fingerprint engeli | Sadece Kick HTTP çağrılarında |
| `numpy` | Sinyal işleme (EWMA, z-skor, RMS) | |
| `pydantic` | Config + LLM JSON çıktısı doğrulama | |
| `pyyaml` | Config dosyası | |
| `fastapi` + `uvicorn` | Yerel kontrol paneli | Frontend: düz HTML + vanilla JS, build aracı yok |
| `cryptography` | OAuth token'larını DB'de şifreli saklama (Fernet) | |
| `pytest` | Testler | |
| `yt-dlp` (**opsiyonel**) | Sadece YouTube canlı/VOD HLS URL çözümlemesi | Twitch ve Kick için kendi kaydedicimiz var |
| Font: Montserrat ExtraBold (OFL lisanslı) | Altyazı | `assets/fonts/` içine konur |

**Kullanılmayanlar (bilinçli):** Redis/Celery (yerine SQLite tabanlı kendi iş kuyruğumuz), streamlink (yerine kendi HLS kaydedicimiz), MediaPipe/OpenCV (facecam konumu config'ten veya Claude vision ile bir kez tespit), MoviePy/Remotion (saf ffmpeg), üçüncü parti paylaşım servisleri.

---

## 2. İnsanların (bizim) yapması gerekenler — Claude Code yapamaz

Claude Code ilgili aşamaya gelince bunları kullanıcıdan istesin, beklerken diğer işlere devam etsin.

- [ ] **Anthropic API key** → `.env` içinde `ANTHROPIC_API_KEY`
- [ ] *(Opsiyonel, yedek)* **Twitch Developer app**: sadece anonim GQL bozulursa Helix yedeği için. İzleme için gerekmez.
- [ ] **Google Cloud projesi** → YouTube Data API v3 etkin. OAuth consent screen ve OAuth Client (tip: *Desktop app*). **Sadece YouTube'a yükleme yapmak için.** Test modunda refresh token'lar ~7 günde düşebilir; uygulamayı "In production"a al veya haftalık yeniden yetkilendir.
- [ ] *(Opsiyonel, yedek)* **YouTube Data API key**: sadece innertube chat bozulursa resmi API yedeği için. İzleme için gerekmez.
- [ ] **Meta Developer app** + Instagram **Professional (Business/Creator)** hesap. İzin: `instagram_business_content_publish` (Instagram Login) veya `instagram_content_publish` (Facebook Login). Hesap app'te admin/tester rolündeyse app review olmadan kendi hesabımıza paylaşabiliriz.
- [ ] **TikTok Developer app** + "Content Posting API" ürünü + Login Kit. Redirect URI HTTPS olmak zorunda (gerekirse GitHub Pages'te tek sayfalık redirect). Audit yokken her şey `SELF_ONLY` kalır. Bu yüzden varsayılan mod **inbox (taslak)**.
- [ ] **X Developer hesabı** (pay-per-use, kredi yüklü). OAuth 2.0 PKCE, scope: `tweet.write tweet.read users.read media.write offline.access`. Redirect: `http://127.0.0.1:8765/callback/x`
- [ ] **Yayıncı izinleri**: her yayıncı için yazılı izin. Panelde `permission_note` ve izin belgesi yolu doldurulacak; izni olmayan yayıncıda yayınlama butonu kilitli olacak.
- [ ] Donanım: önerilen NVIDIA GPU (≥8 GB VRAM), ≥200 GB SSD, sabit upload hızı.

---

## 3. Mimari

### 3.1 Süreç modeli
Tek komut: `clipper run`. Tek Python süreci, `asyncio` supervisor:

```
┌──────────────── clipper run ───────────────────────────────────────┐
│ LiveWatcher (her 60 sn: yayıncı canlı mı?)                          │
│   └─ yayın başladıysa → Session aç → Recorder + ChatClient + Detector│
│ Recorder      : HLS segmentlerini diske yazar (halka tampon, PDT ile)│
│ ChatClient    : Twitch / Kick / YouTube → ChatMessage akışı          │
│ Detector      : saniyelik sinyaller → Candidate                      │
│ JobQueue (SQLite) ← cut / transcribe / judge / render / publish     │
│ Workers       : cpu_pool (ffmpeg, N=2), gpu_pool (whisper, N=1),     │
│                 io_pool (LLM, upload, N=4)                           │
│ Panel (FastAPI, 127.0.0.1:8765)                                      │
└──────────────────────────────────────────────────────────────────────┘
```

- Ağır işler (ffmpeg, whisper) `asyncio.create_subprocess_exec` veya `ProcessPoolExecutor` ile çalışır. Event loop asla bloklanmaz.
- Çökmeden sonra yeniden başlatınca `jobs` tablosundaki `running` ama kilidi süresi dolmuş işler `queued`'a döner (crash recovery).

### 3.2 Pipeline (bir klibin yolculuğu)

```
            ┌─ (1) Reaktif: chat + ses + olay filtresi ─► Detector ──────┐
canlı akış ─┼─ (2) İçerik: LiveTranscriber ─► ContentScanner (Haiku) ────┼─► candidate (source=reactive|content|viewer_clip)
            └─ (3) İzleyici klibi: Twitch GQL clips ─────────────────────┘
   (aynı zaman aralığına düşen adaylar birleştirilir, skorlar ve source'lar birlikte tutulur)

candidate
   ─► job:cut        (segmentlerden [peak-60s, peak+25s] ham kesit → raw.mp4)
   ─► job:transcribe (faster-whisper, tr, word_timestamps → transcript.json)
   ─► job:prefilter  (Haiku 5.5: hızlı keep/drop + kaba skor)
   ─► job:judge      (Sonnet 5.5 / Opus 5.5: kesin start/end, başlık, metinler → llm.json)
   ─► job:render     (ffmpeg: 9:16 düzen + ASS altyazı + hook + loudnorm → final.mp4)
   ─► review (panel: manuel onay | auto: skor ≥ eşik ve izin var)
   ─► job:publish × platform (YouTube / Instagram / X / TikTok)
```

### 3.3 Klasör yapısı

```
clipper/
├─ pyproject.toml
├─ .env.example
├─ config.example.yaml
├─ CLAUDE.md
├─ ROADMAP.md
├─ assets/fonts/Montserrat-ExtraBold.ttf
├─ src/clipper/
│  ├─ __main__.py            # CLI: run, vod, auth, db, test-render, calibrate
│  ├─ config.py              # pydantic modelleri + YAML yükleme
│  ├─ db.py                  # SQLite bağlantısı (WAL), migration'lar
│  ├─ jobs.py                # kendi iş kuyruğumuz
│  ├─ log.py                 # JSON log + panel için ring buffer
│  ├─ models.py              # ChatMessage, Segment, Candidate... dataclass'lar
│  ├─ platforms/
│  │  ├─ twitch/  gql.py, live.py, hls.py, chat_irc.py, vod.py, vod_chat.py, helix.py (yedek)
│  │  ├─ kick/    live.py, hls.py, chat_pusher.py
│  │  └─ youtube/ live.py, hls.py (yt-dlp), innertube.py, chat_official.py (yedek)
│  ├─ recorder/   ring_buffer.py, hls_recorder.py, cutter.py
│  ├─ detect/     signals.py, detector.py, keywords_tr.py, audio.py
│  ├─ transcribe/ whisper.py
│  ├─ llm/        client.py, prompts.py, schemas.py, facecam_vision.py
│  ├─ render/     layouts.py, ass_captions.py, tr_text.py, ffmpeg.py
│  ├─ publish/    base.py, youtube.py, instagram.py, x.py, tiktok.py, oauth.py, scheduler.py
│  ├─ panel/      app.py, api.py, static/{index.html, app.js, style.css}
│  └─ util/       timeutil.py, fs.py, crypto.py, httpx_client.py
├─ tests/
│  ├─ fixtures/   (sentetik chat jsonl, ffmpeg testsrc ile üretilen küçük video)
│  └─ test_*.py
└─ data/          (gitignore: db, recordings, clips, models)
```

### 3.4 Veri modeli (SQLite, WAL modu)

```sql
CREATE TABLE streamers (
  id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  platform TEXT NOT NULL CHECK (platform IN ('twitch','kick','youtube')),
  channel TEXT NOT NULL,                 -- login / slug / channel_id
  enabled INTEGER NOT NULL DEFAULT 1,
  layout TEXT NOT NULL DEFAULT 'stack',  -- stack | blur | center
  facecam_rect TEXT,                     -- JSON {x,y,w,h} kaynak çözünürlüğe oranla (0-1)
  keywords TEXT,                         -- JSON ek anahtar kelimeler/emote ağırlıkları
  detector_overrides TEXT,               -- JSON eşik/lag override
  publish_targets TEXT,                  -- JSON ["youtube:hesapA","instagram:hesapB",...]
  auto_publish INTEGER NOT NULL DEFAULT 0,
  permission_ok INTEGER NOT NULL DEFAULT 0,
  permission_note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE sessions (
  id INTEGER PRIMARY KEY,
  streamer_id INTEGER NOT NULL REFERENCES streamers(id),
  source TEXT NOT NULL,                  -- live | vod
  title TEXT, category TEXT,
  started_at TEXT NOT NULL, ended_at TEXT,
  dir TEXT NOT NULL,                     -- data/sessions/<id>/
  status TEXT NOT NULL                   -- recording | ended | failed
);

CREATE TABLE segments (                  -- halka tampondaki HLS segmentleri
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  seq INTEGER NOT NULL,
  path TEXT NOT NULL,
  pdt_start REAL NOT NULL,               -- EXT-X-PROGRAM-DATE-TIME (unix sn)
  duration REAL NOT NULL,
  is_ad INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(session_id, seq)
);

CREATE TABLE signal_seconds (            -- saniyelik özet (grafik + kalibrasyon için)
  session_id INTEGER NOT NULL, t INTEGER NOT NULL,     -- unix sn
  msgs INTEGER, uniq INTEGER, emote REAL, kw REAL, audio_db REAL, score REAL,
  PRIMARY KEY (session_id, t)
);

CREATE TABLE candidates (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  peak_ts REAL NOT NULL, win_start REAL NOT NULL, win_end REAL NOT NULL,
  det_score REAL NOT NULL, features TEXT NOT NULL,     -- JSON
  status TEXT NOT NULL,  -- new | cut | transcribed | prefiltered | judged | rendered | rejected | failed
  created_at TEXT NOT NULL
);

CREATE TABLE clips (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL UNIQUE REFERENCES candidates(id),
  raw_path TEXT, transcript_path TEXT, llm_json TEXT,
  llm_score INTEGER, start_rel REAL, end_rel REAL,
  title TEXT, final_path TEXT, thumb_path TEXT,
  review TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
  reviewer_note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE publications (
  id INTEGER PRIMARY KEY,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  platform TEXT NOT NULL, account TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'direct',   -- direct | inbox (tiktok)
  status TEXT NOT NULL,                  -- queued | uploading | processing | published | failed
  remote_id TEXT, url TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  scheduled_at TEXT, published_at TEXT,
  UNIQUE(clip_id, platform, account)
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL, pool TEXT NOT NULL,      -- cpu | gpu | io
  payload TEXT NOT NULL,                       -- JSON
  status TEXT NOT NULL DEFAULT 'queued',       -- queued | running | done | failed | dead
  priority INTEGER NOT NULL DEFAULT 100,
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 4,
  run_after REAL NOT NULL, locked_until REAL, error TEXT,
  created_at REAL NOT NULL, finished_at REAL
);
CREATE INDEX jobs_pick ON jobs(status, pool, run_after, priority);

CREATE TABLE oauth_tokens (
  platform TEXT NOT NULL, account TEXT NOT NULL,
  data_enc BLOB NOT NULL,                      -- Fernet şifreli JSON (access, refresh, expires_at, scopes)
  updated_at TEXT NOT NULL,
  PRIMARY KEY (platform, account)
);

CREATE TABLE llm_usage (                       -- maliyet takibi
  id INTEGER PRIMARY KEY, ts REAL, model TEXT, purpose TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, usd REAL
);
```

**r3 migration (yeni migration dosyası olarak):**
```sql
ALTER TABLE candidates ADD COLUMN sources TEXT NOT NULL DEFAULT '["reactive"]';  -- JSON: reactive | content | viewer_clip
ALTER TABLE candidates ADD COLUMN content_score REAL;        -- içerik yolunun (Haiku) skoru
ALTER TABLE candidates ADD COLUMN content_reason TEXT;

CREATE TABLE live_events (                 -- platform olayları (sub/bits/raid/bağış/superchat/klip)
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  ts REAL NOT NULL, kind TEXT NOT NULL,    -- sub | gift_sub | bits | raid | donation | superchat | viewer_clip
  amount REAL, data TEXT
);
CREATE INDEX live_events_ts ON live_events(session_id, ts);

CREATE TABLE content_scans (               -- içerik yolu tarama kayıtları (maliyet ve kalibrasyon için)
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  win_start REAL NOT NULL, win_end REAL NOT NULL,
  score INTEGER, kind TEXT, reason TEXT, created_at REAL NOT NULL
);

CREATE TABLE feedback (                    -- onay/red geri bildirimi: öğrenme döngüsünün verisi
  id INTEGER PRIMARY KEY,
  clip_id INTEGER NOT NULL REFERENCES clips(id),
  decision TEXT NOT NULL,                  -- approved | rejected | edited
  reason TEXT,                             -- not_funny | no_context | bad_cut | duplicate | music | sensitive | low_quality | other
  start_delta REAL, end_delta REAL,        -- kullanıcı kırpmayı ne kadar değiştirdi
  title_edited INTEGER NOT NULL DEFAULT 0,
  features_snapshot TEXT NOT NULL,         -- karar anındaki TÜM sinyal özellikleri (JSON)
  created_at REAL NOT NULL
);

CREATE TABLE clip_stats (                  -- yayınlanan kliplerin performansı (Faz 8)
  publication_id INTEGER NOT NULL REFERENCES publications(id),
  fetched_at REAL NOT NULL, views INTEGER, likes INTEGER, comments INTEGER,
  PRIMARY KEY (publication_id, fetched_at)
);
```
Canlı transkript DB'ye değil `data/sessions/<id>/live_transcript.jsonl` dosyasına yazılır (`{t0, t1, text, words[]}` satırları, wall-clock zamanlı).

Ham chat mesajları DB'ye **yazılmaz**. `data/sessions/<id>/chat.jsonl.gz` dosyasına akar (hacim yüksek). DB'de sadece saniyelik özet tutulur.

### 3.5 Config (config.yaml) iskeleti

```yaml
paths: { data_dir: ./data, ffmpeg: ffmpeg, ffprobe: ffprobe }
panel: { host: 127.0.0.1, port: 8765 }
recorder:
  buffer_minutes: 15
  quality: "1080p60,1080p,936p60,720p60,best"   # tercih sırası
detector:
  bin_seconds: 1
  burst_window: 5          # kısa pencere (sn)
  baseline_halflife: 300   # EWMA yarı ömrü (sn)
  warmup_seconds: 180      # bu süre dolmadan tetikleme yok
  threshold: 3.0           # birleşik z-skor eşiği
  min_rate: 0.5            # mesaj/sn mutlak alt sınır
  min_ratio: 1.6           # baseline'ın en az kaç katı
  cooldown: 45
  lag_seconds: 20          # chat tepkisinin olaya göre gecikmesi (kalibre edilecek)
  pre_roll: 40             # ham kesit: olay - pre_roll
  post_roll: 25
  weights: { rate: 0.40, uniq: 0.25, emote: 0.20, audio: 0.15 }
transcribe: { model: large-v3, device: auto, compute_type: auto, language: tr, beam_size: 5 }
live_transcribe:           # r3: canlı transkript (içerik yolu için)
  enabled: true
  model: large-v3-turbo    # canlıda hız öncelikli; GPU yoksa "small"
  chunk_seconds: 20
  overlap_seconds: 4
  beam_size: 1
content_scan:              # r3: içerik yolu
  enabled: true
  model: claude-haiku-5-5
  every_seconds: 60
  window_seconds: 90
  min_score: 70            # bu skorun üstü aday olur
  skip_if_silent_ratio: 0.7   # pencerenin %70'i sessizse LLM'e gönderme
events:                    # r3: platform olay filtresi
  suppress_after_sub_train: 20   # ≥5 sub/gift olayından sonraki X sn reaktif tetik eşiği yükselir
  suppress_threshold_boost: 1.5
  raid_suppress_seconds: 60
viewer_clips:
  enabled: true
  as_candidate: true       # izleyici klibi tek başına aday açar
  bonus: 1.5
llm:
  prefilter_model: claude-haiku-5-5
  judge_model: claude-sonnet-5-5     # "premium" mod: claude-opus-5-5
  max_candidates_per_hour: 12
  min_keep_score: 65
  monthly_budget_usd: 30
render:
  width: 1080
  height: 1920
  fps: 30
  encoder: auto            # auto | h264_nvenc | libx264
  loudness_lufs: -14
  min_len: 12
  max_len: 59
publish:
  min_gap_minutes: { youtube: 90, instagram: 120, x: 60, tiktok: 120 }
  daily_cap: { youtube: 8, instagram: 6, x: 10, tiktok: 6 }
  auto_publish_min_score: 85
streamers: []   # panelden de eklenebilir
```

---

## 4. Algoritmalar ve teknik detaylar (Claude Code buna göre yazsın)

### 4.1 Chat kaynakları

**Twitch (okuma: anonim IRC over WebSocket, auth gerekmez)**
- `wss://irc-ws.chat.twitch.tv:443` → `CAP REQ :twitch.tv/tags twitch.tv/commands`, `PASS SCHMOOPIIE`, `NICK justinfan<rastgele 5 hane>`, `JOIN #<login>`.
- `PING` gelince `PONG`. Kopunca üstel backoff ile yeniden bağlan (1, 2, 4… max 60 sn).
- Tag'lerden al: `tmi-sent-ts` (ms, mesaj zamanı), `user-id`, `emotes` (emote id/konumları), `display-name`. Mesaj zamanı olarak `tmi-sent-ts` kullan, yerel saat değil.
- **Canlı mı / izleyici / başlık / kategori (anonim GQL):** `POST https://gql.twitch.tv/gql`, header `Client-ID: kimne78kx3ncx6brgo4mv6wki5h1ko` (Twitch web sitesinin public ID'si, hesap gerekmez). Sorgu: `user(login:"<login>") { id stream { id type createdAt viewersCount title game { name } } }`. `stream == null` → offline. Her 60 sn.
- **Klip sinyali (anonim GQL):** `user(login) { clips(criteria:{period:LAST_DAY, sort:CREATED_AT_DESC}, first:20) { edges { node { id createdAt viewCount } } } }`. Her 60 sn. İzleyicilerin yeni açtığı klipler güçlü sinyaldir (`createdAt` zamanına bonus).
- Ham sorgu reddedilirse (Twitch bazı alanlar için "integrity" ister veya persisted query zorunlu kılabilir): önce persisted query (`extensions.persistedQuery.sha256Hash`, hash'ler config'te) dene. O da olmazsa `monitoring.mode: official` → Helix `GET /helix/streams` ve `GET /helix/clips` (app token) yedeği. *(Claude Code: hangi sorguların anonim çalıştığını Faz 6 başında canlı test et, sonucu buraya not düş.)*
- **Hız sınırı:** Anonim GQL'e kanal başı dakikada ≤ 2 istek. Çoklu kanalda sorguları tek batch istekte birleştir (GQL dizi gövdesi kabul eder).

**Kick (resmi olmayan Pusher WS — kırılabilir, izole et)**
- Kanal bilgisi: `GET https://kick.com/api/v2/channels/{slug}` → `chatroom.id`, `livestream` (null değilse canlı), `playback_url`. Cloudflare yüzünden **`curl_cffi`** ile `impersonate="chrome"` kullan.
- *(2026-11-18 canlı doğrulandı)* **VOD:** `GET /api/v2/channels/{slug}/videos` düz dizi döner (canlı olan yayın da listede, `is_live`/`duration=0` ile ele). `GET /api/v1/video/{uuid}` → `source` (master m3u8, göreli varyant/segment yolları, segmentlerde PROGRAM-DATE-TIME), `livestream.start_time`, `livestream.duration` (ms), `livestream.channel_id`. HLS düz httpx ile iner. **Chat tekrarı:** `GET /api/v2/channels/{channel_id}/messages?start_time=<ISO>` start_time'dan sonraki ~5 sn'yi verir, ileri cursor yok; `start_time` 5'er sn ilerletilir, id ile tekrar elenir (1 saat ≈ 720 istek). Emote biçimi `[emote:<id>:<ad>]`. API çağrıları bu tarihte düz istemciyle 200 döndü; curl_cffi yedek (`kick.http_backend`). Komutlar: `clipper kick-vods <slug>`, `clipper vod --kick-vod <uuid>`.
- WS: `wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false`. Ardından `{"event":"pusher:subscribe","data":{"channel":"chatrooms.<id>.v2"}}`.
- Event `App\Events\ChatMessageEvent`; `data` alanı **string olarak JSON** → ikinci kez `json.loads`. Emote'lar mesaj içinde `[emote:ID:NAME]` formatında.
- Pusher key/cluster değişebilir: config'e taşı. Başlangıçta bağlantı testi yap, hata mesajı net olsun ("Kick Pusher anahtarı değişmiş olabilir").
- Resmi Kick API'nin `chat.message.sent` webhook'u public HTTPS endpoint ister ve hatada 3 denemeden sonra aboneliği düşürür. **v1'de kullanılmayacak**; `platforms/kick/` içinde ileride eklenecek alternatif olarak not düş.

**YouTube (varsayılan: innertube, anonim, kotasız)**
- Canlı videoId: `https://www.youtube.com/channel/<id>/live` (veya `/@handle/live`) sayfasını httpx ile çek, `ytInitialPlayerResponse` içinden `videoDetails.videoId` + `isLive` + izleyici sayısı parse et. Yedek: yt-dlp.
- Header'larda gerçekçi bir masaüstü `User-Agent` ve `Accept-Language: tr-TR` kullan. AB/TR'de çıkabilen consent sayfası için `SOCS`/`CONSENT` cookie'si gönder.
- **Canlı chat:**
  1. `GET https://www.youtube.com/live_chat?v=<videoId>` → HTML'den `ytcfg` (`INNERTUBE_API_KEY`, `INNERTUBE_CONTEXT_CLIENT_VERSION`) ve `ytInitialData` içindeki ilk `continuation` token'ı.
  2. Döngü: `POST https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key=<key>` gövde `{"context":{"client":{"clientName":"WEB","clientVersion":"<ver>","hl":"tr"}},"continuation":"<token>"}`.
  3. Cevaptaki `continuationContents.liveChatContinuation.actions[].addChatItemAction.item.liveChatTextMessageRenderer` → `timestampUsec`, `authorExternalChannelId`, `message.runs` (metin + `emoji` run'ları). Paid message / membership renderer'ları da sayılır (ekstra ağırlık 1.5).
  4. Sonraki token ve bekleme süresi: `continuations[0].(timedContinuationData|invalidationContinuationData).continuation / timeoutMs`.
- **VOD / bitmiş yayın chat replay:** `GET /live_chat_replay?v=<id>` → aynı şekilde key + continuation, sonra `POST /youtubei/v1/live_chat/get_live_chat_replay` (gövdeye `currentPlayerState: {playerOffsetMs: "<ms>"}`). Aksiyonlar `replayChatItemAction` içinde, `videoOffsetTimeMsec` ile gelir.
- Hepsi `platforms/youtube/innertube.py` içinde izole. Alan yolları değişirse parse hatası net loglansın.
- **Yedek (`monitoring.mode: official`):** Data API `videos.list` → `liveChatId`, sonra `liveChatMessages.streamList` (çağrı başı 5 birim, günlük 10.000 kota, sayaç DB'de).

**Ortak `ChatMessage` modeli:** `ts: float (unix sn), user_id: str (sha1 ile hash'lenmiş), text: str, emotes: list[str], platform: str`.

### 4.2 Kendi HLS kaydedicimiz

- **Twitch canlı:** GQL `PlaybackAccessToken` (POST `https://gql.twitch.tv/gql`, public web Client-ID: `kimne78kx3ncx6brgo4mv6wki5h1ko`) → `value` + `signature` → `https://usher.ttvnw.net/api/channel/hls/<login>.m3u8?sig=..&token=..&allow_source=true&fast_bread=true&p=<rand>`. Master playlist'ten config'teki kalite sırasına göre varyantı seç.
- **Kick canlı:** kanal API'sindeki `playback_url` (master m3u8).
- **YouTube canlı:** `yt-dlp -g` ile HLS URL al (tek yerde yt-dlp kullanılan nokta), sonra aynı kaydedici.
- Media playlist'i `target_duration/2` aralıkla çek. Yeni `seq` segmentlerini indir: `data/sessions/<id>/seg/<seq>.ts`. Her segment için `EXT-X-PROGRAM-DATE-TIME` değerini `pdt_start` olarak kaydet (yoksa: ilk segmentin indirme zamanı + kümülatif süre).
- **Twitch reklam segmentleri:** `#EXT-X-DATERANGE` içinde `CLASS="twitch-stitched-ad"` veya `stitched-ad` ID'li aralıklar ve `#EXTINF` başlığı `live` olmayan segmentler → `is_ad=1`, kesitlere dahil etme.
- Token süresi dolarsa (403) yeni access token al, kaldığı yerden devam et.
- Halka tampon: `buffer_minutes`'tan eski segmentler, bekleyen bir `cut` işi onları kapsamıyorsa silinir.
- **Kesici (`cutter.py`):** `[t0, t1]` wall-clock aralığını kapsayan segmentleri bul → concat listesi → `ffmpeg -f concat -safe 0 -i list.txt -ss <ofs> -t <dur> -c copy raw.mp4`. Hassas kesim render'da yeniden encode ile yapılır, burada keyframe hassasiyeti yeterli.
- **VOD modu (offline geliştirme için):** Twitch VOD → GQL `PlaybackAccessToken` (isVod) → `https://usher.ttvnw.net/vod/<id>.m3u8?...`. Twitch VOD chat → GQL `VideoCommentsByOffsetOrCursor` (persisted query hash'i değişebilir, izole et). Ayrıca **yerel dosya modu:** `clipper vod --video x.mp4 --chat chat.jsonl` (her platformdan bağımsız test için).

### 4.3 Dedektör

Saniyelik bin'ler (t = unix sn):
- `msgs[t]` mesaj sayısı, `uniq[t]` tekil kullanıcı sayısı
- `emote[t]` = Σ emote/anahtar kelime ağırlıkları. Aynı kullanıcının aynı saniyedeki tekrarları 1'e kırpılır (spam kırpma). Kullanıcı başına 5 sn'de max 2 mesaj sayılır.
- `audio_db[t]`: kaydedilen segmentlerden ffmpeg ile mono 16 kHz PCM → numpy RMS → dBFS (1 sn bin). Ses, chat'ten `lag_seconds` kadar **önce** olduğu için ses sinyali skora `t - lag_seconds` hizasıyla girer.

Hesap (her saniye):
1. `x_k(t)` = son `burst_window` saniyelik toplam (her özellik k için).
2. Baseline: EWMA ortalama `μ_k` ve EWMA varyans `σ²_k` (yarı ömür `baseline_halflife`). **Tetikleme anında baseline güncellenmez** (spike kendi baseline'ını şişirmesin).
3. `z_k = (x_k − μ_k) / max(σ_k, floor_k)`; floor_k: düşük trafikli kanallarda sıfıra bölmeyi ve aşırı hassasiyeti önler (örn. rate için 1.0 mesaj).
4. `S = Σ w_k · clip(z_k, −2, 10)`
5. Tetik: `S ≥ threshold` **ve** `rate ≥ min_rate` **ve** `x_rate ≥ min_ratio · μ_rate` **ve** warmup bitti **ve** cooldown dışında.
6. Tetik sonrası 10 sn içinde `S`'nin maksimumu = `peak_ts`. Olay tahmini: `event_ts = peak_ts − lag_seconds`.
7. Kesit penceresi: `[event_ts − pre_roll, peak_ts + post_roll]`. Çakışan adaylar birleştirilir.
8. Twitch'te izleyici klibi geldiyse: o anın ±30 sn'sindeki adayın skoruna +1.5 bonus. Aday yoksa `viewer_clips.as_candidate` açıksa doğrudan aday aç (`source=viewer_clip`). İzleyici klibinin `createdAt`'i olayın ~10–40 sn sonrasıdır; pencereyi buna göre geriye kaydır.
8b. **Olay filtresi (r3):** Platform olayları `live_events`'e yazılır. Kaynaklar: Twitch IRC `USERNOTICE` (`msg-id`: sub, resub, subgift, submysterygift, raid) ve `PRIVMSG` içindeki `bits` tag'i; Kick Pusher event'leri (`SubscriptionEvent`, `GiftedSubscriptionsEvent`, `StreamHostEvent` — adlar canlı testte doğrulanacak); YouTube innertube `liveChatPaidMessageRenderer` / `liveChatMembershipItemRenderer`. Bu olaylar **aday üretmez**. Sub yağmuru / raid sonrası chat patlaması "an" değil, o yüzden:
   - 5 sn içinde ≥5 sub/gift olayı → sonraki `suppress_after_sub_train` sn boyunca `threshold × suppress_threshold_boost`.
   - Raid → `raid_suppress_seconds` boyunca reaktif tetik kapalı (izleyici sayısı baseline'ı da sıfırlanmaz, yeni izleyicilerle yavaşça adapte olur).
   - "Teşekkürler X abone için" tarzı chat dalgaları: aynı kelimelerin (`hoş geldin`, `welcome`, `sub`) baskın olduğu pencerelerde emote skoru 0.3× çarpanla düşürülür.
   - Bağıştan sonraki yayıncı tepkisi yine de içerik yolu ile yakalanabilir.
9. Her adayın `features` JSON'una tüm z'leri, ham değerleri ve en sık 20 chat mesajını yaz (LLM'e gidecek).

`keywords_tr.py` varsayılan ağırlıklar (yayıncı bazında config ile genişletilebilir):
- Gülme: `KEKW, OMEGALUL, LUL, LULW, ICANT, pepeLaugh, 😂, 🤣, xd, xD, sjsjsj, ahsdj` + regex `(?:a?ha){3,}`, `(?:s?j){4,}` → 1.0
- Şok: `monkaS, PogChamp, Pog, POGGERS, 😱, 😳, oha, ohaa+, yok artık, nasıl ya` → 1.2
- Klip talebi: `clip, klip, kliple, clipit, clip it, klipp+` → 2.0
- Övgü: `efsane, helal, GG, ez, 🔥` → 0.6
- Yas/fail: `F, RIP, 💀` → 0.8
- Tekrar spam (aynı metin ≥5 farklı kullanıcıdan 5 sn içinde) → mesaj başı +0.5

### 4.3b İçerik yolu (r3): canlı transkript + Haiku taraması

**LiveTranscriber** (session başına bir async görev, gpu_pool'u kullanır):
- Recorder yeni segment yazdıkça sesi `chunk_seconds` (20 sn) + `overlap_seconds` (4 sn) parçalar halinde 16 kHz mono PCM'e çevirir, faster-whisper'a verir (`beam_size=1`, `vad_filter=True`, `word_timestamps=True`).
- Overlap bölgesindeki kelimeler zaman damgasına göre tekilleştirilir. Çıktı wall-clock zamanlı olarak `live_transcript.jsonl`'a eklenir.
- **Öncelik kuralı:** gpu_pool'da kesin klip transkripti (`transcribe` işi) canlı transkriptten önce gelir. Canlı transkript gerçek zamanın 60 sn'den fazla gerisine düşerse eski parçaları atla ve logla.
- Kazanç: klip kesildiğinde transkriptin büyük kısmı hazırdır. `transcribe` işi sadece kenarlarda (large-v3 ile) yeniden yazar.
- Ek ucuz sinyaller transkriptten çıkar ve dedektöre beslenir: **konuşma hızı** (kelime/sn, yayıncının kendi baseline'ına göre z-skor) ve **anahtar laf** puanı (`keywords_tr.py`'deki şok/gülme kelimeleri yayıncının ağzından çıkınca ayrıca puanlanır).

**ContentScanner** (session başına bir async görev):
- Her `every_seconds` (60 sn) son `window_seconds` (90 sn) transkripti alır. Pencerenin `skip_if_silent_ratio`'dan fazlası sessizse atlar.
- Haiku 5.5'e gönderir. Girdi: yayıncı adı, kategori, pencere transkripti (saniye ofsetli), aynı penceredeki chat'ten 15 örnek mesaj + mesaj/sn özeti. System prompt cache'li.
- Çıktı (tool use, zorunlu JSON): `{score: 0-100, kind: "story|joke|rant|argument|reaction|info|none", reason, best_start, best_end}`
- `score ≥ min_score` → `candidates` (`source=content`). Pencere `[best_start − 10, best_end + 10]`. Önceki pencereyle çakışıyorsa birleştir (hikâyeler 90 sn'den uzun sürebilir: art arda iki yüksek skorlu pencere tek aday olur, max 3 dk).
- Her tarama `content_scans`'e yazılır.
- Maliyet: ~60 çağrı/saat × ~1.5k girdi + ~100 çıktı token → Haiku 5.5 ile **~$0.01/yayın saati**.

**Birleştirme:** Reaktif, içerik ve izleyici klibi adayları ±20 sn içinde çakışıyorsa tek aday olur, `sources` dizisinde hepsi tutulur. Birden fazla kaynaktan gelen aday prefilter'da öncelik alır (prefilter'ı atlayıp doğrudan judge'a gidebilir: `sources` uzunluğu ≥2 ise).

### 4.4 Transkript

- `faster-whisper` `WhisperModel(model, device, compute_type)`; GPU'da `float16` veya `int8_float16`, CPU'da `int8`.
- `transcribe(raw.wav, language="tr", word_timestamps=True, vad_filter=True, beam_size=5, condition_on_previous_text=False)`.
- Çıktı: `[{start, end, text, words:[{w, s, e, p}]}]` → `transcript.json`.
- Model tek sefer yüklenir, `gpu_pool` tek işçidir (VRAM).

### 4.5 LLM katmanı (Claude)

- Model adları config'ten: `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`. Fiyatlar (girdi/çıktı, $/1M token): Haiku 5.5 0.10/0.50, Sonnet 5.5 2/10, Opus 5.5 4/20. Fiyat tablosunu `llm/client.py`'de sabit tut, `llm_usage`'e her çağrının maliyetini yaz. `monthly_budget_usd` aşılırsa judge'ı Haiku'ya düşür ve panelde uyarı göster. *(Claude Code: fiyatları ve model adlarını docs.claude.com'dan doğrula.)*
- System prompt sabit ve uzun → **prompt caching** (`cache_control: ephemeral`) kullan.
- Çıktı **tool use ile zorunlu JSON** (tek tool: `submit_clip_decision`, `tool_choice` ile zorlanır). Pydantic ile doğrula. Geçersizse hata mesajını ekleyip 1 kez tekrar dene.

**Prefilter (Haiku 5.5)** girdi: yayıncı adı, kategori, transkript düz metin, en sık chat mesajları, dedektör özellikleri. Çıktı: `{keep: bool, rough_score: 0-100, reason: str}`. Saatlik `max_candidates_per_hour` sınırı: skora göre en iyileri judge'a geçer.

**Judge (Sonnet 5.5 veya Opus 5.5)** girdi:
- Saniye ofsetli transkript (`[12.4] kelime kelime…`, 0 = ham kesit başı)
- Chat'in zaman çizelgesi (ham kesit ofsetine çevrilmiş, saniye başı mesaj sayısı + örnek mesajlar)
- Ses enerjisi profili (saniye başı dB, kısaltılmış)
- Bu yayıncının son 30 yayınlanmış başlığı (tekrarı önlemek için)
- (r3) Adayın kaynakları (`sources`), içerik yolunun `kind` + `reason`'ı, penceredeki platform olayları (örn. "bu sırada 10 gift sub geldi")
- (r3) Bu yayıncı için son 20 **reddedilen** klibin red nedeni özeti ve son 20 **onaylanan** klibin başlığı. Böylece model kullanıcının zevkini few-shot olarak görür (öğrenme döngüsünün ucuz, ilk gün çalışan hali; bkz. 4.9).

Çıktı şeması (`llm/schemas.py`):
```json
{
  "keep": true,
  "score": 0,
  "reason": "kısa gerekçe",
  "start": 0.0, "end": 0.0,
  "title": "≤ 70 karakter, Türkçe, clickbait ama yalan değil",
  "hook": "ilk 2.5 sn ekranda görünecek ≤ 6 kelime",
  "caption_fixes": [{"from": "yanlış duyulan", "to": "doğrusu"}],
  "hashtags": ["#..."],
  "texts": {
    "youtube": {"title": "...", "description": "..."},
    "instagram": {"caption": "..."},
    "tiktok": {"caption": "..."},
    "x": {"text": "≤ 270 karakter"}
  },
  "flags": {"music_risk": false, "nsfw": false, "sensitive_topic": false, "needs_context": false}
}
```
Kurallar (prompt'a yazılacak): klip tek başına anlaşılmalı. Başlangıç cümle ortasında olmamalı (kelime sınırına hizala). Süre `min_len`–`max_len`. Ünlü telifli müzik duyuluyorsa `music_risk=true`. Hakaret/nefret içeriği varsa `sensitive_topic=true` (bunlar otomatik yayınlanmaz).

`start/end` kodla tekrar kelime sınırına snap edilir (en yakın kelime başlangıcı − 0.15 sn / bitişi + 0.3 sn).

**Facecam tespiti (`facecam_vision.py`):** Yayın başında 3 kare (ffmpeg ile) → Claude Sonnet 5.5 vision → `{has_facecam, rect:{x,y,w,h} oranlı}`. Sonuç `streamers.facecam_rect`'e öneri olarak yazılır, panelde onaylanır. Onaylı değer varsa vision çağrılmaz.

### 4.6 Render (saf ffmpeg)

Tek pass: `raw.mp4` → `-ss start -to end` (input seek + doğru kesim için yeniden encode) → filtre grafiği → `final.mp4`.

**Düzenler (`layouts.py` filtre grafiği üretir):**
- `stack` (facecam varsa): üstte facecam `1080x608`, altta oyun `1080x1312`.
  ```
  [0:v]crop=iw*W:ih*H:iw*X:ih*Y,scale=1080:608:force_original_aspect_ratio=increase,crop=1080:608[cam];
  [0:v]crop=ih*1080/1312:ih:(iw-ih*1080/1312)/2:0,scale=1080:1312[game];
  [cam][game]vstack=inputs=2[v0]
  ```
- `blur` (facecam yok, geniş görüntü): arka plan büyütülmüş + `boxblur=20:2`, ön plan `scale=1080:-2` ortada.
- `center`: `crop=ih*9/16:ih,scale=1080:1920`.
- Sonra: `[v0]subtitles=clip.ass:fontsdir=assets/fonts[v]`. Windows'ta ass yolundaki `:` ve `\` kaçışlarına dikkat: dosyayı çalışma dizinine kopyalayıp göreli yol ver.
- Ses: `loudnorm=I=-14:TP=-1.5:LRA=11` (iki geçişli loudnorm ileride; v1 tek geçiş yeterli).
- Encoder: `auto` → `ffmpeg -encoders` çıktısında `h264_nvenc` varsa `-c:v h264_nvenc -preset p5 -cq 23 -b:v 0`, yoksa `-c:v libx264 -preset veryfast -crf 21`. Ortak: `-pix_fmt yuv420p -r 30 -c:a aac -b:a 160k -ar 48000 -movflags +faststart`.
- Thumbnail: en yüksek ses enerjili saniyeden 1 kare `thumb.jpg`.

**Altyazı (`ass_captions.py`) — kendi ASS üreticimiz:**
- PlayResX=1080, PlayResY=1920. Stil: Montserrat ExtraBold 78, beyaz, siyah outline 6, gölge 0, hizalama 5 (orta). `stack` düzeninde y≈640 (iki panelin birleşim yeri), diğerlerinde y≈1350.
- Kelimeler 2–4'lük gruplara bölünür (max 18 karakter/satır, noktalama ve >0.6 sn sessizlikte kır).
- Aktif kelime vurgusu: grup gösterilirken her kelime aralığı için ayrı `Dialogue` satırı. Aktif kelime `{\c&H00FFFF&}` (sarı) + hafif `\fscx110\fscy110`, diğerleri beyaz.
- Hook: ilk 2.5 sn üstte (y≈180) beyaz kutu içinde siyah yazı (`BorderStyle=3`).
- `caption_fixes` transkripte uygulanır.
- **Türkçe büyük harf (`tr_text.py`):** `str.upper()` kullanma! `i→İ`, `ı→I` eşlemesi yapan `tr_upper()` yaz, testini yaz ("istanbul ılık" → "İSTANBUL ILIK").

### 4.7 Yayınlama (kendi istemcilerimiz)

Ortak arayüz (`publish/base.py`):
```python
class Publisher(Protocol):
    platform: str
    async def ensure_auth(self, account: str) -> None: ...
    async def upload(self, clip: ClipForPublish, account: str) -> PublishResult: ...
    async def check_status(self, pub: Publication) -> PublishResult: ...
```
- `scheduler.py`: onaylanan klipleri platform başına `min_gap_minutes` ve `daily_cap` kurallarına göre `scheduled_at` ile sıraya koyar. Aynı klibi tüm platformlara aynı anda basmaz, aralık bırakır.
- Hatalar: 429/5xx → üstel backoff. 4xx (yetki/format) → `failed`, panelde net mesaj. Token yenileme otomatik.
- `oauth.py`: genel OAuth2 (authorization code + PKCE) akışı. Loopback redirect `http://127.0.0.1:8765/callback/<platform>`. Panel "Hesap bağla" butonu tarayıcıda yetki sayfasını açar. Token'lar Fernet ile şifrelenip `oauth_tokens`'a yazılır (anahtar `.env`'de `CLIPPER_SECRET_KEY`).

**YouTube (`youtube.py`)**
- Scope `https://www.googleapis.com/auth/youtube.upload`. Resumable upload: `POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status` (metadata) → `Location` URL'ine 8 MB chunk'larla `PUT` (Content-Range). Kopmada `Content-Range: bytes */size` ile kaldığı yeri sor.
- `snippet.title` (≤100), `description`, `tags`, `categoryId=20` (Gaming), `status.privacyStatus` (config: public/unlisted), `selfDeclaredMadeForKids=false`. 9:16 ve ≤3 dk otomatik Short olur.
- Kota: upload ~100 birim; Haziran 2026'dan beri ayrı "Video Uploads" kovası. Günlük sayaç tut. *(Claude Code: güncel kota sayfasından doğrula.)*

**Instagram Reels (`instagram.py`)** — public URL gerektirmeyen resumable yol
1. `POST /{ig-user-id}/media` `media_type=REELS&upload_type=resumable&caption=...&share_to_feed=true` → container `id` + `uri`
2. `POST {uri}` (rupload.facebook.com) header: `Authorization: OAuth <token>`, `offset: 0`, `file_size: <bytes>`, gövde = video baytları
3. `GET /{container-id}?fields=status_code` → `FINISHED` olana kadar 10 sn arayla bekle (`ERROR` → failed)
4. `POST /{ig-user-id}/media_publish?creation_id=<container-id>` → media id. Permalink için `GET /{media-id}?fields=permalink`
- Yayından önce `GET /{ig-user-id}/content_publishing_limit` kontrol et.
- Graph API sürümünü config'te tut. *(Claude Code: Instagram Login vs Facebook Login host'larını — `graph.instagram.com` / `graph.facebook.com` — güncel dokümana göre seç.)*

**X (`x.py`)**
- OAuth 2.0 PKCE (user context). Media upload v2 chunked akış: initialize → append (≤ 5 MB parçalar) → finalize → `processing_info` bitene kadar status poll. Sonra `POST https://api.x.com/2/tweets` `{"text": ..., "media": {"media_ids": [id]}}`.
- Pay-per-use: gönderi başı ~$0.015; metne link koyma (link'li gönderi ~$0.20). Video ≤140 sn.
- *(Claude Code: v2 media upload endpoint adlarını ve alanlarını developer.x.com'dan doğrula; v1.1 kullanma.)*

**TikTok (`tiktok.py`)**
- Varsayılan **inbox modu** (scope `video.upload`): `POST https://open.tiktokapis.com/v2/post/publish/inbox/video/init/` `source_info: {source: FILE_UPLOAD, video_size, chunk_size, total_chunk_count}` → `upload_url`'e `PUT` (Content-Range) → `POST /v2/post/publish/status/fetch/` ile durum. Video kullanıcının TikTok uygulamasına taslak/bildirim olarak düşer, son dokunuşu biz yaparız.
- **Direct modu** (scope `video.publish`) kodda hazır dursun ama config'te kapalı: önce `POST /v2/post/publish/creator_info/query/` → `privacy_level_options`. Audit yoksa sadece `SELF_ONLY`. Audit sonrası açılır.
- Not: TikTok yönergeleri "başka platformlardan rastgele içerik kopyalayan" uygulamaları kabul etmiyor. Audit başvurusunda yayıncı izinli, editlenmiş özgün klip üretimi olarak anlat.

### 4.8 Panel (FastAPI + vanilla JS)

Sayfalar (tek sayfa, sekmeli):
1. **Canlı**: yayıncılar, canlı mı, kayıt durumu, son 30 dk sinyal grafiği (SVG, kendi çizimimiz), aday işaretleri.
2. **Klipler**: render edilmiş klipler kartlar halinde. `<video>` önizleme, LLM skoru/gerekçesi, başlık ve metinler düzenlenebilir, start/end ±sn ayarı ("yeniden render"), Onayla / Reddet, hangi platformlara.
3. **Yayınlar**: publications tablosu, durum, link, hata, "tekrar dene".
4. **Yayıncılar**: ekle/düzenle, facecam rect onayı (karenin üstünde dikdörtgen çizerek), izin notu, auto_publish.
5. **Hesaplar**: platform hesabı bağla/yenile, kota/kredi sayaçları.
6. **Maliyet & Log**: LLM harcaması (gün/ay), son 500 log satırı.

API: `/api/streamers`, `/api/sessions/{id}/signals`, `/api/clips`, `/api/clips/{id}/approve|reject|rerender`, `/api/publications`, `/api/auth/{platform}/start`, `/callback/{platform}`, `/media/{path}` (data dizininden güvenli dosya servisi, path traversal korumalı). Panel sadece `127.0.0.1`'e bind olur. Uzaktan erişim gerekirse basit parola (`.env`) + bilinçli olarak `0.0.0.0`.

Reddet/onay kararları `feedback` tablosuna yazılır (bkz. 4.9). Reddet butonu tek tıkla neden seçtirir: *komik değil · bağlamsız · kötü kesim · tekrar · müzik · hassas · kalitesiz · diğer*. Kullanıcı kırpmayı veya başlığı değiştirip onaylarsa `edited` olarak kaydedilir, farklar saklanır.

### 4.9 Öğrenme döngüsü (r3)

Üç kademe. Veri toplama ilk günden, öğrenme veri biriktikçe:

**Kademe 0 — Veri toplama (Faz 3 + Faz 5, zorunlu):**
- Her aday oluştuğunda **tüm** özellikler `candidates.features`'a yazılır: reaktif z'ler, ham değerler, konuşma hızı, anahtar laf puanı, içerik skoru/türü, izleyici klibi var mı, olay filtresi aktif miydi, kategori, saat, izleyici sayısı, LLM prefilter/judge skorları.
- Panelde karar verildiğinde bu özelliklerin kopyası `feedback.features_snapshot`'a donar. Sonradan özellik hesabı değişse bile eğitim verisi tutarlı kalır.
- Reddedilen adaylar da (LLM'in elediği, hiç render edilmeyenler dahil) `status=rejected` ile saklanır; bunlar negatif örnek.

**Kademe 1 — Few-shot geri bildirim (Faz 3'ten itibaren, otomatik):**
- Judge prompt'una yayıncı bazında son onay/red örnekleri eklenir (4.5). Eğitim gerektirmez, ilk onaydan itibaren etki eder.

**Kademe 2 — Öğrenilmiş skor modeli (Faz 8, ≥200 karar sonrası):**
- `clipper learn [--streamer X]`: `feedback` + `candidates`'tan veri seti kur. Hedef: `approved`/`edited` = 1, `rejected` = 0.
- Model: **numpy ile kendi yazdığımız L2-regularize logistic regression** (ek kütüphane yok, 15–30 özellik için yeterli). Global model + veri yetiyorsa (≥100 karar) yayıncı bazlı model; az veride global modele doğru shrinkage.
- Kullanım:
  - Reaktif dedektör ağırlıkları (`weights`) ve `threshold` modelden türetilir. Hangi sinyal bu yayıncıda daha belirleyiciyse ağırlığı artar.
  - Prefilter öncesi bir "kabul olasılığı" hesaplanır. Olasılığı çok düşük adaylar LLM'e hiç gitmez (maliyet düşer), çok yüksekler prefilter'ı atlar.
  - `min_keep_score` ve `auto_publish_min_score` önerilir.
- Değerlendirme: zaman bazlı ayırma (son %20 karar test). Rapor: precision@k, ROC-AUC, önceki ağırlıklara göre fark. Yeni ağırlıklar **otomatik uygulanmaz**; panelde "öneriyi uygula" butonu. Eski ağırlıklar versiyonlu saklanır, geri alınabilir.

**Kademe 3 — Performans sinyali (Faz 8, opsiyonel):**
- Yayınlanan kliplerin izlenme/beğeni sayıları günde bir çekilir (`clip_stats`; YouTube `videos.list?part=statistics`, Instagram `/{media-id}/insights`, X post metrics).
- Kullanıcı onayı "iyi mi" sorusunu, izlenme ise "tutuyor mu" sorusunu cevaplar. İkinci bir hedef olarak 7. gün izlenmesinin yayıncı medyanına oranı (log) kullanılır. Modele ek özellik değil, **ayrı bir hedef** olarak eğitilir. Panelde "hangi sinyal izlenme getiriyor" raporu üretilir.

---

## 5. Aşamalar (Claude Code bu sırayla uygular)

### Faz 0 — İskelet
- [x] `uv` projesi, `pyproject.toml`, `src/` layout, `clipper` CLI (`argparse`, harici CLI kütüphanesi yok)
  - Not: Faz 0'da `doctor`, `db migrate|version` ve `keygen` (CLIPPER_SECRET_KEY üretir) komutları var; diğerleri ilgili fazda eklenecek.
- [x] `config.py` (pydantic + YAML + `.env` okuma, kendi küçük `.env` parser'ımız)
  - Not: r2 gereği `monitoring.mode: anonymous | official` alanı eklendi (varsayılan `anonymous`).
- [x] `db.py`: bağlantı (WAL, `foreign_keys=ON`), sürüm numaralı migration sistemi (`schema_version` tablosu), Bölüm 3.4 şeması
- [x] `jobs.py`: `enqueue(type, payload, pool, priority, run_after)`. `claim(pool)` → `UPDATE … WHERE id = (SELECT … LIMIT 1) RETURNING *` atomik alma + `locked_until`. Başarı/başarısızlık, üstel backoff, `dead` durumu, crash recovery.
  - Not: kurtarılan işler `run_after` değerini korur (sıradaki yerini kaybetmez); kilit süresi dolmuş ama denemesi bitmiş iş `dead` olur. Uzun işler için `heartbeat()` kilidi uzatır.
- [x] `log.py`: JSON satır log (dosya, günlük rotasyon) + bellekte son 500 satır
- [x] `util/crypto.py` (Fernet), `util/timeutil.py`, `util/fs.py` (atomik yazma, disk doluluk kontrolü)
- [x] `ffmpeg` / `ffprobe` keşfi ve sürüm/encoder kontrolü: `clipper doctor` komutu (ffmpeg, libass, nvenc, CUDA, model, API key, disk alanı → yeşil/kırmızı rapor)
  - Not: keşif kodu `src/clipper/ffmpeg_probe.py`, rapor `src/clipper/doctor.py` (3.3'teki yapıya eklendi). Üç seviye: yeşil / sarı uyarı / kırmızı; sadece kırmızı varsa çıkış kodu 1. NVENC derlemede olsa bile küçük bir deneme encode'u ile gerçekten çalıştığı kontrol ediliyor. `--online` bayrağı Faz 6'da.
- [x] pytest altyapısı + `tests/fixtures/make_fixtures.py` (ffmpeg `testsrc2` + `sine` ile 3 dk 1920x1080 test videosu, sentetik chat jsonl: düz trafik + 3 belirgin spike)
  - Not: ek olarak GitHub Actions CI (Ubuntu + Windows: ruff, pytest, doctor) eklendi.

**Bitti sayılması için:** `uv run clipper doctor` çalışıyor. `uv run pytest` yeşil. İş kuyruğu testleri (eşzamanlı claim'de çift alma yok, crash recovery) geçiyor.

### Faz 1 — Offline giriş (VOD / yerel dosya)
- [x] `clipper vod --video <file> --chat <jsonl> --streamer <ad>`: session oluşturur, videoyu segment gibi kaydeder (tek segment, pdt=0 tabanlı)
  - Not: chat'teki zamanlar mutlaksa (unix sn) video başına hizalanır: `--video-start` > videonun `creation_time` etiketi > ilk mesaj (uyarı loglanır). Video aynı diskteyse hardlink, değilse kopya. Ek seçenekler: `--platform`, `--channel`, `--title`.
- [x] Twitch VOD indirici (kendi kodumuz: GQL token + usher VOD m3u8 + segment indirme + concat). `clipper vod --twitch-vod <id>`
  - Not: `PlaybackAccessToken` ham sorgu olarak gönderiliyor (config'te persisted hash verilirse o kullanılır); usher VOD parametreleri `nauth` / `nauthsig`. Yarım kalan indirme `data/downloads/twitch-<id>/` üzerinden devam eder. fMP4 (`#EXT-X-MAP`) VOD'lar henüz desteklenmiyor. **Canlı ağ doğrulaması bekliyor** (bulut ortamından Twitch'e istek atılamadı): `uv run pytest -m online` + gerçek VOD denemesi.
- [x] Twitch VOD chat indirici (GQL comments, cursor ile sayfalama → jsonl). Persisted query hash'i config'te.
  - Not: hash `twitch.persisted_queries.VideoCommentsByOffsetOrCursor`. Hata mesajları hash değişimi / integrity durumunu ayrı söyler.
- [x] YouTube VOD: video yt-dlp ile, chat replay innertube `get_live_chat_replay` ile (anonim) → jsonl. `clipper vod --youtube <videoId>`
  - Not: key/version/continuation `/live_chat_replay?v=` yerine `/watch?v=` sayfasından alınıyor (`ytcfg` + `ytInitialData`); "tüm mesajlar" görünümü tercih ediliyor. yt-dlp Python bağımlılığı değil, PATH'teki komut olarak çağrılıyor (`config: youtube.yt_dlp`). Paid/membership mesajları `weight=1.5` ile kaydediliyor. Canlı ağ doğrulaması bekliyor.
- [x] Ortak chat jsonl formatı + dönüştürücüler (TwitchDownloader JSON formatından içe aktarma da olsun, elde hazır veri varsa diye)
  - Not: `src/clipper/chatlog.py`; `.jsonl` / `.jsonl.gz` okur-yazar. `clipper chat-import <td.json> <out.jsonl.gz>` komutu eklendi; `--chat` TwitchDownloader `.json` dosyasını doğrudan da kabul eder.

**Bitti sayılması için:** Gerçek bir Twitch VOD'u (kısa, 30–60 dk) video + chat olarak indirilip session'a dönüşüyor. Fixture ile aynı akış testte çalışıyor.

### Faz 2 — Dedektör
- [x] `signals.py` (bin'leme, spam kırpma, emote/anahtar kelime puanlama), `keywords_tr.py`
  - Not: `signal_seconds.emote` = emote + anahtar kelime + tekrar-spam toplam skoru (dedektörün kullandığı); `kw` = klip talebi mesaj sayısı (bilgi amaçlı). Bir kategori mesaj başına bir kez sayılır. Emote'lar büyük/küçük harfe duyarlı, Türkçe kelimeler değil (`render/tr_text.py` Faz 2'de erkenden yazıldı: `tr_upper`, `tr_lower`, `tr_casefold`). YouTube paid/membership mesajlarının ek ağırlığı (`weight - 1`) emote skoruna eklenir.
- [x] `audio.py` (segmentlerden PCM → dB/sn)
  - Not: PCM bellekte tutulmaz, saniye saniye okunur; saniyeler PDT'ye hizalı.
- [x] `detector.py` (Bölüm 4.3). Hem **akış** (canlı, saniye saniye `feed()`) hem **toplu** (VOD) modda aynı sınıf.
  - Not: (1) EWMA soğuk başlangıçta düz ortalama gibi davranır (`alpha = max(alpha, 1/n)`), yoksa baseline ilk saniyelere takılıp sürekli yanlış pozitif üretiyordu. (2) z paydası `max(σ, floor, √μ)`: sayım verisi için Poisson gürültü tabanı. (3) z 10'da kırpıldığı için tepe noktası eşitlikte kırpılmamış toplam reaksiyona göre seçilir. Yeni config alanları: `floors`, `peak_window`, `viewer_clip_bonus`, `viewer_clip_radius`. "Aday yoksa izleyici klibinden düşük eşikle aday aç" kısmı canlı klip sinyali gelince (Faz 6) eklenecek; bonus kısmı hazır (`add_viewer_clip`). Sentetik chat'te 40 rastgele tohumda 3/3 spike, 0 yanlış pozitif.
- [x] `signal_seconds` ve `candidates` yazımı, `cut` işini kuyruğa atma
  - Not: `clipper detect <session>` (yeniden çalıştırma `--force`, sadece henüz işlenmemiş adaylar silinir). `clipper vod` sonunda dedektör + rapor otomatik çalışır (`--no-detect`). Yayıncı bazlı `detector_overrides` ve `keywords` uygulanır. Kesit işi önceliği skora göre.
- [x] `clipper report <session_id>` → tek HTML dosyası: sinyal grafiği (kendi SVG'miz), aday işaretleri, her adayın en sık chat mesajları
  - Not: varsayılan çıktı `data/sessions/<id>/report.html`; açık/koyu tema.
- [x] (r3) Olay filtresi: `live_events` yazımı ve sub yağmuru / raid bastırma (4.3 madde 8b). VOD modunda Twitch VOD chat'teki USERNOTICE karşılıkları da işlenir.
  - Sapma: 2026-11-18 canlı doğrulamada ne Twitch VOD chat'inde (persisted `VideoCommentsByOffsetOrCursor`: sadece `fragments`, `userBadges`, `userColor`) ne Kick chat replay'inde (`type` sadece `message` / `reply`) sub/gift/raid/bits olayı yok. Bu yüzden VOD modunda olay kaynağı yok; filtre (`detect/events.py`, `EventFilter`) ve `live_events` tablosu hazır, olaylar Faz 6'da canlı IRC/Pusher/innertube'dan gelecek. "Hoş geldin / sub için teşekkürler" dalgası bastırması chat metninden çalıştığı için VOD'da da aktif (`events.thanks_words`).
  - Not: raid sırasında baseline yavaş (yarılanma süresiyle) adapte olur; soğuk başlangıç hızlandırması kullanılmaz.
- [x] (r3) İzleyici klibi yolu: VOD'da GQL ile o VOD'dan açılmış klipleri çek (`video(id) { clips }` veya kanalın `clips` listesi, `vod offset` ile eşle), aday yap.
  - Not: `video(id) { clips }` anonim sorguda hep boş dönüyor (2026-11-18). Kanalın `user(login).clips(criteria)` listesi VOD yaşına göre en dar periyotla (`LAST_DAY` … `ALL_TIME`) sayfalanıp `video.id` ile süzülüyor, zaman `videoOffsetSeconds` (`platforms/twitch/clips.py`). Klipler `live_events` (`kind=viewer_clip`) olarak yazılır; çekim hatası ingest'i durdurmaz. Kick ve YouTube'da karşılığı yok.
- [x] (r3) Aday birleştirme (±20 sn, `sources` dizisi), tüm özelliklerin `features`'a yazılması (4.9 Kademe 0)
  - Not: `detect/merge.py`; aynı kaynaktan gelen adaylar birbiriyle birleşmez (dedektör zaten birleştiriyor). Tepe noktası öncelik sırası reaktif > içerik > izleyici klibi. `features`: z/x/baseline, `sources`, `events`, `event_filter`, `viewer_clips`, `category`, `hour`, `top_messages`. İzleyici sayısı VOD'da zaman serisi olarak yok (Kick sadece tek bir `viewer_count` veriyor), canlıda eklenecek.
- [x] Testler: sentetik chat'te 3 spike'ın 3'ü de bulunuyor, düz bölgede yanlış pozitif yok, cooldown ve birleştirme çalışıyor, tek kullanıcı spam'i spike sayılmıyor

**Bitti sayılması için:** Gerçek VOD'da `report` çıktısı makul adaylar gösteriyor. Kullanıcı 5–10 adayı izleyip "mantıklı" diyor.

### Faz 3 — Kesim + Transkript + LLM
- [x] `cutter.py` (segment aralığından ham kesit + `raw.wav` 16 kHz mono)
- [x] `whisper.py` (Bölüm 4.4), gpu_pool işçisi, model önbelleği
  - Not: model `data/models/` altına iner, tek iş parçacıklı executor'da çalışır. `faster-whisper` normal bağımlılık (Bölüm 1'de zaten var).
- [x] `llm/client.py` (retry, prompt caching, maliyet kaydı, bütçe koruması), `prompts.py`, `schemas.py`
  - Sapma: Sonnet 5.5 / Opus 5.5'te zorunlu `tool_choice` (any/tool) 400 döndürdüğü için yapılandırılmış çıktı (`output_config.format` json_schema) kullanılıyor; şema pydantic'ten üretilir, cevap pydantic ile yeniden doğrulanır, geçersizse bir kez düzeltme isteğiyle tekrar sorulur.
  - Sapma: Opus 5.5'te thinking kapatılamadığı için hız/maliyet `effort` ile ayarlanıyor (`llm.prefilter_effort: low`, `llm.judge_effort: medium`).
  - Ek: Sonnet/Opus çağrılarında sunucu taraflı ret yedeği (beta `server-side-fallback-2026-07-01`, `llm.refusal_fallback`). Ret gelirse aday `rejected` olur, tekrar denenmez.
  - Fiyatlar docs'tan doğrulandı (2026-11, $/1M): Haiku 5.5 0.10/0.50, Sonnet 5.5 2/10 (cache okuma 0.10), Opus 5.5 4/20 (cache okuma 0.20). Haiku cache okuma 0.01 varsayıldı (girdinin %10'u). Cache yazma girdinin 1.25 katı.
- [x] prefilter ve judge işleri, saatlik aday sınırı, kelime sınırına snap
  - Not: işler `clipper work` ile kuyruk boşalana kadar çalışır (`clipper run` Faz 5/6'da). Sonuçlar `clipper clips <session_id> [--all]` ile görülür.
  - Not: saatlik sınır için judge işleri prefilter puanına göre sıralanır (yüksek puan önce), saat dilimi dolunca kalanlar "saatlik aday sınırı doldu" gerekçesiyle reddedilir. Son başlıklar, yayınlama gelene kadar yayıncının daha önce judge'dan geçmiş klip başlıklarından alınıyor.
- [x] (r3) **İçerik yolu toplu modu**: VOD'un tam transkripti çıkarılır (faster-whisper large-v3), ContentScanner aynı mantıkla 60 sn adımla tüm VOD'u tarar. Canlıdaki kodla **aynı sınıf**, sadece zaman kaynağı farklı.
  - Not: `clipper vod` reaktif dedektörden sonra `session_transcribe` (gpu, öncelik 300: klip transkriptlerinden sonra) işini kuyruğa atar; bitince `content_scan` (io) gelir. İkisi de `clipper work` ile çalışır. Transkript `data/sessions/<id>/live_transcript.jsonl` (`content/transcript.py`). Tarayıcı `content/scanner.py` (`ContentScanner`, `ContentAccumulator`), toplu sürücü `content/batch.py`. İçerik adayı mevcut bir adayla ±`merge.radius_seconds` çakışırsa o adaya `content` kaynağı eklenir, yoksa yeni aday + `cut` işi.
  - Sapma: Haiku çıktısı tool use yerine yapılandırılmış çıktı ile alınıyor (Faz 3'teki gerekçeyle aynı).
  - Bekleyen: klip `transcribe` işinin hazır tam transkripti kullanıp sadece kenarları yeniden yazması (4.3b "Kazanç") şimdilik yok; klip transkripti eskisi gibi raw.wav'dan çıkarılıyor.
- [x] (r3) Konuşma hızı ve anahtar laf sinyalleri (4.3b) dedektöre beslenir
  - Not: dedektöre iki yeni özellik: `speech` (kelime/sn) ve `vkw` (yayıncının ağzından çıkan şok/gülme kelimeleri), ağırlıklar 0.10 / 0.10. Transkript olmayan saniyelerde z=0 ve baseline güncellenmez. VOD'da transkript `clipper work` ile sonradan geldiği için etkisi `clipper detect --force <id>` ile görülür (adaylar henüz işlenmediyse).
- [x] (r3) Judge prompt'una few-shot geri bildirim bloğu (4.9 Kademe 1). Veri yoksa blok boş geçilir.
  - Not: judge'a ayrıca adayın kaynakları, içerik yolunun `kind`/`reason`'ı, izleyici klipleri ve penceredeki platform olayları gidiyor (4.5 r3). `sources` ≥ 2 ise prefilter atlanıyor.
- [x] (r3) Testler: sessiz pencere atlanıyor, art arda iki yüksek skorlu pencere birleşiyor, 3 dk sınırı uygulanıyor
- [x] Testler: LLM istemcisi `httpx.MockTransport` ile (SDK'ya custom http_client verilerek), geçersiz JSON → tekrar deneme, bütçe aşımı → Haiku'ya düşme
  - Not: anthropic SDK 2.x `httpx2` üzerine kurulu, testler `httpx2.MockTransport` + `anthropic.DefaultAsyncHttpxClient(transport=...)` kullanıyor.
  - Bekleyen: "Bitti" maddesi için gerçek bir VOD'da `ANTHROPIC_API_KEY` ile `clipper work` çalıştırılması ve `llm_usage` maliyetinin ölçülmesi.

**Bitti sayılması için:** VOD session'ında adaylar transkriptlenip judge'dan geçiyor. `clips` tablosunda başlık/metinler dolu. Saatlik maliyet `llm_usage`'te görülüyor (hedef Sonnet ile < $0.25/yayın saati).

### Faz 4 — Render
- [x] `tr_text.py` (`tr_upper`, satır kırma), `ass_captions.py`, `layouts.py`, `render/ffmpeg.py` (komut oluşturucu + çalıştırıcı + ilerleme parse)
  - Not: üst seviye `render/clip.py` (`render_clip`). `render` işi (cpu) judge'da tutulan klipler için otomatik kuyruğa girer, aday durumu `rendered` olur; `clips.final_path` / `thumb_path` yazılır. Render klip klasöründe (cwd) çalışır, `clip.ass` ve `fonts/` göreli verilir (Windows yol kaçışı sorunu yok). `stack` için facecam yoksa `blur`'a düşer. Oyun paneli ve `center` kırpması dar kaynakta taşmasın diye `min(iw, ...)` ile korunuyor.
  - Not: font repoya konmadı (binary); `assets/fonts/README.md` indirme adımlarını anlatıyor, `paths.fonts_dir`, `doctor` kontrolü var. Font yoksa libass sistem fontunu kullanır.
- [x] Encoder otomatik seçimi, thumbnail
  - Not: `auto` → `doctor`'daki gibi küçük bir deneme encode'u ile h264_nvenc gerçekten çalışıyorsa NVENC, yoksa libx264. Thumbnail klip aralığındaki en yüksek ses enerjili saniyeden.
- [x] `facecam_vision.py` + `clipper facecam <streamer>` (öneri üretir)
  - Not: yayıncının son session videosundan 3 kare, her biri için judge modeline (görsel) tek çağrı; bulunan kutuların medyanı öneri olur. Önizleme `data/facecam/<yayıncı>/preview.jpg` (kırmızı kutu). `--apply` öneriyi `streamers.facecam_rect`'e yazar ve düzeni `stack` yapar.
- [x] `clipper test-render --layout stack|blur|center` fixture'dan örnek çıktı üretir
  - Not: ffmpeg `testsrc2` + sahte facecam kutusu + ton ile örnek video üretir, çıktılar `data/test-render/<düzen>/final.mp4`.
- [x] Testler: ffprobe ile çıktı 1080x1920, süre ±0.2 sn, ses var, -14 LUFS ±1.5. ASS dosyası beklenen satır sayısında. `tr_upper` testleri.

**Bitti sayılması için:** Gerçek VOD'dan 5 klip üç düzende render edilip telefonda izlendiğinde altyazı senkronu ve kadraj düzgün.

### Faz 5 — Panel
- [x] FastAPI uygulaması, statik dosyalar, güvenli `/media`
  - Not: HTTP katmanı `panel/app.py`, veri/işlemler `panel/api.py` (HTTP'siz, test edilebilir). `/media` yolu `data_dir` içine `resolve()` + `is_relative_to` ile hapsedilir, dışarısı 404. `PANEL_PASSWORD` tanımlıysa her istek HTTP Basic ister; `panel.host` loopback değilse ve parola yoksa `clipper run` başlamaz. Endpoint'ler async ve workerlarla aynı event loop'ta, tek sqlite bağlantısını paylaşır. `/api/auth/{platform}/start` ve yayın "tekrar dene" Faz 7'de (şimdilik 501).
- [x] Bölüm 4.8'deki 6 sekme. Klip düzenleme + yeniden render. Facecam dikdörtgen seçici (canvas).
  - Not: Canlı sekmesi şimdilik session seçip sinyal grafiğini (SVG, aday işaretleri kaynağa göre renkli, altında içerik tarama skorları) gösteriyor; canlı durum Faz 6'da. Kırpma/hook değişince kaydetmek render'ı öncelik 5 ile kuyruğa koyar; sadece başlık/hashtag değişirse render yok. Facecam: "Kare getir" son kayıttan kare alır, kullanıcı kutuyu sürükleyerek çizer; API anahtarı varsa "Claude ile öner" de çıkar.
- [x] `clipper run` paneli de başlatır. Tarayıcıyı otomatik açar.
  - Not: `runner.py`: uvicorn + `worker.run_forever` aynı süreçte; Ctrl+C önce paneli sonra workerları temiz kapatır (uvicorn'un sinyali yeniden fırlatması kapatıldı). `--no-browser`. API anahtarı yoksa io havuzu (LLM) çalışmaz, işler kuyrukta bekler. LiveWatcher Faz 6'da eklenecek.
- [x] (r3) Reddet nedeni seçimi, `edited` kaydı (kırpma/başlık farkları), `feedback` yazımı (4.8, 4.9 Kademe 0)
  - Not: `feedback.py`. İlk düzenlemede LLM'in orijinal start/end/başlığı `llm_json.original`'a yazılır; onayda fark varsa `edited` + `start_delta`/`end_delta`/`title_edited`. Her kararda `features_snapshot` (aday özellikleri + prefilter/judge skorları) donar.
- [x] (r3) Klip kartında adayın kaynakları rozet olarak görünsün (`chat`, `içerik`, `izleyici klibi`). İçerik yolunun `kind`/`reason`'ı da görünsün.
- [x] (r3) "Reddedilenler" görünümü: LLM'in elediği adaylar da listelensin. Kullanıcı "bu aslında iyiydi" diyerek kurtarabilsin (kurtarılanlar en değerli eğitim verisi, `feedback.decision=approved`, `reason=rescued`).
  - Not: Kartta hangi aşamada elendiği (prefilter/judge) ve ham kesit videosu görünür. Kurtarılan aday judge kırpması yoksa ham kesitin ilk `render.max_len` saniyesiyle render edilir.

**Bitti sayılması için:** VOD'dan gelen klipler panelde izlenip onaylanabiliyor, başlık düzenlenip yeniden render alınabiliyor.

### Faz 6 — Canlı hat
- [ ] Anonim uç noktaların canlı doğrulaması: Twitch GQL (stream, clips) ve YouTube innertube (live_chat) gerçekte çalışıyor mu? Sonucu Bölüm 4.1'e not düş. Çalışmayan için persisted query / resmi API yedeğini devreye al.
  - Not (2026-11-18): Bulut geliştirme ortamı bu sitelere erişemiyor; masaüstünde deneme betiği (`faz6_probe.py`) oradaki güvenlik kontrolüne takıldı. Doğrulama gerçek test aşamasına kaldı. Kod ROADMAP'teki biçimlere göre yazıldı, alan/olay adları config'te. Kick'te `ChatMessageEvent` dışındaki olay adları (Subscription, GiftedSubscriptions, StreamHost) doğrulanmadı; bilinmeyen olaylar bir kez loglanır.
- [x] `platforms/*/live.py` (canlı mı kontrolü; Twitch: anonim GQL, Kick: kanal API, YouTube: /live sayfası), `LiveWatcher`
  - Not: Twitch kanalları tek GQL isteğinde (`users(logins:)`) sorulur. YouTube canlı durumu `innertube.live_status`. Takipçi `live/watcher.py`, platform bağları `live/sources.py`. Aynı anda en çok `monitoring.max_concurrent_sessions` (5) session. Yayın `offline_grace_seconds` (120 sn) kapalı kalırsa session kapanır.
- [x] `hls_recorder.py` + Twitch/Kick/YouTube HLS çözümleyiciler (Bölüm 4.2), reklam segment filtresi, token yenileme
  - Not: `recorder/hls_live.py`. Zaman tabanı VOD ile aynı: session zamanı = duvar saati − session açılışı (`session.json` `t0`), böylece kesici/dedektör/transkript kodu ortak. fMP4 playlist'lerde init bölümü her parçanın önüne eklenir (her dosya tek başına çözülebilir). Reklam segmentleri indirilmez, satırı `is_ad=1, deleted=1` olarak tutulur. 403/404/410'da çözümleyici tekrar çağrılır (yeni token).
- [x] Chat istemcileri: `twitch/chat_irc.py` (anonim), `kick/chat_pusher.py` (anonim), `youtube/innertube.py` (anonim) + `youtube/chat_official.py` (yedek). Hepsinde otomatik yeniden bağlanma, kopma süresince "chat yok" işaretlenir ve dedektör o aralıkta tetiklemez)
  - Not: Ortak yeniden bağlanma `live/chat.py` (1, 2, 4… 60 sn, jitter). `youtube/chat_official.py` (Data API yedeği) yazılmadı: API anahtarı ve kota gerektiriyor, innertube çalışmazsa eklenecek.
- [x] Dedektörün akış modu + canlı ses analizi (her yeni segmentte)
  - Not: Dedektör gerçek zamanın `feed_delay` (8 sn) gerisinden besleniyor (chat gecikmesi). Ses segmentle geç gelir; `Detector.add_audio` ile eklenir, skora zaten `lag_seconds` geriden girer. Kesim işi aday penceresinin sonu + 20 sn'ye ertelenir (`run_after`).
- [x] (r3) **LiveTranscriber** + **ContentScanner** canlı görevleri (4.3b), gpu_pool öncelik kuralı, gerideyse parça atlama
  - Not: `live/content.py`. Kuyrukta `transcribe` işi varken canlı parça bekler. Canlı transkriptten gelen konuşma hızı/anahtar laf sinyali canlı dedektöre beslenmiyor (transkript dedektörden ~30 sn geride); VOD modunda besleniyor.
- [x] (r3) Canlı izleyici klibi takibi (anonim GQL, 60 sn) ve canlı olay toplama (IRC USERNOTICE, Kick event'leri, YouTube paid/membership)
  - Not: Gizemli hediye paketi tek olay sayılır, ardından gelen tek tek `subgift` bildirimleri (community-gift-id'li) atlanır. Canlı klipte olay zamanı `createdAt − durationSeconds`.
- [x] (r3) Panel "Canlı" sekmesinde üç yolun adayları ayrı renkte, içerik taramalarının skor çizgisi sinyal grafiğinin altında
  - Not: Ayrıca kaydedilen canlı yayınlar kart olarak ve "Şimdi oldu" butonu (kalibrasyon işareti).
- [x] Halka tampon temizliği, bekleyen kesimleri koruma
- [x] Session kapanışı (yayın bitti → 2 dk bekle → kaydı kapat, kalan işleri bitir)
  - Not: Kapanışta açık tespit flush edilir, canlı chat dosyası `chat.jsonl.gz`'ye sıkıştırılır. Kuyruktaki işler workerlarda devam eder.
- [x] `clipper calibrate <streamer>`: canlı yayında panelden "şimdi oldu" butonuna basılan anlar ile chat peak'leri arasındaki farkın medyanı → `lag_seconds` önerisi
  - Not: İşaretler `live_events` (`kind=mark`). En az 3 eşleşme gerekir. `--apply` yayıncının `detector_overrides.lag_seconds`'ına yazar.
- [x] Testler: kaydedici için yerel sahte HLS sunucusu (FastAPI test app, fixture segmentleriyle). Chat istemcileri için sahte WS sunucusu (`websockets.serve`).
  - Not: HLS için `httpx.MockTransport` (ffmpeg'den üretilen gerçek TS segmentleri, kayıt sonrası kesim ve ses analizi dahil). IRC ve Pusher için `websockets.serve`.

**Bitti sayılması için:** Bir Twitch ve bir Kick yayını 2 saat boyunca kesintisiz kaydediliyor. Adaylar yayın sırasında oluşuyor, yayın bitmeden klipler panelde görünüyor. Bellek/disk sabit kalıyor.

### Faz 7 — Yayınlama
- [ ] `oauth.py` + panel "Hesap bağla" akışı
- [ ] Sıra: **YouTube → Instagram → X → TikTok (inbox)**. Her biri için upload, status, hata eşlemesi.
- [ ] `scheduler.py` (aralık, günlük limit, izin kontrolü: `permission_ok=0` ise asla yayınlama)
- [ ] Auto-publish kuralı: `auto_publish=1` **ve** `llm_score ≥ auto_publish_min_score` **ve** flag'ler temiz (`music_risk`, `nsfw`, `sensitive_topic` false) → otomatik onay
- [ ] Testler: her publisher için `httpx.MockTransport` ile tam akış (chunk'lı upload, kopma sonrası devam, 429 backoff, token yenileme)

**Bitti sayılması için:** Panelden onaylanan bir klip test hesaplarında YouTube'a (unlisted), Instagram'a, X'e yayınlanıyor, TikTok inbox'a düşüyor. Linkler panelde görünüyor.

### Faz 8 — Sağlamlaştırma ve kalibrasyon
- [ ] Çoklu yayıncı eşzamanlılığı (aynı anda 3–5 kanal). Kaynak limitleri (aynı anda max N render).
- [ ] Saklama politikası: segmentler 15 dk, ham kesitler 7 gün, final klipler 30 gün (config). Disk %85 dolunca agresif temizlik + uyarı.
- [ ] (r3) **Öğrenilmiş skor modeli** (4.9 Kademe 2): `clipper learn`, numpy logistic regression, global + yayıncı bazlı, zaman bazlı test raporu, panelde "öneriyi uygula / geri al", versiyonlu ağırlıklar. ≥200 karar birikmeden komut "yetersiz veri" der ve çıkar.
- [ ] (r3) **Performans sinyali** (4.9 Kademe 3, opsiyonel): `clip_stats` günlük çekim, izlenme hedefli ikinci model, panelde "hangi sinyal izlenme getiriyor" raporu
- [ ] (r3, v2 opsiyonel) **Judge'a görüntü kareleri**: kesit içinden en yüksek skorlu 4–6 an (ses/chat tepe noktaları) 512 px genişlikte JPEG olarak Sonnet 5.5'e eklenir. Kategori bazlı aç/kapa (`llm.vision_categories`; oyun kategorilerinde açık, "Just Chatting"/"Sohbet"te kapalı). Ölçüt: açık/kapalı A/B ile onay oranı farkına bak, fark yoksa kapat.
- [ ] (r3, v2 opsiyonel) **Bağlam özeti**: Haiku 5.5 her 5 dk'da son 5 dk transkript + önceki özetle "yayında şu ana kadar ne oldu" özetini (≤ 150 kelime) günceller. Özet judge ve ContentScanner prompt'larına eklenir. Maliyet ~$0.003/saat.
- [ ] Başlangıçta otomatik çalışma: Windows'ta Task Scheduler kaydı oluşturan `clipper install-autostart`, Linux'ta systemd unit dosyası üreten komut
- [ ] `README.md` (kurulum, `.env`, hesap bağlama adımları, sık hatalar)

**Bitti sayılması için:** Sistem 7 gün kesintisiz çalışıyor, elle müdahale sadece onay/red.

---

## 6. Bilinen kırılgan noktalar (önceden yönetilecek)

| Nokta | Risk | Önlem |
|---|---|---|
| Twitch GQL (canlı durum, klipler, PlaybackAccessToken, VOD comments) | Resmi değil, hash/Client-ID değişebilir, integrity isteyebilir | `platforms/twitch/` içinde izole, config'ten parametre, net hata mesajı, entegrasyon testi `clipper doctor --online`, Helix yedeği |
| YouTube innertube (canlı chat, replay) | Resmi değil, JSON yolları ve consent sayfası değişebilir | `platforms/youtube/innertube.py` izole, parse hatası alarmı, Data API yedeği |
| Kick API + Pusher | Resmi değil, Cloudflare | `curl_cffi`, key config'te, bağlantı testi |
| YouTube `/live` sayfası parse | HTML değişebilir | yt-dlp yedeği |
| Chat ↔ video zaman hizası | Kayma = yanlış kesit | PDT tabanlı hiza, `lag_seconds` kalibrasyonu, LLM'in kesin kırpması |
| Twitch reklamları | Reklam klibe girer | DATERANGE/EXTINF filtresi |
| Whisper Türkçe hataları | Yanlış altyazı | `caption_fixes`, panelde altyazı düzenleme (ASS yeniden üret) |
| TikTok audit | Direct post yok | Inbox modu varsayılan |
| Google OAuth test modu | Token 7 günde düşer | Production'a al / panelde "yeniden bağla" uyarısı |
| Platform politikaları ("reused/inauthentic content") | Monetizasyon ve erişim | Hook + altyazı + kırpma ile editlenmiş içerik, aralıklı paylaşım, günlük limit |

---

## 7. Maliyet (tahmini, aylık)

| Kalem | Tahmin |
|---|---|
| Claude (Haiku ön eleme + Sonnet judge, ~150 yayın saati) | ~$15–35 |
| Claude içerik yolu (Haiku 5.5, ~$0.01/saat × 150 saat) (r3) | ~$1.5 |
| (v2) Judge'a görüntü kareleri | +~$5–10 |
| X API (pay-per-use, ~200 gönderi) | ~$3 |
| YouTube / Instagram / TikTok API | $0 |
| Elektrik/donanım | mevcut PC |
| **Toplam** | **~$20–40** |

---

## 8. Kabul testi (tüm sistem)

1. `clipper doctor` tamamen yeşil.
2. İzinli 1 Twitch + 1 Kick yayıncısı ekleniyor, facecam rect onaylanıyor.
3. Yayın başlayınca kayıt + chat otomatik başlıyor.
4. 2 saatlik yayından ≥5 klip panelde hazır, her biri 12–59 sn, altyazılı, dikey.
5. 3 klip onaylanıyor → planlanan saatlerde YouTube, Instagram ve X'te yayında, TikTok inbox'ta.
6. Sistem yeniden başlatılınca yarım işler kaldığı yerden devam ediyor.
7. LLM maliyeti panelde görünüyor ve bütçe altında.
