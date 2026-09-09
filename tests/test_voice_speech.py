"""
A spoken sentence, transmitted and then measured where a listener hears it.

Every other voice test sends placeholder bytes: they prove frames move,
not that speech survives the move. These send a real utterance
(tests/speech.py: four words at falling pitches, a fricative onset, a
breath pause) and ask what came out the far end.

Two layers, for two different jobs:

- Through the codec and the wire, with no clock in it. Deterministic, so
  the thresholds are tight: word for word, pitch for pitch, onset for
  onset, and an envelope correlation against what was spoken.
- Between two peers in real time, microphone to speaker. The whole
  production path runs: the voice-activity gate, the encoder, the packet
  format, VoiceManager, the transport, the jitter buffer, the decoder,
  the mixer, and a playback device clocked at 20 ms. Timing wobble is
  real here, so the assertions are about content, which words arrived, in
  what order, and whether the pauses stayed pauses.
"""

import sys
import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("opuslib")

import numpy as np                                               # noqa: E402

from tests import speech                                        # noqa: E402
from tests.fake_audio import FakeAudioDevices                    # noqa: E402
from tests.helpers import wait_for, wait_for_roster              # noqa: E402
from tests.test_voice import (                                   # noqa: E402
    _setup_invite_channel, _setup_open_channel,
)
from trenchchat.network.voice_wire import (                      # noqa: E402
    VOICE_FRAME_MS, VOICE_MAX_FRAME_BYTES,
    bundle_frames, pack_audio, unpack_audio,
)

try:
    from trenchchat.core.audio.codec import OpusCodec            # noqa: E402
except Exception as error:  # no system libopus on this machine
    pytest.skip(f"opus codec unavailable: {error}", allow_module_level=True)

# A clean path should track the spoken envelope almost exactly; loss costs
# some of that. Anything that is not this sentence scores far below both:
# a reversed reading of it measures ~0.27, noise ~0.02.
CLEAN_CORRELATION = 0.90
LOSSY_CORRELATION = 0.80
IMPOSTOR_CORRELATION = 0.50
REALTIME_CORRELATION = 0.85

PITCH_TOLERANCE_HZ = 3.0
ONSET_TOLERANCE_SECS = 0.06
# Real time adds the jitter buffer's fill depth and two 20 ms device
# clocks that are not in step with each other.
REALTIME_GAP_TOLERANCE_SECS = 0.12
# Concealment and the codec's own noise floor live well below the words.
QUIET_PAUSE_DB = -30.0

_DRAIN_POLL_SECS = 0.15
_HUM_SECS = 4.0
_SCATTERED_LOSS_EVERY = 20   # one frame in twenty
_LOST_WORD = 2               # the word a burst of loss swallows whole


def _transmit(pcm: bytes, *, bitrate: int = 16000,
              lose: frozenset = frozenset()) -> bytes:
    """Encode a signal, put it on the wire, take it off, decode it.

    Frames listed in `lose` never arrive, so the decoder conceals them the
    way the playout thread asks it to for a gap in the jitter buffer.
    """
    encoder, decoder = OpusCodec(bitrate=bitrate), OpusCodec(bitrate=bitrate)
    encoded = [encoder.encode(frame) for frame in speech.frames(pcm)]
    heard: list[bytes] = []
    index = 0
    for bundle in bundle_frames(encoded):
        _seq, arrived = unpack_audio(pack_audio(index, bundle))
        for frame in arrived:
            heard.append(decoder.decode(None if index in lose else frame))
            index += 1
    return b"".join(heard)


def _pitches(pcm: bytes) -> list[float]:
    return [word["pitch_hz"] for word in speech.words(pcm)]


def _gaps(spoken_words: list[dict]) -> list[float]:
    """Silence between consecutive words, which survives a path delay."""
    return [later["start_secs"] - earlier["end_secs"]
            for earlier, later in zip(spoken_words, spoken_words[1:])]


def _rms(signal: np.ndarray) -> float:
    return float(np.sqrt((signal ** 2).mean())) if signal.size else 0.0


