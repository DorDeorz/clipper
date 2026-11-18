# clipper

clipper izinli yayıncıların yayınlarını izler ve iyi anları bulur. Bulduğu anları altyazılı dikey
klip olarak editler. Senin onayından sonra klipleri YouTube Shorts, Instagram Reels ve X'te
yayınlar, TikTok'a da taslak olarak gönderir.

Tasarım ve yol haritası [ROADMAP.md](ROADMAP.md) dosyasında, çalışma kuralları
[CLAUDE.md](CLAUDE.md) dosyasında.

Anları üç kaynaktan buluyor:
- chat'in hızlanması,
- yayında söylenenlerin (transkript) Claude ile taranması,
- izleyicilerin aldığı klipler.

## Kurulum

Gerekenler:
- [uv](https://docs.astral.sh/uv/)
- Python 3.12 (uv kendisi kurar)
- ffmpeg, libass destekli olmalı. Windows'ta gyan.dev'in "full" sürümünü kullan.
- NVIDIA GPU önerilir (8 GB ve üstü VRAM). GPU yoksa transkript ve render CPU'da daha yavaş çalışır.

```bash
uv sync
cp .env.example .env              # Windows: copy .env.example .env
cp config.example.yaml config.yaml
uv run clipper keygen             # çıkan CLIPPER_SECRET_KEY satırını .env'e ekle
uv run clipper doctor             # ffmpeg, GPU, anahtarlar ve disk kontrolü
uv run clipper run                # paneli açar: http://127.0.0.1:8765
```

Yayıncıları paneldeki **Yayıncılar** sekmesinden ekle. Her yayıncı için sırasıyla:
1. Facecam alanını karenin üstüne çiz.
2. Yazılı iznin varsa "Yayıncı izni var" kutusunu işaretle. **İzni olmayan yayıncının klibi hiçbir
   koşulda yayınlanmaz.**
3. İstersen otomatik yayını aç.

## `.env`

| Anahtar | Ne için | Nereden |
|---|---|---|
| `ANTHROPIC_API_KEY` | ön eleme, değerlendirme, içerik taraması | console.anthropic.com |
| `CLIPPER_SECRET_KEY` | bağlı hesap token'larının şifrelenmesi | `uv run clipper keygen` |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` | YouTube'a yükleme | Google Cloud, OAuth client tipi *Desktop app* |
| `META_APP_ID`, `META_APP_SECRET` | Instagram Reels | Meta Developer, Facebook Login for Business |
| `X_CLIENT_ID`, `X_CLIENT_SECRET` | X | developer.x.com, OAuth 2.0 |
| `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` | TikTok taslak gönderimi | developers.tiktok.com, Login Kit (Desktop) ve Content Posting API |
| `PANEL_PASSWORD` | panel 127.0.0.1 dışına açılacaksa | kendin seç |

Yayın izleme için hesap gerekmez. Twitch, Kick ve YouTube anonim olarak okunur.

## Hesap bağlama

Her platformun geliştirici uygulamasına şu yönlendirme adresini ekle:
`http://127.0.0.1:8765/callback/<platform>`. `<platform>` yerine `youtube`, `instagram`, `x` ya da
`tiktok` yaz.

Sonra panelde **Hesaplar → Hesap bağla** ile tarayıcıda yetki ver. Token'lar şifreli saklanır ve
süresi dolmadan kendiliğinden yenilenir.

Platformlara göre notlar:
- **YouTube:** OAuth consent screen "Testing" modundaysa refresh token yaklaşık 7 günde düşer.
  Uygulamayı "In production"a al ya da haftada bir yeniden bağla. Yükleme kotası günde 100 video.
- **Instagram:** Hesap profesyonel (Business ya da Creator) olmalı ve bir Facebook Sayfasına bağlı
  olmalı.
  - Gereken izinler: `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
    `pages_read_engagement`.
  - Hesabın uygulamada admin ya da tester rolündeyse app review gerekmez.
- **X:** Scope listesi `tweet.read tweet.write users.read media.write offline.access`. Gönderi
  ücretlidir (pay-per-use).
- **TikTok:** Varsayılan **inbox** modunda video telefondaki TikTok uygulamasına taslak olarak
  düşer, paylaşmayı orada sen bitirirsin. Doğrudan paylaşım (`tiktok_mode: direct`) için TikTok
  audit'inin geçmiş olması ve config'te `tiktok_audit_passed: true` yazması gerekir.

Onaylanan klipler platform başına belli aralıklarla ve günlük sınırla planlanır
(`publish.min_gap_minutes`, `publish.daily_cap`). Aynı klip platformlar arasında 15 dakika arayla
gider. Durumu ve linkleri **Yayınlar** sekmesinde görürsün, başarısız olanları oradan "Tekrar dene"
ile yeniden gönderebilirsin.

## Komutlar

| Komut | Ne yapar |
|---|---|
| `clipper run [--no-browser] [--no-live]` | panel, canlı takip, iş kuyruğu ve yayınlama |
| `clipper doctor` | ortam kontrolü |
| `clipper vod <url/dosya>` | geçmiş yayından klip çıkar |
| `clipper calibrate <yayıncı> [--apply]` | panelde basılan "Şimdi oldu" işaretlerinden chat gecikmesi önerir |
| `clipper learn [--streamer X]` | kararlardan skor modeli öğrenir, raporlar ve öneri üretir (en az 200 karar gerekir) |
| `clipper cleanup [--dry-run]` | saklama politikasına göre eski dosyaları siler |
| `clipper install-autostart [--dry-run]` / `uninstall-autostart` | oturum açınca otomatik başlatma (Windows Görev Zamanlayıcı / systemd) |

Öğrenilen öneriler kendiliğinden uygulanmaz. Panelde **Maliyet & Log → Öğrenme** bölümünde
"Öneriyi uygula" ile uygulanır, "Geri al" ile eski değerlere dönülür.

## Saklama

Varsayılan süreler (`retention` ve `recorder.buffer_minutes` ayarları):
- Canlı yayın segmentleri: 15 dakika
- Ham kesitler: 7 gün
- Final klipler: 30 gün. Yayını bekleyen klip silinmez.

Disk %85 dolunca 1 ve 7 günlük agresif süreler devreye girer ve panelin üstünde uyarı görünür.

## Sık karşılaşılan hatalar

- **"CLIPPER_SECRET_KEY yok"**: `uv run clipper keygen` çıktısını `.env`'e ekle. Anahtarı
  değiştirirsen bağlı hesapları yeniden bağlaman gerekir.
- **Hesap bağlarken "redirect_uri mismatch"**: Uygulamadaki yönlendirme adresi birebir
  `http://127.0.0.1:8765/callback/<platform>` olmalı. `localhost` ile `127.0.0.1` farklı sayılır.
- **"hesabı yeniden bağla"**: Token iptal edilmiş ya da süresi dolmuş. Hesaplar sekmesinden yeniden
  bağla, sonra Yayınlar'da "Tekrar dene".
- **"panel.host dışarıya açık"**: Panel 127.0.0.1 dışına açılacaksa `.env`'e `PANEL_PASSWORD` ekle.
- **LLM işleri kuyrukta bekliyor**: `ANTHROPIC_API_KEY` eksik ya da aylık bütçe
  (`llm.monthly_budget_usd`) dolmuş.
- **ffmpeg altyazı hatası**: Kullandığın ffmpeg libass desteklemiyor. Windows'ta gyan.dev "full"
  sürümünü kur.
- **Twitch, Kick ya da YouTube canlı takip çalışmıyor**: Anonim uç noktalar değişmiş olabilir. Ayar
  değerleri `config.yaml`'da (`twitch`, `kick`, `youtube` bölümleri). Ayrıntılar loglarda ve
  `clipper doctor` çıktısında.

## Geliştirme

```bash
uv run pytest                     # internetsiz geçer
uv run pytest -m online           # gerçek ağ testleri
uv run ruff check . && uv run ruff format --check .
uv run python tests/fixtures/make_fixtures.py   # örnek video ve sentetik chat
```
