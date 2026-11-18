# TESTPLAN — clipper kapsamlı test planı

> Proje bittikten sonra (ROADMAP Faz 0–8 tamamlanınca) sistemi uçtan uca doğrulamak için.
> Claude Code: bu dosyadaki test altyapısını kur, testleri sırayla çalıştır, sonuçları `eval/reports/` altına yaz, başarısızları düzelt ve tekrar koş. Her test ID'sinin durumunu bu dosyada `[x]` / `[!]` (başarısız, not ile) olarak işaretle.
> İnsan gerektiren adımlar **[İNSAN]** etiketli. Claude Code bunları kullanıcıdan istesin, beklerken diğer testlere devam etsin.

---

## 0. Test seviyeleri ve çalıştırma sırası

| # | Seviye | Komut | İnternet | Maliyet | Süre |
|---|---|---|---|---|---|
| 1 | Birim testleri | `uv run pytest -m "not online and not slow"` | Hayır | $0 | dakikalar |
| 2 | Entegrasyon (sahte sunucular) | `uv run pytest -m integration` | Hayır | $0 | dakikalar |
| 3 | Sentetik uçtan uca (offline) | `uv run clipper eval synthetic` | Hayır | $0 (LLM replay) | ~15 dk |
| 4 | Çevrimiçi duman testleri | `uv run pytest -m online` | Evet | ~$0.05 | ~10 dk |
| 5 | Altın set kalite değerlendirmesi | `uv run clipper eval golden` | Evet (ilk koşu) | ~$3–6 (ilk koşu), sonra $0 | saatler |
| 6 | Ablasyon (hangi sinyal ne katıyor) | `uv run clipper eval golden --ablation` | Hayır (replay) | ~$0 | ~1 saat |
| 7 | Canlı yayın testi | `uv run clipper eval live --hours 2` | Evet | ~$0.5 | 2–3 saat |
| 8 | Dayanıklılık (soak) | `uv run clipper eval soak --hours 24` | Evet | ~$3 | 24 saat |
| 9 | Kaos / hata enjeksiyonu | `uv run clipper eval chaos` | Kısmen | ~$0.2 | ~1 saat |
| 10 | Yayınlama testleri | `uv run clipper eval publish --sandbox` | Evet | ~$0.05 (X) | ~30 dk |
| 11 | Güvenlik kontrolleri | `uv run pytest -m security` | Hayır | $0 | dakikalar |
| 12 | Manuel kabul [İNSAN] | Bölüm 9 kontrol listesi | Evet | — | ~1 saat |

