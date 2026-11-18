# CLAUDE.md — clipper projesi çalışma kuralları

Bu repo, yayıncı kliplerini chat hızına göre otomatik tespit edip dikey video olarak editleyen ve sosyal platformlara yükleyen bir sistem. **Tek kaynak doküman `ROADMAP.md`.** Her oturumun başında onu oku.

## Nasıl çalışacaksın
- Fazları **sırayla** uygula (Faz 0 → 8). Bir fazın "Bitti sayılması için" maddeleri sağlanmadan sonrakine geçme.
- Bitirdiğin maddeyi `ROADMAP.md` içinde `[x]` yap. Yaptığın sapmaları ilgili maddenin altına kısa not olarak yaz.
- Her anlamlı adımdan sonra `uv run pytest` çalıştır. Kırmızı testle commit yapma.
- Küçük, odaklı commit'ler: `faz2: detector EWMA baseline`.
- Bir faz bittiğinde kullanıcıya kısa özet ver: ne çalışıyor, nasıl denenir (komut), neye ihtiyacın var.

## Bağımlılık kuralı
- Sadece ROADMAP Bölüm 1'deki dış bağımlılıklar. Yeni paket eklemeden önce gerekçesini kullanıcıya yaz ve onay al.
- Celery, Redis, Postgres, MoviePy, OpenCV, MediaPipe, streamlink, Upload-Post, Postiz, requests kullanma. HTTP için `httpx`, Kick için `curl_cffi`.

## API ve doğruluk
- Platform API alanlarını, endpoint adlarını, kota/fiyatları **tahmin etme**. Yazmadan önce resmi dokümandan doğrula (dev.twitch.tv, developers.google.com/youtube, developers.facebook.com, developers.tiktok.com, developer.x.com, docs.claude.com). ROADMAP'teki değerler 2026-11 itibarıyla; farklıysa güncel dokümana uy ve ROADMAP'i düzelt.
- Resmi olmayan uç noktalar (Twitch GQL, Kick API/Pusher, YouTube /live parse) sadece ilgili `platforms/<p>/` modülünde olsun. Parametreleri config'te tutup bozulduğunda anlaşılır hata ver.

## Kod standartları
- Python 3.12, tam type hint, `ruff` uyumlu stil (ruff dev bağımlılığı olabilir).
- Async I/O. Ağır işler subprocess/process pool'da. Event loop'u bloklama.
- Tüm yollar `pathlib`. Windows 11 ve Linux'ta çalışmalı. `subprocess` asla `shell=True` değil.
- Türkçe metin işlemede `str.upper()/lower()` yerine `render/tr_text.py` fonksiyonları.
- Gizli bilgiler sadece `.env` ve şifreli `oauth_tokens` tablosunda. Log'a token/anahtar yazma.
- Kullanıcıya dönük metinler (panel, CLI çıktıları, hata mesajları) **Türkçe**. Kod, değişken adları ve commit mesajları İngilizce.

## Testler
- Ağ gerektiren her şey için sahte sunucu veya `httpx.MockTransport`. Testler internetsiz geçmeli.
- Gerçek ağ testleri `pytest -m online` işaretiyle ayrı tutulur.
- Fixture videoları `tests/fixtures/make_fixtures.py` ile ffmpeg'den üretilir. Büyük binary dosyaları repoya koyma.

## Kullanıcıdan isteyeceklerin
API anahtarları, OAuth client'ları, developer app kurulumları ve yayıncı izinleri kullanıcıda (ROADMAP Bölüm 2). İhtiyaç olduğunda tam olarak neyi, nereden alacağını adım adım söyle. Beklerken bağımsız işlere devam et.

## Yasaklar
- `permission_ok=0` olan yayıncının klibini hiçbir koşulda yayınlama.
- TikTok direct post'u, kullanıcı audit'in geçtiğini söylemeden açma.
- Platform limitlerini aşmaya çalışan hile (çoklu hesap rotasyonu, bot tespitinden kaçma) yazma.