def _loudest_and_quietest(pcm: bytes) -> tuple:
    """The loudest and the quietest still-audible tenth-second of audio."""
    signal = speech.samples(pcm)
    width = speech.SAMPLE_RATE // 10
    windows = [signal[i:i + width]
               for i in range(0, signal.size - width, width)]
    floor = max(_rms(window) for window in windows) * 0.1
    audible = [window for window in windows if _rms(window) >= floor]
    return max(audible, key=_rms), min(audible, key=_rms)


def _frame_at(secs: float) -> int:
    """Which 20 ms capture frame a moment in the sentence falls in."""
    return int(secs * 1000 / VOICE_FRAME_MS)


def _still_sending(sent: list) -> bool:
    before = len(sent)
    time.sleep(_DRAIN_POLL_SECS)
    return len(sent) != before


# ---------------------------------------------------------------------------
# Through the codec and the wire
# ---------------------------------------------------------------------------

class TestSentenceThroughTheCodecAndWire:
    def test_the_sentence_arrives_word_for_word(self):
        spoken = speech.sentence()
        heard = _transmit(spoken)

        said, received = speech.words(spoken), speech.words(heard)
        assert len(said) == len(speech.WORD_PITCHES_HZ), \
            "the reference sentence is not four words"
        assert len(received) == len(said), \
            f"{len(received)} words arrived, {len(said)} were spoken"
        for expected, word, original in zip(speech.WORD_PITCHES_HZ,
                                            received, said):
            assert abs(word["pitch_hz"] - expected) <= PITCH_TOLERANCE_HZ, \
                f"word arrived at {word['pitch_hz']:.0f} Hz, spoken at {expected:.0f}"
            assert abs(word["start_secs"] - original["start_secs"]) \
                <= ONSET_TOLERANCE_SECS
            assert abs((word["end_secs"] - word["start_secs"])
                       - (original["end_secs"] - original["start_secs"])) \
                <= ONSET_TOLERANCE_SECS
        assert speech.band_envelope_correlation(spoken, heard) >= \
            CLEAN_CORRELATION

    def test_the_pauses_do_not_fill_with_noise(self):
        heard = _transmit(speech.sentence())

        assert speech.silence_floor_db(heard, speech.words(heard)) <= \
            QUIET_PAUSE_DB

    @pytest.mark.parametrize("impostor", ["silence", "noise", "backwards"])
    def test_the_measure_rejects_audio_that_is_not_the_sentence(self,
                                                                impostor):
        """A correlation this test suite trusts has to be able to fail.

        Silence, noise and the same sentence read backwards all reach the
        listener as audio; none of them is what was said.
        """
        spoken = speech.sentence()
        if impostor == "silence":
            other = bytes(len(spoken))
        elif impostor == "noise":
            rng = np.random.default_rng(11)
            other = rng.normal(0, 3000, len(spoken) // 2).astype(
                np.int16).tobytes()
        else:
            other = speech.samples(spoken)[::-1].astype(np.int16).tobytes()

        assert speech.band_envelope_correlation(spoken, other) < \
            IMPOSTOR_CORRELATION

    def test_scattered_loss_stays_intelligible_and_in_time(self):
        spoken = speech.sentence()
        total = len(speech.frames(spoken))
        heard = _transmit(
            spoken, lose=frozenset(range(3, total, _SCATTERED_LOSS_EVERY)))

        assert _pitches(heard) == pytest.approx(
            list(speech.WORD_PITCHES_HZ), abs=PITCH_TOLERANCE_HZ)
        assert speech.band_envelope_correlation(spoken, heard) >= \
            LOSSY_CORRELATION

    def test_a_lost_burst_costs_its_word_and_nothing_after_it(self):
        """Concealment emits a whole frame for a lost one, so loss costs
        audio and never alignment. Losing every frame of one word must
        cost exactly that word: the next one still has to arrive at the
        moment, and the pitch, it was spoken at.
        """
        spoken = speech.sentence()
        covered = speech.words(spoken)[_LOST_WORD]
        heard = _transmit(spoken, lose=frozenset(range(
            _frame_at(covered["start_secs"]), _frame_at(covered["end_secs"]))))

        survivors = [round(pitch)
                     for index, pitch in enumerate(speech.WORD_PITCHES_HZ)
                     if index != _LOST_WORD]
        received = speech.words(heard)
        assert [round(word["pitch_hz"]) for word in received] == survivors, \
            "the burst took more than the word it covered"
        last_spoken, last_heard = speech.words(spoken)[-1], received[-1]
        assert abs(last_heard["start_secs"] - last_spoken["start_secs"]) \
            <= ONSET_TOLERANCE_SECS

    def test_a_fricative_at_the_top_bitrate_fits_the_wire(self):
        """Regression: the sentence's opening fricative encoded to 306
        bytes at 64 kbps, past the 255-byte per-frame length field, and
        pack_audio then rejected the whole bundle."""
        frames = [OpusCodec(bitrate=64000).encode(frame)
                  for frame in speech.frames(speech.sentence())]

        assert all(0 < len(frame) <= VOICE_MAX_FRAME_BYTES
                   for frame in frames)
        assert speech.band_envelope_correlation(
            speech.sentence(), _transmit(speech.sentence(), bitrate=64000)) \
            >= CLEAN_CORRELATION


