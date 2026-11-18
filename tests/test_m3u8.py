from __future__ import annotations

from clipper.recorder.m3u8 import parse_master, parse_media, pick_variant

MASTER = """#EXTM3U
#EXT-X-TWITCH-INFO:ORIGIN="s3"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2",VIDEO="chunked",FRAME-RATE=60.000
chunked/index-dvr.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="720p60",NAME="720p60",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="720p60",FRAME-RATE=60.000
720p60/index-dvr.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="audio_only",NAME="Audio Only",AUTOSELECT=NO,DEFAULT=NO
#EXT-X-STREAM-INF:BANDWIDTH=160000,CODECS="mp4a.40.2",VIDEO="audio_only"
audio_only/index-dvr.m3u8
"""

MEDIA_LIVE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-11-18T10:00:04.000Z",DURATION=2.0
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T10:00:00.000Z
#EXTINF:2.000,live
seg100.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T10:00:02.000Z
#EXTINF:2.000,live
seg101.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T10:00:04.000Z
#EXTINF:2.000,live
seg102.ts
#EXTINF:2.000,Amazon|123
ad.ts
"""


def test_master_variants() -> None:
    vs = parse_master(MASTER, "https://example.com/vod/1/master.m3u8")
    assert [v.name for v in vs] == ["1080p60", "720p60", "Audio Only"]
    assert vs[0].url == "https://example.com/vod/1/chunked/index-dvr.m3u8"
    assert vs[0].frame_rate == 60.0 and vs[0].resolution == "1920x1080"


def test_pick_variant_preference() -> None:
    vs = parse_master(MASTER, "https://e/")
    assert pick_variant(vs, ["936p60", "720p60", "best"]).name == "720p60"
    assert pick_variant(vs, ["480p", "best"]).name == "1080p60"
    assert pick_variant(vs, ["nothing"]).name == "1080p60"


def test_media_playlist_pdt_and_ads() -> None:
    pl = parse_media(MEDIA_LIVE, "https://e/x/")
    assert pl.target_duration == 2 and pl.media_sequence == 100 and not pl.ended
    assert [s.seq for s in pl.segments] == [100, 101, 102, 103]
    assert pl.segments[0].url == "https://e/x/seg100.ts"
    assert pl.segments[1].pdt is not None and pl.segments[0].pdt is not None
    assert pl.segments[1].pdt - pl.segments[0].pdt == 2.0
    assert [s.is_ad for s in pl.segments] == [False, False, True, True]


def test_vod_playlist_without_pdt() -> None:
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.000,\n0.ts\n"
        "#EXTINF:4.5,\n1.ts\n#EXT-X-ENDLIST\n"
    )
    pl = parse_media(text, "https://e/")
    assert pl.ended
    assert [(s.seq, s.duration, s.pdt, s.is_ad) for s in pl.segments] == [
        (0, 10.0, None, False),
        (1, 4.5, None, False),
    ]
