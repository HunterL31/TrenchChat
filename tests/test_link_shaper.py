"""
Tests for the dev environment's connection shaper.

Covers the pure pieces -- the HDLC frame splitter, the store-and-forward
schedule, and the profile table -- without sockets or an event loop.
"""

import sys
from pathlib import Path

import pytest

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

from link_profiles import (  # noqa: E402
    LINK_PROFILES, LinkProfile, lora_bitrate, resolve,
)
from link_shaper import HDLC_FLAG, schedule, split_frames  # noqa: E402

FLAG = bytes([HDLC_FLAG])


def _frame(payload: bytes) -> bytes:
    return FLAG + payload + FLAG


def test_split_frames_returns_whole_frames():
    stream = _frame(b"one") + _frame(b"two")
    frames, tail = split_frames(stream)

    assert frames == [_frame(b"one"), _frame(b"two")]
    assert tail == b""


def test_split_frames_is_byte_exact():
    """Concatenating the frames and tail must reproduce the input untouched."""
    stream = _frame(b"alpha") + _frame(b"beta") + FLAG + b"partia"
    frames, tail = split_frames(stream)

    assert b"".join(frames) + tail == stream


def test_split_frames_holds_partial_frame_as_tail():
    frames, tail = split_frames(_frame(b"done") + FLAG + b"incomplete")

    assert frames == [_frame(b"done")]
    assert tail == FLAG + b"incomplete"


def test_split_frames_does_not_split_on_escaped_flag():
    """0x7D 0x5E is an escaped 0x7E and must stay inside its frame."""
    escaped = bytes([0x7D, 0x5E])
    payload = b"a" + escaped + b"b"
    frames, tail = split_frames(_frame(payload))

    assert frames == [_frame(payload)]
    assert tail == b""


def test_split_frames_with_no_flags_keeps_everything_as_tail():
    frames, tail = split_frames(b"no flags here")

    assert frames == []
    assert tail == b"no flags here"


def test_schedule_serialises_back_to_back_frames():
    """A burst queues on the channel instead of each frame paying full price."""
    profile = LinkProfile("t", "t", "t", bitrate_bps=1000)
    frame_len = 125  # 1000 bits, so exactly one second on the wire

    first_deliver, free_at = schedule(0.0, 0.0, frame_len, profile)
    second_deliver, free_at = schedule(0.0, free_at, frame_len, profile)

    assert first_deliver == pytest.approx(1.0)
    assert second_deliver == pytest.approx(2.0)
    assert free_at == pytest.approx(2.0)


def test_schedule_latency_is_not_paid_per_frame_in_a_burst():
    profile = LinkProfile("t", "t", "t", bitrate_bps=1000, latency_ms=500.0)
    frame_len = 125

    first_deliver, free_at = schedule(0.0, 0.0, frame_len, profile)
    second_deliver, _ = schedule(0.0, free_at, frame_len, profile)

    assert first_deliver == pytest.approx(1.5)
    # The second frame trails the first by its serialisation time only.
    assert second_deliver - first_deliver == pytest.approx(1.0)


def test_schedule_idle_channel_does_not_rewind_to_the_past():
    profile = LinkProfile("t", "t", "t", bitrate_bps=1000)

    deliver_at, free_at = schedule(100.0, 5.0, 125, profile)

    assert deliver_at == pytest.approx(101.0)
    assert free_at == pytest.approx(101.0)


def test_schedule_unshaped_delivers_immediately():
    profile = LINK_PROFILES["broadband"]

    deliver_at, free_at = schedule(42.0, 0.0, 4096, profile)

    assert deliver_at == pytest.approx(42.0)
    assert free_at == pytest.approx(42.0)


def test_schedule_applies_jitter_in_both_directions():
    profile = LinkProfile("t", "t", "t", latency_ms=100.0, jitter_ms=50.0)

    early, _ = schedule(0.0, 0.0, 0, profile, jitter_frac=-1.0)
    late, _ = schedule(0.0, 0.0, 0, profile, jitter_frac=1.0)

    assert early == pytest.approx(0.05)
    assert late == pytest.approx(0.15)


def test_schedule_never_returns_negative_latency():
    """Jitter larger than the base latency must not pull delivery backwards."""
    profile = LinkProfile("t", "t", "t", latency_ms=10.0, jitter_ms=100.0)

    deliver_at, _ = schedule(7.0, 0.0, 0, profile, jitter_frac=-1.0)

    assert deliver_at == pytest.approx(7.0)


def test_lora_bitrate_matches_rnode_formula():
    assert lora_bitrate(7) == 5469
    assert lora_bitrate(10) == 977
    assert lora_bitrate(12) == 293


def test_shipped_lora_profiles_use_the_formula():
    assert LINK_PROFILES["lora_fast"].bitrate_bps == lora_bitrate(7)
    assert LINK_PROFILES["lora_long"].bitrate_bps == lora_bitrate(10)


def test_default_profile_is_unshaped():
    assert LINK_PROFILES["broadband"].shaped is False
    assert LINK_PROFILES["broadband"].summary() == "unshaped"


def test_every_other_profile_is_shaped():
    shaped = [p for name, p in LINK_PROFILES.items() if name != "broadband"]

    assert shaped and all(p.shaped for p in shaped)


def test_resolve_without_overrides_returns_the_shipped_profile():
    assert resolve("lora_fast") is LINK_PROFILES["lora_fast"]


def test_resolve_applies_overrides():
    profile = resolve("custom", bitrate_bps=2400, latency_ms=50.0, loss_pct=7.5)

    assert profile.bitrate_bps == 2400
    assert profile.latency_ms == 50.0
    assert profile.loss_pct == 7.5
    # Untouched fields keep the base profile's values.
    assert profile.jitter_ms == LINK_PROFILES["custom"].jitter_ms


def test_resolve_clamps_out_of_range_overrides():
    profile = resolve("custom", bitrate_bps=-1, latency_ms=-5.0, loss_pct=500.0)

    assert profile.bitrate_bps == 0
    assert profile.latency_ms == 0.0
    assert profile.loss_pct == 100.0


def test_resolve_rejects_an_unknown_profile():
    with pytest.raises(ValueError):
        resolve("carrier_pigeon")


def test_summary_reports_the_shaped_numbers():
    assert LINK_PROFILES["lora_long"].summary() == "1.0 kbps · 120 ± 40 ms · 3% loss"