# ---------------------------------------------------------------------------
# The transmit gate, driven by a real microphone signal
# ---------------------------------------------------------------------------

class TestTransmitGate:
    def test_the_gate_closing_leaves_no_gap_in_the_sequence(self,
                                                            monkeypatch):
        """Regression: a pause closes the voice-activity gate, and the
        gate used to discard whatever half-bundle it had already encoded.
        Those frames held sequence numbers, so the listener saw a hole,
        counted it as packet loss and concealed over the end of a word.
        """
        from trenchchat.core.audio.engine import AudioPipeline

        sent: list[tuple[int, list[bytes]]] = []
        devices = FakeAudioDevices(speech.sentence())
        monkeypatch.setitem(sys.modules, "sounddevice", devices)
        pipeline = AudioPipeline(None,
                                 lambda seq, frames: sent.append((seq, frames)),
                                 lambda speaking: None)
        pipeline.start()
        try:
            microphone = devices.capture_of(pipeline)
            microphone.speak()
            assert wait_for(lambda: microphone.exhausted, timeout=15.0,
                            msg="microphone drained")
            assert wait_for(lambda: sent and not _still_sending(sent),
                            interval=0.1, msg="transmission finished")
        finally:
            pipeline.stop()

        expected = sent[0][0]
        for seq, frames in sent:
            assert seq == expected, \
                f"packet at seq {seq} follows a hole ending at {expected}"
            expected = seq + len(frames)


# ---------------------------------------------------------------------------
# Microphone to speaker, between two peers, in real time
# ---------------------------------------------------------------------------

def _join_with_devices(peer, ch_hash: str, monkeypatch,
                       capture: bytes = b"") -> tuple:
    """Join voice with a scripted microphone and a recording speaker.

    The pipeline imports sounddevice when it starts, so the fake devices
    go into sys.modules immediately before the join, which is what lets
    each peer in a test have its own.
    """
    devices = FakeAudioDevices(capture)
    monkeypatch.setitem(sys.modules, "sounddevice", devices)
    # The join blip is mixed into playout like any other sound, and these
    # tests measure what a voice put there; cues have their own tests.
    peer.config.voice_event_sounds = False
    assert peer.voice_mgr.join_voice(ch_hash) is True
    pipeline = peer.voice_mgr.audio_pipeline
    assert pipeline is not None, "voice joined without an audio pipeline"
    return devices.capture_of(pipeline), devices.playback_of(pipeline)


def _wait_for_stream(listener, speaker_hex: str) -> None:
    assert wait_for(
        lambda: speaker_hex in listener.voice_transport.connected_peers(),
        msg=f"link to {speaker_hex[:12]}…",
    )


def _wait_until_spoken(microphone, listener, speaker_hex: str) -> None:
    """Wait for the microphone to run dry, then for delivery to stop."""
    assert wait_for(lambda: microphone.exhausted, timeout=15.0,
                    msg="microphone drained")
    heard = -1

    def settled() -> bool:
        nonlocal heard
        before, heard = heard, listener.voice_mgr.frame_stats()[
            "rx_frames"].get(speaker_hex, 0)
        return heard == before

    assert wait_for(settled, interval=_DRAIN_POLL_SECS,
                    msg="delivery settled")


