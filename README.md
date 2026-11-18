# clipper

Yayıncı kliplerini chat hızına göre otomatik tespit edip dikey video olarak editleyen ve sosyal
platformlara yükleyen sistem. Tasarım ve yol haritası: [ROADMAP.md](ROADMAP.md). Çalışma
kuralları: [CLAUDE.md](CLAUDE.md).

## Hızlı başlangıç

Gerekenler: [uv](https://docs.astral.sh/uv/), ffmpeg (libass destekli; Windows için gyan.dev
"full" build).

```bash
uv sync
cp .env.example .env            # anahtarları doldur
cp config.example.yaml config.yaml
uv run clipper keygen           # çıktıyı .env'e ekle
uv run clipper doctor           # ortam kontrolü
uv run pytest                   # testler (internetsiz geçer)
```

Test videosu ve sentetik chat üretmek için:
`uv run python tests/fixtures/make_fixtures.py`
