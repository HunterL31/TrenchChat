"""
The spoken sentence over links people actually have.

tests/test_voice_speech.py sends it over a path that never loses, delays or
reorders anything, which flatters the whole pipeline. These send the same
sentence over the dev environment's consumer link profiles (home fibre,
home Wi-Fi, mobile LTE) and over a stalled path, and ask the same question
at the far end: which words arrived, in what order, and how much of the
sound survived.

What the four cases pin down:

- On a good home link the sentence is untouched.
- On Wi-Fi and LTE it is still the sentence, spoken in order, with the
  timing intact. It is rougher, and the receive metrics say so: that is
  what a connection indicator in the UI is reading.
- A stalled path (a Wi-Fi retransmit storm, a handover) costs continuity
  and nothing else. The listener hears a break; no word is lost, none
  arrives out of order, and the stream does not slip out of time.
- Stalls do not accumulate. Three of them in one sentence cost far less
  than three stalls' worth of delay, because the buffer gives the time
  back at the next pause instead of holding it for the rest of the call.
"""

import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("opuslib")

from tests import speech                                        # noqa: E402
from tests.fake_network import (                                 # noqa: E402
    HOME_FIBRE, HOME_WIFI, MOBILE_LTE, ShapedPath,
)
from tests.helpers import wait_for_roster                        # noqa: E402
from trenchchat.network.voice_wire import VOICE_FRAME_MS          # noqa: E402
from tests.test_voice import _setup_invite_channel               # noqa: E402
from tests.test_voice_speech import (                            # noqa: E402
    _gaps, _join_with_devices, _wait_for_stream, _wait_until_spoken,
)

# Measured floors, with room for a loaded machine. Home fibre reproduces
# the unshaped result (0.966); Wi-Fi lands 0.81 to 0.93, LTE 0.74 to 0.89,
# and a stalled path 0.25 to 0.59, which is the break being audible.
FIBRE_CORRELATION = 0.90
WIFI_CORRELATION = 0.70
MOBILE_CORRELATION = 0.60
BROKEN_CORRELATION = 0.75

# How far the sentence may stretch or shrink between first word and last.
DRIFT_TOLERANCE_SECS = 0.12
GAP_TOLERANCE_SECS = 0.12

STALL_SECS = 0.25
REPEATED_STALLS = ((0.25, 0.3), (0.75, 0.3), (1.25, 0.3))
# Three 300 ms stalls: holding every one of them would put the last word
# nearly a second late.
ACCUMULATED_STALL_SECS = sum(secs for _at, secs in REPEATED_STALLS)
STALL_RECOVERY_SECS = 0.4

_STALL_POLL_SECS = 0.01


class Call:
    """What one shaped call produced, ready to be measured."""

    def __init__(self, spoken: bytes, heard: bytes, quality: dict,
                 path: ShapedPath):
        self.spoken = spoken
        self.heard = heard
        self.quality = quality
        self.path = path

    @property
    def correlation(self) -> float:
        return speech.band_envelope_correlation(self.spoken, self.heard)

    @property
    def words_in_order(self) -> list[float]:
        """The words heard, each snapped to the pitch it was spoken at.

        A dropout can break one word into two runs, which is a continuity
        problem and not an ordering one, so consecutive runs of the same
        word count once.
        """
        ordered: list[float] = []
        for word in speech.words(self.heard):
            nearest = min(speech.WORD_PITCHES_HZ,
                          key=lambda pitch: abs(pitch - word["pitch_hz"]))
            if not ordered or ordered[-1] != nearest:
                ordered.append(nearest)
        return ordered

    @property
    def drift_secs(self) -> float:
        """How much longer the sentence took to arrive than to say."""
        return self._span(speech.words(self.heard)) - \
            self._span(speech.words(self.spoken))

    @staticmethod
    def _span(words: list[dict]) -> float:
        return words[-1]["end_secs"] - words[0]["start_secs"] if words else 0.0


def _call_over(peer_factory, monkeypatch, profile: str, *, seed: int,
               stalls: tuple = (), direct: bool = False) -> Call:
    """Speak the sentence from one peer to another over a shaped path.

    With *direct* the sending plane carries the bundle the way a direct
    session does: one frame per datagram at the direct path's budget, each
    timed on its own.
    """
    alice, bob, ch_hash = _setup_invite_channel(peer_factory)
    path = ShapedPath(profile, seed=seed)
    alice.voice_transport.path = path
    alice.voice_transport.direct = direct
    spoken = speech.sentence()

    microphone, _ = _join_with_devices(alice, ch_hash, monkeypatch, spoken)
    assert wait_for_roster(bob, ch_hash, alice.identity.hash_hex)
    _, listener = _join_with_devices(bob, ch_hash, monkeypatch)
    _wait_for_stream(alice, bob.identity.hash_hex)

    microphone.speak()
    for at_secs, hold_secs in stalls:
        while microphone.position_secs < at_secs:
            time.sleep(_STALL_POLL_SECS)
        path.stall(hold_secs)
    _wait_until_spoken(microphone, bob, alice.identity.hash_hex)

    quality = bob.voice_mgr.frame_stats()["rx_quality"][
        alice.identity.hash_hex]
    bob.voice_mgr.leave_voice()
    return Call(spoken, listener.played(), quality, path)