class TestSentenceBetweenTwoPeers:
    def test_the_sentence_reaches_the_other_peers_speaker(self, peer_factory,
                                                          monkeypatch):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        spoken = speech.sentence()
        microphone, _ = _join_with_devices(alice, ch_hash, monkeypatch, spoken)
        assert wait_for_roster(bob, ch_hash, alice.identity.hash_hex)
        _, listener = _join_with_devices(bob, ch_hash, monkeypatch)
        _wait_for_stream(alice, bob.identity.hash_hex)

        microphone.speak()
        _wait_until_spoken(microphone, bob, alice.identity.hash_hex)
        quality = bob.voice_mgr.frame_stats()["rx_quality"][
            alice.identity.hash_hex]
        bob.voice_mgr.leave_voice()
        heard = listener.played()

        received = speech.words(heard)
        assert [round(word["pitch_hz"]) for word in received] == \
            [round(pitch) for pitch in speech.WORD_PITCHES_HZ], \
            "the four words did not arrive at the pitches they were spoken at"
        for spoken_gap, heard_gap in zip(_gaps(speech.words(spoken)),
                                         _gaps(received)):
            assert abs(heard_gap - spoken_gap) <= REALTIME_GAP_TOLERANCE_SECS
        assert speech.band_envelope_correlation(spoken, heard) >= \
            REALTIME_CORRELATION
        assert quality["loss_pct"] == 0.0

    def test_going_quiet_mid_sentence_stops_what_the_listener_hears(
            self, peer_factory, monkeypatch):
        """Muting is not cosmetic: nothing spoken after it may arrive."""
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        spoken = speech.sentence()
        pause_starts = speech.words(spoken)[1]["end_secs"]
        microphone, _ = _join_with_devices(alice, ch_hash, monkeypatch, spoken)
        assert wait_for_roster(bob, ch_hash, alice.identity.hash_hex)
        _, listener = _join_with_devices(bob, ch_hash, monkeypatch)
        _wait_for_stream(alice, bob.identity.hash_hex)

        microphone.speak()
        assert wait_for(
            lambda: microphone.position_secs >= pause_starts + 0.1,
            interval=0.02, msg="the second word finished")
        alice.voice_mgr.set_muted(True)
        _wait_until_spoken(microphone, bob, alice.identity.hash_hex)
        bob.voice_mgr.leave_voice()

        heard = [round(pitch) for pitch in _pitches(listener.played())]
        assert heard == [round(pitch)
                         for pitch in speech.WORD_PITCHES_HZ[:2]], \
            f"words heard after the mute: {heard}"

    def test_two_speakers_arrive_mixed_in_one_stream(self, peer_factory,
                                                     monkeypatch):
        """A held note under a sentence: the listener must hear both, so
        the pauses in the sentence are covered by the other voice rather
        than silent, and the words are louder than the pauses because two
        streams are summed there."""
        peers, ch_hash = _setup_open_channel(
            peer_factory, names=("alice", "bob", "carol"))
        alice, bob, carol = peers
        spoken = speech.sentence()
        sentence_mic, _ = _join_with_devices(alice, ch_hash, monkeypatch,
                                             spoken)
        hum_mic, _ = _join_with_devices(bob, ch_hash, monkeypatch,
                                        speech.hum(_HUM_SECS))
        assert wait_for_roster(carol, ch_hash, alice.identity.hash_hex)
        _, listener = _join_with_devices(carol, ch_hash, monkeypatch)
        _wait_for_stream(carol, alice.identity.hash_hex)
        _wait_for_stream(carol, bob.identity.hash_hex)

        hum_mic.speak()
        sentence_mic.speak()
        _wait_until_spoken(sentence_mic, carol, alice.identity.hash_hex)
        carol.voice_mgr.leave_voice()

        heard = listener.played()
        runs = speech.words(heard)
        assert len(runs) == 1, \
            f"the held note left {len(runs)} gaps in what carol heard"
        loud, quiet = _loudest_and_quietest(heard)
        assert speech.pitch_hz(quiet) == pytest.approx(
            speech.HUM_PITCH_HZ, abs=PITCH_TOLERANCE_HZ), \
            "the sentence's pauses are not the other speaker's note"
        assert _rms(loud) > _rms(quiet) * 1.5, \
            "the two streams were not summed"