**Kural:** Bir seviye kırmızıyken bir sonrakine geçme (1→2→3 zorunlu sıra; 4'ten sonrası paralel olabilir).

---

## 1. Kurulacak test altyapısı (Claude Code yapar)

- [x] **LLM kayıt/oynatma (record/replay):** `config.llm.cache_mode: off | record | replay`. İstek gövdesinin (model + mesajlar + tool şeması) SHA-256'sı anahtar; cevap `eval/llm_cache/<hash>.json`. `replay` modunda cache'te olmayan istek → test hatası (sessizce gerçek API'ye gitmesin). Böylece eval'ler tekrar tekrar ücretsiz koşar.
  > Not: `llm.cache_dir` (varsayılan `./eval/llm_cache`). Replay'deki cevaplar `llm_usage`'e token'larıyla ama `usd=0` ile yazılır. Cache miss `LLMCacheMiss` fırlatır (LLMError değil, pipeline yutmaz).
- [x] **Whisper önbelleği:** Aynı ses dosyasının (hash) transkripti `eval/whisper_cache/`'te tutulur. `--no-whisper-cache` ile kapatılır.
  > Not: `transcribe.cache_dir` ayarlıysa açık (anahtar: model + dil + beam + WAV SHA-256). `--no-whisper-cache` bayrağı eval komutlarıyla gelecek.
- [x] **Sahte transkript enjeksiyonu:** Sentetik senaryolarda Türkçe konuşma üretmek zor. Fixture'lar `fake_transcript.jsonl` sağlayabilir. `transcribe.backend: fake` olunca whisper yerine bu okunur.
  > Not: `transcribe.fake_path`; satırlar `{"start","end","text"}` oturum zamanında (PDT). Kelime zamanları eşit aralıklı üretilir.
- [x] **Sahte platform sunucuları** (`tests/fakes/`), hepsi yerel ve ağsız:
  - `fake_twitch.py`: IRC-WS (PRIVMSG, USERNOTICE sub/subgift/raid, PING, kopma), GQL (stream, clips, PlaybackAccessToken), usher HLS (master + media playlist, PDT, reklam DATERANGE'i)
  - `fake_kick.py`: kanal API (Cloudflare 403 simülasyonu dahil), Pusher WS (çift JSON-encoded event)
  - `fake_youtube.py`: `/live` sayfası, `live_chat` HTML + `get_live_chat` continuation zinciri, replay
  - `fake_publishers.py`: YouTube resumable upload, IG container/rupload/publish, X media upload v2 + tweets, TikTok inbox init/PUT/status. Hepsinde 429, 5xx, token süresi dolması ve yarıda kopma modları.
  > Not: `tests/fakes/` (fake_twitch: IRC-WS + GQL + HLS; fake_kick: API + Pusher; fake_youtube: /live + live_chat zinciri; faults: 429/5xx/401/403/kopma enjeksiyonu). Yayıncılar için mevcut `tests/test_publish.py` sahteleri `faults.Faulty` ile sarılıyor. Testler: `tests/test_integration.py` (`-m integration`). Bulunan hatalar: YouTube yükleme 429'u kalıcı hata sayıp yüklemeyi bırakıyordu (artık Retry-After ile tekrar); YouTube chat yapısı değişince sessizce 0 mesaj okunuyordu (artık 3 sayfa üst üste tanınmayan öğe → anlaşılır hata).
- [x] **Senaryo üretici** `tests/fixtures/scenarios.py`: Bölüm 2.1'deki her senaryo için video + chat + olaylar + (gerekiyorsa) sahte transkript + **beklenen sonuç dosyası** (`expected.json`) üretir.
  > Not: `clipper.evals.scenarios` (testler `tests/fixtures/scenarios.py` üzerinden kullanır). Dedektör testleri için sadece ses segmentleri (8 kHz WAV), tam pipeline için `video=True` ile 1080p mpegts segmentleri. Sapmalar: S02'de ses olay anında, chat tepkisi `lag_seconds` (20 sn) sonra zirve yapıyor (DET-20 ile tutarlı olsun diye; plandaki "2 sn önce" varsayılan lag ile çelişiyordu). S17/S18 6 dk (3 dk'lık `warmup_seconds` yüzünden). S14/S15 de 6 dk; içerik anı 270. sn'de (tam pipeline'da klip üretmeleri için). S12 VOD modunda kopma bilgisi olmadığından sadece "mesaj yok" olarak modellendi.
- [x] **Etiketleme modu (panel):** `clipper label <session_id>` paneli etiketleme ekranında açar. Video oynatıcı + zaman çizelgesi. Kullanıcı "iyi an" işaretler: başlangıç/bitiş, tür (`komik / şok / hikâye / atışma / oyun anı / diğer`), kalite (1 = idare eder, 2 = iyi, 3 = kesin klip). Çıktı `eval/labels/<session>.json`. Sistemin aday çıktıları bu ekranda **gizli** olmalı (etiketleyen etkilenmesin).
  > Not: `clipper label <session> [--labeler ad]` ayrı bir küçük sunucu açar (panel portu + 1); sadece `/api/label` ve 540p önizleme videosu var, aday/klip/sinyal uç noktası hiç yok. Etiket zamanları session saatinde (`candidates.peak_ts` ile aynı), izlenen video konumu da saklanır. İkinci kişi `eval/labels/<session>_<ad>.json`'a yazar; uyum `clipper.evals.labels.agreement`.
- [x] **Eval rapor üretici:** her `clipper eval ...` koşusu `eval/reports/<tarih>_<tür>.html` + `.json` üretir. Metrik tabloları, başarısız test listesi, sinyal grafikleri, örnek klip linkleri, maliyet.
  > Not: `clipper.evals.report` temel sürüm: özet + senaryo/kontrol tablosu + başarısız liste (JSON + HTML). Sinyal grafikleri ve maliyet sütunu henüz yok.
- [x] **Kaynak izleyici:** eval sırasında her 10 sn: RSS bellek, CPU %, GPU VRAM (varsa `nvidia-smi --query-gpu`), disk kullanımı, açık dosya sayısı, kuyruk uzunlukları → `metrics.csv`.
  > Not: `clipper.evals.monitor.ResourceMonitor` (sadece standart kütüphane, psutil yok). `clipper eval synthetic` koşusunda `<out>/metrics.csv`; zirve değerler rapor özetine `kaynak_*` olarak yazılır. CPU % = bu süreç + biten alt süreçler (ffmpeg dahil).

---

## 2. Test verisi

### 2.1 Sentetik senaryolar (otomatik üretilir, beklenen sonuç bilinir)

Her senaryo 1920x1080 30 fps test videosu (ffmpeg `testsrc2` + üstüne saniye sayacı `drawtext`), ses (arka plan `anoisesrc` + belirli anlarda `sine` patlamaları) ve sentetik chat üretir. Baseline trafik: Poisson, 1.5 mesaj/sn, 120 tekil kullanıcı.

| ID | Senaryo | Süre | Beklenen |
|---|---|---|---|
| S01 | Düz trafik, hiçbir olay yok | 20 dk | **0 aday** |
| S02 | 3 net chat patlaması (6×, KEKW ağırlıklı) t=5,10,15 dk + 2 sn önce ses patlaması | 20 dk | 3 aday; olay zamanı hatası ≤ 3 sn |
| S03 | Tek kullanıcı spam (1 kişi 40 mesaj/10 sn) | 10 dk | 0 aday |
| S04 | Sub yağmuru (5 sn'de 15 subgift) + ardından "teşekkürler/hoş geldin" chat dalgası | 10 dk | 0 aday (bastırma aktif) |
| S05 | Raid (USERNOTICE raid, 400 yeni kullanıcı, 60 sn "raid" spam) | 10 dk | 0 aday; sonra baseline 5 dk'da yeni seviyeye otursun |
| S06 | Sakin chat, ama sahte transkriptte 70 sn'lik komik hikâye (t=6 dk) | 12 dk | 1 aday, `sources=["content"]` |
| S07 | İzleyici klibi olayı (GQL clips) t=8 dk, chat hafif artış (2×) | 12 dk | 1 aday, `sources` içinde `viewer_clip` |
| S08 | Aynı anda chat patlaması + içerik skoru + izleyici klibi | 10 dk | **Tek** birleşik aday, 3 kaynak; prefilter atlanmış |
| S09 | Küçük kanal: baseline 0.1 mesaj/sn, patlama 1.2 mesaj/sn | 20 dk | 1 aday (floor + oran kuralı çalışıyor) |
| S10 | Dev kanal: baseline 40 mesaj/sn, patlama 120 mesaj/sn | 10 dk | 1 aday; işlem gecikmesi < 1 sn/sn |
| S11 | Art arda iki patlama 20 sn arayla | 5 dk | 1 birleşik aday (cooldown / birleştirme) |
| S12 | Chat kopması 3 dk (bağlantı yok) + bu sırada ses patlaması | 10 dk | Kopma aralığında reaktif tetik yok; içerik yolu çalışmaya devam |
| S13 | Twitch reklam segmentleri (2 dk stitched ad) adayın ortasında | 10 dk | Kesitte reklam karesi yok (renk/sayaç kontrolü) |
| S14 | Türkçe karakterli transkript ("İstanbul'da ılık şişli ÖĞÜŞ") | 2 dk | Altyazıda `İSTANBUL'DA ILIK ŞİŞLİ ÖĞÜŞ` birebir |
| S15 | Facecam olan düzen (videonun sağ üstüne ayrı renkli kutu "CAM") | 3 dk | `stack` çıktısında üst panelde CAM kutusu, altta oyun |
| S16 | Uzun hikâye: art arda 3 yüksek içerik penceresi (4 dk) | 8 dk | 1 aday, süre ≤ 3 dk sınırına kırpılmış |
| S17 | Müzik riski: sahte transkriptte şarkı sözü kalıbı + LLM replay `music_risk=true` | 3 dk | Otomatik yayın **yok**, panelde uyarı |
| S18 | `permission_ok=0` yayıncı, yüksek skorlu klip, `auto_publish=1` | 3 dk | Hiçbir publish işi oluşmaz |

Senaryo üretimi deterministik (sabit seed). `expected.json` aday sayısı, zaman aralıkları (±tolerans), kaynaklar ve yasak durumları içerir.

### 2.2 Altın set (gerçek veri, insan etiketli) [İNSAN]

İzinli yayıncılardan **en az 10 VOD**, toplam 15–20 saat. Çeşitlilik zorunlu:

| Slot | Tür | Platform | Not |
|---|---|---|---|
| G01–G02 | Sohbet / Just Chatting, büyük kanal (>5k izleyici) | Twitch | İçerik yolu için kritik |
| G03 | Sohbet, küçük kanal (<200 izleyici) | Twitch veya Kick | Düşük trafik testi |
| G04–G05 | FPS / rekabetçi oyun (Valorant, CS2 vb.) | Twitch | Ses + izleyici klibi |
| G06 | Korku / hikâyeli oyun | herhangi | Ani şok anları |
| G07 | IRL / dışarı yayını | Kick | Ses kalitesi kötü, kamera hareketli |
| G08 | YouTube canlı yayın kaydı | YouTube | innertube replay chat |
| G09 | Kick, yoğun bağış/sub yayını | Kick | Olay filtresi |
| G10 | Müzik çalan yayın | herhangi | Müzik riski bayrağı |

Etiketleme protokolü:
1. [İNSAN] `clipper label <session>` ile **tüm** VOD'u 1.5× hızda izle, her iyi anı işaretle. Hedef: saat başına 5–15 etiket.
2. Mümkünse ikinci kişi aynı 2 VOD'u bağımsız etiketlesin. İki kişinin uyumu (çakışma ≥ %50 olan etiketler / tüm etiketler) raporlanır. Bu, sistemden beklenebilecek üst sınırdır.
3. Etiketler etiketlenirken sistem çıktısı görülmez. Etiketleme bittikten sonra eval koşulur.
4. Altın set `eval/golden/` altında sabitlenir (video hash'leri + chat + labels). Sonraki sürümler hep aynı sete karşı ölçülür.

---

## 3. Birim ve entegrasyon testleri (seviye 1–2)

Mevcut testlere ek olarak şunların hepsi var olmalı ve geçmeli:

**İş kuyruğu (JOB)**
- [x] JOB-01 4 eşzamanlı işçi, 1000 iş → her iş tam 1 kez işlenir — `test_jobs::test_concurrent_claim_no_double_take (300 iş / 8 işçi)`
- [x] JOB-02 İşçi `kill -9` → `locked_until` dolunca iş tekrar alınır, sonuç tek — `test_jobs::test_crash_recovery`
- [x] JOB-03 Üstel backoff süreleri ve `max_attempts` sonrası `dead` — `test_jobs::test_fail_backoff_then_dead, test_backoff_capped`
- [x] JOB-04 Öncelik: gpu_pool'da `transcribe` (klip), canlı transkriptten önce alınır — `test_testplan_units::test_job04_clip_transcripts_go_before_live`

**Chat ayrıştırıcıları (CHAT)**
- [x] CHAT-01 Twitch IRC: tag'li PRIVMSG, emote konumları, `tmi-sent-ts`, kaçışlı tag değerleri (`\s`, `\:`), USERNOTICE türleri, bits — `test_live::test_irc_parse_message_emotes_bits, test_irc_usernotice_events`
- [x] CHAT-02 Kick Pusher: çift JSON decode, `[emote:ID:NAME]` ayrıştırma, sub/gift event'leri, bilinmeyen event'te çökmeme — `test_live::test_kick_frames, test_kick_pusher_client_with_fake_server`
- [x] CHAT-03 YouTube innertube: text + emoji run'ları, paid message, membership, continuation zinciri, `timeoutMs`, replay `videoOffsetTimeMsec` — `test_youtube_vod::test_live_message_uses_timestamp, test_chat_replay_paging`
- [x] CHAT-04 Yeniden bağlanma: sahte sunucu 3 kez bağlantıyı koparır → istemci geri gelir, kopma aralıkları `chat_gap` olarak işaretlenir — `test_live::test_reconnecting_reports_gap_and_backs_off`
- [x] CHAT-05 Kullanıcı ID'leri hash'lenmiş; ham kullanıcı adı diske yazılmıyor (dosyada grep) — `test_testplan_units::test_chat05_raw_usernames_never_written`

**Kaydedici (REC)**
- [x] REC-01 Sahte HLS: 30 dk boyunca segment kaybı yok, seq sıralı, PDT doğru — `test_integration::test_rec01_thirty_minutes_no_segment_loss`
- [x] REC-02 Reklam DATERANGE segmentleri `is_ad=1` — `test_m3u8::test_media_playlist_pdt_and_ads`
- [x] REC-03 403 (token süresi doldu) → yeni token, boşluk ≤ 1 segment — `test_live::test_recorder_takes_new_segments_skips_ads_and_refreshes`
- [x] REC-04 Halka tampon: 15 dk'dan eski segmentler silinir, bekleyen `cut` işinin kapsadıkları silinmez — `test_live::test_prune_keeps_segments_pending_cuts_need`
- [x] REC-05 Kesici: `[t0,t1]` aralığı ±0.5 sn (keyframe hassasiyeti) ile kesiliyor, segment sınırı geçişinde kare atlama yok — `test_testplan_units::test_rec05_cut_across_segment_boundary`

**Dedektör ve sinyaller (DET)**
- [x] DET-01 – DET-18: Bölüm 2.1'deki S01–S18 senaryolarının **dedektör kısmı** (render/LLM olmadan) `expected.json` ile birebir
  > Not: içerik yolu için Haiku yerine senaryonun doğruluk değerlerini veren `OracleScanner` kullanılıyor. Bulunan ve düzeltilen hatalar: (1) yoğun kanalda (S10) baseline ilk saniyelerde "sıcak" kalıp donuyordu, hiç öğrenmiyordu → ilk 30 örnek koşulsuz öğreniliyor; (2) sub sonrası teşekkür dalgası (S04) sadece emote'u bastırıyordu ve pencere gecikmesiyle ilk saniyeler kaçıyordu → teşekkür mesajları chat hızından da düşülüyor, saniye bazında da kontrol var.
- [x] DET-19 Akış modu ile toplu mod aynı veride aynı adayları üretiyor (fark = 0) — `test_detector::test_streaming_with_gaps_equals_dense`
- [x] DET-20 Ses sinyali `lag_seconds` hizası doğru (sentetik ses patlaması chat'ten 20 sn önce → olay zamanı ses anına ±2 sn) — `test_detector::test_audio_contributes_with_lag`
- [x] DET-21 Konuşma hızı z-skoru ve anahtar laf puanı sahte transkriptte beklenen değerde — `test_content::test_speech_signal_reaches_detector`

**İçerik yolu (CNT)**
- [x] CNT-01 Sessiz pencere (%70+) LLM'e gitmiyor (çağrı sayacı)
- [x] CNT-02 Pencere birleştirme ve 3 dk sınırı — `test_content::test_three_minute_cap, test_consecutive_high_windows_merge`
- [x] CNT-03 Overlap tekilleştirme: 4 sn çakışan parçalarda aynı kelime iki kez yazılmıyor — `test_live::test_dedupe_overlap_words`
- [x] CNT-04 Canlı transkript 60 sn geride kalınca eski parçalar atlanıyor, log var — `test_testplan_units::test_cnt04_live_transcript_skips_when_behind`

**LLM katmanı (LLM)**
- [x] LLM-01 Geçersiz JSON → hata mesajıyla 1 tekrar → hâlâ geçersizse aday `failed`, sistem devam — `test_llm::test_invalid_json_is_retried_once, test_invalid_twice_raises`
- [x] LLM-02 Şema dışı değerler (`start > end`, süre < `min_len`, başlık > 70 karakter) düzeltiliyor veya reddediliyor — `test_llm::test_semantic_validation_triggers_retry`
- [x] LLM-03 Bütçe aşımı → judge Haiku'ya düşüyor, panel uyarısı — `test_llm::test_budget_exceeded_downgrades_judge`
- [x] LLM-04 Prompt caching başlığı gönderiliyor; replay'de cache_read token'ları `llm_usage`'te — `test_llm::test_structured_output_request_shape, test_cost_recorded`
- [x] LLM-05 Few-shot bloğu: veri yokken boş, 20+ feedback varken doğru yayıncının örnekleri — `test_content::test_prompt_blocks_empty_when_no_data`
- [x] LLM-06 429/529 (overloaded) → backoff, işi kaybetmiyor — `test_testplan_units::test_llm06_overloaded_is_retried_and_never_swallowed`
- [x] LLM-07 Snap: start/end kelime sınırına hizalanıyor (cümle ortasında başlamıyor) — `test_llm::test_snap_to_words`

**Render (RND)**
- [x] RND-01 Üç düzen × iki encoder (nvenc varsa) → ffprobe: 1080x1920, yuv420p, h264, aac 48 kHz, faststart — `test_render::test_render_output (3 düzen, libx264) + test_testplan_units::test_rnd01_rnd06_output_format_in_awkward_dir`
- [x] RND-02 Süre hedefle ±0.2 sn — `test_render::test_render_output`
- [x] RND-03 Ses yüksekliği −14 LUFS ±1.5 (`ffmpeg -af ebur128` ile ölç) — `test_render::test_render_output`
- [x] RND-04 ASS: kelime vurgusu olay sayısı = kelime sayısı, satır ≤ 18 karakter, hook ilk 2.5 sn — `test_render::test_build_ass_lines_and_highlight, test_wrap_words`
- [x] RND-05 `tr_upper`/`tr_lower` tüm Türkçe harf çiftleri (ç ğ ı i ö ş ü, İ I) — `test_tr_text::test_tr_upper, test_tr_lower, test_casefold_mixes_i`
- [x] RND-06 Windows yol kaçışları: boşluk, `:` ve Türkçe karakter içeren klasör adıyla render başarılı — `test_testplan_units::test_rnd01_rnd06_output_format_in_awkward_dir`
- [x] RND-07 S13 reklam karesi yok, S15 facecam doğru panelde (kare örnekleyip ortalama renk kontrolü)
- [x] RND-08 Altyazı senkronu: sahte transkriptteki kelime zamanları ile ASS olay zamanları arasında fark ≤ 50 ms — `test_testplan_units::test_rnd08_caption_events_match_word_times`

**Yayınlama (PUB, sahte sunucularla)**
- [x] PUB-01 YouTube resumable: 8 MB chunk, yarıda kopma → `bytes */size` ile devam, son video bütün — `test_publish::test_youtube_chunked_upload_resumes_after_drop, test_youtube_503_then_resume_and_saved_session`
- [x] PUB-02 Instagram: container → rupload → `IN_PROGRESS`×3 → `FINISHED` → publish; `ERROR` durumunda failed + mesaj — `test_publish::test_instagram_resumable_reel, test_instagram_error_status_fails`
- [x] PUB-03 X: init/append/finalize/status, `processing_info` bekleme, tweet oluşturma, metinde link yok kontrolü — `test_publish::test_x_chunked_upload_status_and_tweet, test_x_resumes_from_saved_segment`
- [x] PUB-04 TikTok inbox: init → chunk PUT → status; direct mod config'te kapalıyken asla çağrılmıyor — `test_publish::test_tiktok_inbox_flow, test_tiktok_direct_needs_audit`
- [x] PUB-05 Token süresi doldu → refresh → işlem devam; refresh de başarısızsa panelde "yeniden bağla" — `test_publish::test_oauth_flow_stores_encrypted_and_refreshes, test_oauth_tiktok_hex_pkce_and_failed_refresh`
- [x] PUB-06 Zamanlayıcı: `min_gap_minutes` ve `daily_cap` (saat donmuş sahte zamanla test) — `test_publish::test_plan_gaps_caps_and_missing_account`
- [x] PUB-07 S17 (müzik riski) ve S18 (izin yok) için publish işi **hiç oluşmuyor**
- [x] PUB-08 Aynı klip aynı platform+hesaba iki kez yüklenmiyor (UNIQUE + idempotency) — `test_publish::test_plan_gaps_caps_and_missing_account (aynı klip+platform ikinci kez planlanmıyor) + DB UNIQUE`

**Panel (PNL)**
- [x] PNL-01 Tüm API uçları 200/4xx doğru; `/media` path traversal (`../`, mutlak yol, URL-encode) → 403 — `test_panel::test_media_is_confined_to_data_dir`
- [x] PNL-02 Onay/red/edited → `feedback` satırı + `features_snapshot` dolu — `test_panel::test_reject_with_reason`
- [x] PNL-03 "Reddedilenler"den kurtarma → `rescued` feedback — `test_panel::test_rescue_llm_rejected`
- [x] PNL-04 Facecam dikdörtgen seçimi oranlı koordinat olarak kaydediliyor — `test_panel::test_streamers_crud`
- [x] PNL-05 Etiketleme modunda sistem adayları API'den dönmüyor

---

## 4. Sentetik uçtan uca (seviye 3)

`clipper eval synthetic`: S01–S18'i **tam pipeline** ile (kayıt → tespit → kesim → sahte/gerçek transkript → LLM replay → render → sahte yayınlama) koşar.

İlk koşu `llm.cache_mode=record` ile gerçek Claude'a gider (~$0.30), sonra hep `replay`.

Geçme kriterleri:
- [x] E2E-01 Tüm senaryolarda aday sayısı ve kaynaklar `expected.json` ile aynı
- [x] E2E-02 Üretilen her klip RND-01–RND-04'ten geçiyor
- [!] E2E-03 "Pik → klip hazır" gecikmesi (GPU'lu makinede) medyan ≤ 3 dk, p95 ≤ 6 dk
- [x] E2E-04 Yasak durumlar (S17, S18) ihlal edilmiyor
- [x] E2E-05 Koşu sonunda `jobs` tablosunda `running`/`queued` iş kalmıyor, `dead` iş yok
  > Not (2026-11-18): `uv run clipper eval synthetic --llm oracle` ile 18/18 geçti (LLM yerine senaryo doğrusunu bilen sahte Claude). Gerçek Claude ile `--llm record` koşusu kullanıcının makinesinde ANTHROPIC_API_KEY girildikten sonra yapılacak. E2E-03: VOD modunda pik→klip gecikmesi ölçülmüyor, canlı/GPU testinde (Seviye 6) ölçülecek.

---

## 5. Çevrimiçi duman testleri (seviye 4)

Gerçek platformlara **salt okunur** bağlantılar. Gerçek yayıncı adı config'ten gelir (`eval.online.channels`). [İNSAN] O an canlı olan izinli 1 Twitch, 1 Kick, 1 YouTube kanalı ver.

- [ ] ONL-01 Twitch anonim IRC: 60 sn'de ≥1 mesaj (aktif kanalda)
- [ ] ONL-02 Twitch anonim GQL: `stream` ve `clips` sorguları çalışıyor. Çalışmıyorsa hangi alan reddedildi raporla, persisted/Helix yedeği devreye giriyor mu kontrol et.
- [ ] ONL-03 Twitch HLS: PlaybackAccessToken → 60 sn kayıt, PDT var
- [ ] ONL-04 Kick: `curl_cffi` ile kanal API 200, Pusher'a abone olup 60 sn'de mesaj, playback_url ile 60 sn kayıt
- [ ] ONL-05 YouTube: `/live` → videoId, innertube `get_live_chat` 60 sn'de mesaj, yt-dlp ile 60 sn kayıt
- [ ] ONL-06 YouTube replay: bitmiş bir yayının ilk 10 dk chat'i çekiliyor
- [ ] ONL-07 Twitch VOD + VOD chat: 5 dk'lık parça indiriliyor
- [ ] ONL-08 Claude: üç model adıyla (`claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`) birer küçük çağrı, tool-use JSON dönüyor
- [ ] ONL-09 `clipper doctor --online` tamamen yeşil

Başarısız olan anonim uç nokta varsa: ROADMAP Bölüm 4.1'e tarihli not düş, yedeği doğrula.

---

## 6. Altın set kalite değerlendirmesi (seviye 5–6)

`clipper eval golden`: Altın setteki her VOD'u VOD modunda tam pipeline'dan geçirir (render dahil, yayınlama hariç) ve insan etiketleriyle karşılaştırır.

**Eşleşme kuralı:** Sistem adayı ile etiket, zaman aralıkları ≥ %40 örtüşüyorsa (IoU değil, kısa olanın uzunluğuna göre örtüşme) eşleşmiş sayılır.

**Metrikler** (VOD başına, tür başına, toplam):

| Metrik | Tanım | Hedef (v1) |
|---|---|---|
| Recall@kalite3 | "Kesin klip" etiketlerinin kaçı aday olarak bulundu (LLM öncesi) | ≥ %80 |
| Recall@kalite2+ | Kalite 2–3 etiketlerin kaçı bulundu | ≥ %65 |
| Final recall@3 | Kalite 3 etiketlerin kaçı LLM'den `keep` ile çıktı | ≥ %65 |
| Final precision | LLM'in `keep` dediklerinin kaçı herhangi bir etiketle eşleşiyor | ≥ %50 |
| Aday/saat | Saatte LLM'e giden aday sayısı | 6–20 |
| Zaman hatası | Eşleşen kliplerde başlangıç farkı (sn), medyan | ≤ 4 sn |
| Kesim kalitesi | Kliplerin cümle ortasında başlama oranı (transkriptten otomatik) | ≤ %10 |
| İnsan uyumu tavanı | İki etiketleyici arası uyum (varsa) | raporla |
| Maliyet/saat | `llm_usage` toplamı / yayın saati | ≤ $0.30 |

**Tür kırılımı zorunlu:** Sohbet (G01–G03) ve oyun (G04–G06) ayrı raporlanır. Sohbette içerik yolunun katkısı, oyunda ses + izleyici kliplerinin katkısı ayrıca görülmeli.

**Ablasyon (`--ablation`, LLM/whisper replay ile ücretsiz):** Aynı set şu konfigürasyonlarla tekrar koşulur ve Recall@3 / precision tablosu çıkarılır:

| Konfigürasyon | Ne gösterir |
|---|---|
| A0: sadece chat hızı (eski yöntem) | Başlangıç noktası |
| A1: chat (akıllı: tekil + emote + olay filtresi) | Akıllı chat'in katkısı |
| A2: A1 + ses | Sesin katkısı |
| A3: A2 + izleyici klipleri | İzleyici kliplerinin katkısı |
| A4: A3 + içerik yolu (**tam v1**) | İçerik yolunun katkısı |
| A5: A4 − olay filtresi | Filtrenin yanlış pozitif azaltma etkisi |
| A6: A4 + görüntü kareleri (v2 açıksa) | Görüntünün katkısı (ilk koşu kayıtlı, ~$5) |
| A7: A4 + bağlam özeti (v2 açıksa) | Özetin katkısı |
| A8: A4, judge = Opus 5.5 | Opus'un Sonnet'e göre farkı ve maliyet farkı |

Rapor sonunda otomatik yorum: "Sohbet yayınlarında içerik yolu recall'ı X puan artırdı" gibi.

**Kalibrasyon kontrolü:** Altın setin yarısıyla `clipper calibrate --from-labels` (lag ve eşik önerisi), diğer yarısında test. Önerilen değerlerle recall/precision iyileşiyor mu?

**Öğrenme döngüsü testi (Kademe 2):** Etiketlerden sentetik feedback üret (etiketle eşleşen aday = approved, eşleşmeyen = rejected). `clipper learn` çalıştır, zaman bazlı test setinde AUC ≥ 0.70 ve öğrenilmiş ağırlıklarla final precision artıyor mu? Yetersiz veri durumunda "yetersiz veri" mesajı veriyor mu?

---

## 7. Canlı, dayanıklılık ve kaos testleri (seviye 7–9)

### 7.1 Canlı yayın testi (`clipper eval live --hours 2`) [İNSAN: izinli, canlı 2 kanal]
- [ ] LIVE-01 Kayıt boşluğu toplam ≤ 10 sn / 2 saat (reklam hariç)
- [ ] LIVE-02 Chat kopması olursa otomatik geri gelme ≤ 30 sn
- [ ] LIVE-03 Canlı transkript gecikmesi medyan ≤ 25 sn, hiç 60 sn'yi geçmiyor (veya atlama loglanıyor)
- [ ] LIVE-04 Yayın bitmeden ≥ 5 render edilmiş klip panelde
- [ ] LIVE-05 [İNSAN] Yayını paralel izle, panelin "şimdi oldu" butonuyla 10+ an işaretle → `calibrate` lag tahmini çıkarılıyor, mevcut config ile farkı raporlanıyor
- [ ] LIVE-06 Yayın bitince 2 dk sonra session kapanıyor, kalan işler bitiyor

### 7.2 Dayanıklılık (`clipper eval soak --hours 24`, 3–5 kanal)
Kanal canlı değilse sistem bekler; gerekirse aynı kanallar farklı günlerde. Alternatif: sahte HLS + sahte chat ile **hızlandırılmamış** 24 saat (gerçek zaman), 5 eşzamanlı sentetik yayın.
- [ ] SOAK-01 RSS bellek artışı ilk saatten sonra ≤ %10 (sızıntı yok)
- [ ] SOAK-02 Disk kullanımı sınırlı (tampon + saklama politikası), %85 eşiğinde temizlik tetikleniyor
- [ ] SOAK-03 Açık dosya / soket sayısı sabit
- [ ] SOAK-04 Kuyruk uzunlukları sınırlı (birikme yok); gpu_pool doygunluğunda canlı transkript atlaması devreye giriyor
- [ ] SOAK-05 `dead` iş oranı < %1, nedenleri raporlanmış
- [ ] SOAK-06 Log dosyası rotasyonu çalışıyor

### 7.3 Kaos / hata enjeksiyonu (`clipper eval chaos`)
Sahte sunucular ve süreç kontrolüyle:
- [ ] CHAOS-01 Pipeline ortasında `clipper` süreci öldürülüp yeniden başlatılıyor (5 farklı anda) → her klip tam bir kez üretiliyor, yarım dosya kalmıyor (atomik yazma)
- [ ] CHAOS-02 Disk doluyor (sahte kota) → yeni kayıt durur, uyarı verir, mevcut işler bozulmaz, yer açılınca devam
- [ ] CHAOS-03 İnternet 2 dk kesik → kayıt/chat/LLM/upload işleri backoff ile devam
- [ ] CHAOS-04 Claude API 10 dk 529 dönüyor → adaylar birikir, düzelince işlenir, hiçbiri kaybolmaz
- [ ] CHAOS-05 ffmpeg bozuk segmentle karşılaşıyor (rastgele bayt bozulması) → o klip `failed`, diğerleri etkilenmez
- [ ] CHAOS-06 Whisper GPU OOM → CPU'ya veya küçük modele düşüş, log
- [ ] CHAOS-07 Saat sıçraması (sistem saati 1 saat ileri/geri, sahte) → PDT tabanlı hiza bozulmuyor, monotonic saat kullanılan yerler doğru
- [x] CHAOS-08 Kick Cloudflare 403 / Pusher anahtarı geçersiz → anlaşılır Türkçe hata, diğer platformlar çalışmaya devam — `test_integration::test_chaos08_kick_cloudflare_403_is_clear`
- [x] CHAOS-09 YouTube innertube yapısı değişmiş (fake sunucu alan adlarını değiştirir) → parse hatası alarmı, resmi API yedeğine geçiş (key varsa) veya kanal "chat yok" moduna — `test_integration::test_chaos09_youtube_layout_change_is_detected (alarm var; resmi API'ye otomatik geçiş yok, `YOUTUBE_API_KEY` yedeği ayrı iş)`

---

## 8. Yayınlama ve güvenlik (seviye 10–11)

### 8.1 Gerçek yayınlama (`clipper eval publish --sandbox`) [İNSAN: test hesapları]
Sadece **test hesaplarına** ve izinli içerikle:
- [ ] PUBL-01 YouTube: `unlisted` yükleme, Shorts olarak görünüyor, başlık/açıklama doğru, sonra API ile siliniyor
- [ ] PUBL-02 Instagram: test hesabında Reels yayınlanıyor, permalink dönüyor; `content_publishing_limit` okunuyor
- [ ] PUBL-03 X: test hesabında video tweet, maliyet `llm_usage` benzeri bir sayaçta (`api_usage`) görünüyor; sonra siliniyor
- [ ] PUBL-04 TikTok: inbox modunda video telefondaki uygulamaya taslak/bildirim olarak düşüyor [İNSAN onayı]
- [ ] PUBL-05 Hesap bağlama akışları (OAuth) panelden baştan sona; token'lar DB'de şifreli (ham token dosyada grep ile bulunamıyor)

### 8.2 Güvenlik (`pytest -m security`)
- [x] SEC-01 Panel sadece `127.0.0.1`'e bind (config değiştirilmeden) — `test_security::test_sec01_panel_binds_localhost_by_default + test_panel::test_bind_rules`
- [x] SEC-02 `/media` path traversal (PNL-01 ile aynı, ayrıca symlink) — `test_panel::test_media_is_confined_to_data_dir + test_testplan_units::test_sec02_symlink_out_of_data_dir_refused (panel 403 değil 404 döner; dosyanın varlığını da sızdırmamak için)`
- [x] SEC-03 Loglarda token, API key, refresh token, `Authorization` header'ı yok (tüm log dosyalarında regex taraması) — `test_security::test_sec03_secret_scanner_catches_and_logs_are_clean`
- [x] SEC-04 `.env` ve `data/` `.gitignore`'da; repo'da secret yok (`git grep` ile anahtar kalıpları) — `test_security::test_sec04_gitignore_and_no_committed_secrets`
- [x] SEC-05 `subprocess` çağrılarında `shell=True` yok (AST taraması) — `test_security::test_sec05_no_shell_true`
- [x] SEC-06 Kullanıcı girdisi (başlık, yayıncı adı) ffmpeg argümanlarında veya ASS'de enjeksiyon yapamıyor: `{\`, `\N`, `;`, `'`, `$(...)` içeren başlıkla render güvenli — `test_security::test_sec06_titles_cannot_inject_ass_or_filters`

---

## 9. Manuel kabul kontrol listesi [İNSAN]

Telefonda ve bilgisayarda bizzat bak:
- [ ] MAN-01 Altın setten rastgele 20 klibi telefonda izle: altyazı okunaklı mı, senkron mu, kadraj doğru mu, ses dengeli mi? (her biri için 1–5 puan, ortalama ≥ 4)
- [ ] MAN-02 Başlık ve hook'lar Türkçe olarak doğal mı, yalan/abartı var mı? (20 klipte ≤ 2 sorunlu)
- [ ] MAN-03 Klipler tek başına anlaşılıyor mu? (bağlamsız oranı ≤ %20)
- [ ] MAN-04 Panel akışı: bir klibi bul → kırp → başlığı değiştir → yeniden render → onayla → yayın sırasında gör; 2 dk'dan kısa sürüyor mu?
- [ ] MAN-05 "Reddedilenler"den 5 aday kurtar; mantıklı adaylar orada mı?
- [ ] MAN-06 Bir yayıncıyı sıfırdan ekleme (facecam seçimi dahil) 5 dk'dan kısa mı?
- [ ] MAN-07 Hata mesajları Türkçe ve ne yapılacağını söylüyor mu? (CHAOS testlerindeki mesajları oku)

---

## 10. Sonuç raporu

Tüm seviyeler bitince Claude Code `eval/reports/SUMMARY.md` yazsın:
1. Seviye seviye geçti/kaldı tablosu (test ID'leriyle)
2. Altın set metrikleri + tür kırılımı + ablasyon tablosu
3. Maliyet (eval'in kendisi + üretim tahmini/saat)
4. Bulunan hatalar ve düzeltmeleri (commit referanslarıyla)
5. Kalan riskler ve önerilen config değişiklikleri (eşikler, lag, model seçimi)
6. "Üretime hazır mı?" kararı: Bölüm 6 hedefleri + E2E + SOAK + CHAOS + SEC + MAN geçtiyse **evet**, değilse eksik listesi

**Regresyon kuralı:** Bundan sonra dedektör, prompt veya model değişikliği yapılırsa `clipper eval synthetic` + `clipper eval golden` (replay) tekrar koşulur. Recall@3 veya final precision 3 puandan fazla düşerse değişiklik geri alınır veya gerekçelendirilir.
