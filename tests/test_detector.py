from __future__ import annotations

from collections.abc import Iterable

import pytest

from clipper.config import DetectorConfig
from clipper.detect.detector import Detection, Detector, Observation
from clipper.detect.signals import bin_messages
from clipper.models import ChatMessage
from tests.fixtures.make_fixtures import BASE_TS, DEFAULT_SPIKES, Spike, iter_chat

SECONDS = 180


def cfg(**kw: object) -> DetectorConfig:
    base: dict[str, object] = dict(warmup_seconds=30, lag_seconds=5, pre_roll=10, post_roll=10)
    base.update(kw)
    return DetectorConfig.model_validate(base)


def run(
    messages: Iterable[ChatMessage],
    c: DetectorConfig,
    seconds: int = SECONDS,
    chat_down: tuple[int, int] | None = None,
) -> list[Detection]:
    bins = bin_messages(messages)
    det = Detector(c)
    out: list[Detection] = []
    for t in range(seconds):
        b = bins.get(t)
        ok = not (chat_down and chat_down[0] <= t < chat_down[1])
        out += det.feed(
            Observation(
                t, b.msgs if b else 0, b.uniq if b else 0, b.emote if b else 0.0, chat_ok=ok
            )
        )
    return out + det.flush()


def synthetic(
    seed: int, spikes: tuple[Spike, ...] = DEFAULT_SPIKES, base_rate: float = 1.0
) -> list[ChatMessage]:
    return [
        ChatMessage.from_json({**m, "ts": float(m["ts"]) - BASE_TS})  # type: ignore[arg-type]
        for m in iter_chat(SECONDS, spikes=spikes, base_rate=base_rate, seed=seed)
    ]


def _near(peaks: list[float], at: float, dur: float) -> bool:
    return any(at <= p < at + dur + 4 for p in peaks)


@pytest.mark.parametrize("seed", range(12))
def test_finds_all_three_spikes_no_false_positives(seed: int) -> None:
    peaks = [d.peak_ts for d in run(synthetic(seed), cfg())]
    for sp in DEFAULT_SPIKES:
        assert _near(peaks, sp.at, sp.duration), (seed, peaks)
    assert len(peaks) == 3, (seed, peaks)


@pytest.mark.parametrize("base_rate", [0.3, 1.0, 4.0])
def test_flat_traffic_no_triggers(base_rate: float) -> None:
    for seed in range(6):
        assert run(synthetic(seed, spikes=(), base_rate=base_rate), cfg()) == []


def test_window_and_lag() -> None:
    c = cfg(lag_seconds=8, pre_roll=12, post_roll=9)
    d = run(synthetic(1), c)[0]
    assert d.event_ts == d.peak_ts - 8
    assert d.win_start == d.event_ts - 12 and d.win_end == d.peak_ts + 9


def test_warmup_blocks_early_spike() -> None:
    peaks = [d.peak_ts for d in run(synthetic(2), cfg(warmup_seconds=60))]
    assert not _near(peaks, 45, 8) and len(peaks) == 2


def test_cooldown() -> None:
    spikes = (Spike(60, 6, 15, ("KEKW",)), Spike(80, 6, 15, ("KEKW",)))
    assert len(run(synthetic(3, spikes), cfg(cooldown=45, post_roll=0, pre_roll=0))) == 1
    assert len(run(synthetic(3, spikes), cfg(cooldown=10, post_roll=0, pre_roll=0))) == 2


def test_overlapping_windows_merge() -> None:
    spikes = (Spike(60, 6, 15, ("KEKW",)), Spike(85, 6, 15, ("oha",)))
    dets = run(synthetic(4, spikes), cfg(cooldown=10, pre_roll=20, post_roll=25))
    assert len(dets) == 1
    assert dets[0].features["merged"] == 2
    assert dets[0].win_start <= 60 - 5 and dets[0].win_end >= 85 + 25


def test_single_user_spam_is_not_a_spike() -> None:
    msgs = synthetic(5, spikes=())
    msgs += [ChatMessage(100 + i * 0.05, "spammer", "KEKW KLİPLE") for i in range(200)]
    assert run(sorted(msgs, key=lambda m: m.ts), cfg()) == []


def test_no_trigger_while_chat_down() -> None:
    assert len(run(synthetic(6), cfg(), chat_down=(95, 115))) == 2


def test_viewer_clip_bonus() -> None:
    msgs = synthetic(7)
    bins = bin_messages(msgs)
    det = Detector(cfg())
    det.add_viewer_clip(110.0)
    out = []
    for t in range(SECONDS):
        b = bins.get(t)
        out += det.feed(
            Observation(t, b.msgs if b else 0, b.uniq if b else 0, b.emote if b else 0.0)
        )
    out += det.flush()
    bonus = [d for d in out if "viewer_clips" in d.features]
    assert len(bonus) == 1 and 100 <= bonus[0].peak_ts < 112


def test_streaming_with_gaps_equals_dense() -> None:
    msgs = synthetic(8)
    bins = bin_messages(msgs)
    dense = run(msgs, cfg())
    sparse = Detector(cfg())
    out: list[Detection] = []
    for t in sorted(bins):  # only seconds that have messages; gaps are auto-filled
        b = bins[t]
        out += sparse.feed(Observation(t, b.msgs, b.uniq, b.emote))
    out += sparse.flush()
    assert [d.peak_ts for d in out] == [d.peak_ts for d in dense]


def test_out_of_order_rejected() -> None:
    det = Detector(cfg())
    det.feed(Observation(5))
    with pytest.raises(ValueError):
        det.feed(Observation(5))


def test_audio_contributes_with_lag() -> None:
    msgs = synthetic(9, spikes=())
    bins = bin_messages(msgs)
    c = cfg(lag_seconds=5, threshold=0.6, min_rate=0.0, min_ratio=0.0)
    det = Detector(c)
    seen = []
    for t in range(SECONDS):
        b = bins.get(t)
        audio = -10.0 if 90 <= t < 96 else -30.0 + (t % 3)
        det.feed(
            Observation(
                t, b.msgs if b else 0, b.uniq if b else 0, b.emote if b else 0.0, audio_db=audio
            )
        )
        seen.append((t, det.history[-1].z["audio"]))
    # loud audio at 90..95 shows up in the score 5 s later
    assert max(seen, key=lambda r: r[1])[0] in range(95, 101)