class TestOrdinaryConsumerLinks:
    def test_a_good_home_link_delivers_the_sentence_untouched(
            self, peer_factory, monkeypatch):
        call = _call_over(peer_factory, monkeypatch, HOME_FIBRE, seed=1)

        assert [round(word["pitch_hz"]) for word in speech.words(call.heard)] \
            == [round(pitch) for pitch in speech.WORD_PITCHES_HZ], \
            "a 12 ms link broke the sentence into something else"
        for spoken_gap, heard_gap in zip(_gaps(speech.words(call.spoken)),
                                         _gaps(speech.words(call.heard))):
            assert abs(heard_gap - spoken_gap) <= GAP_TOLERANCE_SECS
        assert call.correlation >= FIBRE_CORRELATION
        assert call.quality["jitter_ms"] < 10.0, \
            "a steady link should not read as a jittery one"

    def test_wifi_timing_costs_quality_and_not_the_words(self, peer_factory,
                                                         monkeypatch):
        call = _call_over(peer_factory, monkeypatch, HOME_WIFI, seed=1)

        assert call.words_in_order == list(speech.WORD_PITCHES_HZ)
        assert abs(call.drift_secs) <= DRIFT_TOLERANCE_SECS
        assert call.correlation >= WIFI_CORRELATION
        assert call.quality["jitter_ms"] >= 8.0, \
            "20 ms of path jitter went unreported"

    def test_mobile_reordering_is_absorbed(self, peer_factory, monkeypatch):
        """LTE swings wider than the 40 ms between packets, so packets
        arrive out of order. The jitter buffer has to put them back, and
        the metrics have to admit it happened."""
        call = _call_over(peer_factory, monkeypatch, MOBILE_LTE, seed=1)

        assert call.quality["late"] > 0, \
            "no packet arrived out of order, so nothing was reordered"
        assert call.words_in_order == list(speech.WORD_PITCHES_HZ)
        assert abs(call.drift_secs) <= DRIFT_TOLERANCE_SECS
        assert call.correlation >= MOBILE_CORRELATION


class TestStalledPaths:
    def test_a_stall_costs_continuity_and_nothing_else(self, peer_factory,
                                                       monkeypatch):
        """A quarter second where nothing gets through, then everything at
        once. The listener hears a break; the sentence must survive it
        whole, in order, and still in time."""
        call = _call_over(peer_factory, monkeypatch, HOME_WIFI, seed=3,
                          stalls=((0.30, STALL_SECS),))

        assert call.correlation < BROKEN_CORRELATION, \
            "the stall left no mark, so this test is proving nothing"
        assert call.words_in_order == list(speech.WORD_PITCHES_HZ), \
            "the stall cost a word or reordered the sentence"
        assert abs(call.drift_secs) <= DRIFT_TOLERANCE_SECS, \
            "the stream never came back into time after the stall"

    def test_stalls_do_not_accumulate_across_a_sentence(self, peer_factory,
                                                        monkeypatch):
        """Three stalls, and the buffer has to give the time back at the
        pauses rather than carry it. Holding all of it would leave the
        last word most of a second late, and every later word with it."""
        call = _call_over(peer_factory, monkeypatch, HOME_FIBRE, seed=2,
                          stalls=REPEATED_STALLS)

        assert call.words_in_order == list(speech.WORD_PITCHES_HZ)
        assert call.drift_secs <= STALL_RECOVERY_SECS, \
            (f"the sentence ran {call.drift_secs:.2f}s late against "
             f"{ACCUMULATED_STALL_SECS:.2f}s of stalls; the buffer is "
             f"holding the delay instead of recovering it")


class TestTheDirectPlaneOverTheSameLinks:
    """One frame per datagram, over the links people actually have.

    What the direct plane changes about a shaped path is the shape of what
    crosses it: twice as many packets, each a little over half the size, and
    a lost one costs one frame rather than two. The sentence has to survive
    the same Wi-Fi and LTE it survives on the mesh plane, which is what these
    measure; the direct path's own speed is not visible here, because a
    profile shapes the link and not the transport.
    """

    def test_wifi_carries_the_sentence_one_frame_at_a_time(self, peer_factory,
                                                           monkeypatch):
        call = _call_over(peer_factory, monkeypatch, HOME_WIFI, seed=11,
                          direct=True)

        assert call.correlation >= WIFI_CORRELATION
        assert call.words_in_order == list(speech.WORD_PITCHES_HZ)
        assert abs(call.drift_secs) <= DRIFT_TOLERANCE_SECS

    def test_mobile_carries_the_sentence_one_frame_at_a_time(self,
                                                             peer_factory,
                                                             monkeypatch):
        call = _call_over(peer_factory, monkeypatch, MOBILE_LTE, seed=12,
                          direct=True)

        assert call.correlation >= MOBILE_CORRELATION
        assert call.words_in_order == list(speech.WORD_PITCHES_HZ)
        assert abs(call.drift_secs) <= DRIFT_TOLERANCE_SECS

    def test_a_frame_travels_on_its_own(self, peer_factory, monkeypatch):
        """The plane's own claim, measured where it shows: on the mesh two
        frames share a packet and a loss takes both, here neither happens.

        Counted rather than compared against a second call, because the
        packets the path carried are the direct evidence and a second call
        would only be the same sentence again.
        """
        call = _call_over(peer_factory, monkeypatch, HOME_FIBRE, seed=13,
                          direct=True)
        frames = len(speech.sentence()) // (speech.SAMPLE_RATE
                                            * VOICE_FRAME_MS // 1000 * 2)

        assert call.path.delivered + call.path.dropped >= frames * 0.8, \
            "a bundle was still going out as one packet"
        assert call.words_in_order == list(speech.WORD_PITCHES_HZ)
