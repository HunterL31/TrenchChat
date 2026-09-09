"""
A spoken sentence for the voice tests, and the measurements taken on it
at the receiving end.

The sentence is synthesised rather than loaded from a recording: it stays
byte-identical on every machine, needs no binary asset in the repo, and
each word is voiced at its own fundamental, so a listener can be asked
which words arrived and in what order rather than only whether energy
arrived. It is built the way speech is: a harmonic glottal source at a
falling pitch, shaped by three formants that move per vowel, with a noise
burst opening two of the words and real silence between them.

The measurements are the ones that survive a codec. Opus is not
waveform-preserving, so sample-by-sample comparison says nothing; what
carries intelligibility is the amplitude envelope inside each frequency
band, which is what band_envelope_correlation scores (the same modulation
transfer that underlies the Speech Transmission Index). words() and
pitch_hz() then answer the coarser question: did the four words arrive,
in order, at the pitches they were spoken at.
"""

import math

import numpy as np

SAMPLE_RATE = 48000
FRAME_SAMPLES = 960
FRAME_BYTES = FRAME_SAMPLES * 2

# Vowel formants (F1, F2, F3), roughly the standard male values.
_FORMANTS = {
    "a": (730.0, 1090.0, 2440.0),
    "e": (530.0, 1840.0, 2480.0),
    "i": (270.0, 2290.0, 3010.0),
    "o": (570.0, 840.0, 2410.0),
    "u": (300.0, 870.0, 2240.0),
}
_FORMANT_BANDWIDTH_HZ = 90.0
_HARMONICS = 40
_RAMP_SECS = 0.02
_PEAK = 0.6
_ONSET_NOISE_SECS = 0.025
_NOISE_SEED = 20240

# One word per entry: its vowels, its fundamental, and whether it opens on
# a fricative burst. The pitches fall across the sentence the way a
# statement's do, and being distinct is what makes word order measurable.
_WORDS = (
    (("o", 0.10), ("a", 0.12), 190.0, True),
    (("e", 0.10), ("i", 0.12), 155.0, False),
    (("a", 0.10), ("u", 0.12), 130.0, True),
    (("i", 0.10), ("o", 0.12), 110.0, False),
)
_GAP_SECS = 0.18
_BREATH_SECS = 0.5     # the longer pause, after the second word
_BREATH_AFTER_WORD = 2
_TAIL_SECS = 0.08

WORD_PITCHES_HZ = tuple(word[2] for word in _WORDS)

# The second speaker's note: outside the sentence's pitches, and quieter,
# so a mix of the two can be told apart by pitch and by level.
HUM_PITCH_HZ = 240.0
_HUM_PEAK = 0.35

# Octave bands for the envelope correlation. They stop at 4 kHz: at the
# mesh default bitrate Opus spends nothing above it, so a 4-8 kHz band
# scores the codec's bandwidth choice rather than whether speech arrived.
_BANDS_HZ = ((125, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000))
_ENVELOPE_WINDOW_SECS = 0.01
# Alignment resolution: finer than this buys nothing against a 10 ms
# envelope window, and coarser starts to blur the correlation.
_ALIGN_STEP_SAMPLES = 240
_SEGMENT_FLOOR_RATIO = 0.06
_MIN_WORD_SECS = 0.06
_PITCH_RANGE_HZ = (80, 300)


def sentence() -> bytes:
    """The reference utterance as 16-bit mono PCM at 48 kHz."""
    rng = np.random.default_rng(_NOISE_SEED)
    parts: list[np.ndarray] = []
    for index, (first, second, f0, onset) in enumerate(_WORDS, start=1):
        parts.append(_word((first, second), f0, onset, rng))
        if index == _BREATH_AFTER_WORD:
            parts.append(_silence(_BREATH_SECS))
        elif index < len(_WORDS):
            parts.append(_silence(_GAP_SECS))
    parts.append(_silence(_TAIL_SECS))
    signal = np.concatenate(parts)
    return (np.clip(signal, -1.0, 1.0) * _PEAK * 32767).astype(np.int16).tobytes()


def hum(secs: float, f0: float = HUM_PITCH_HZ) -> bytes:
    """A second speaker holding one steady vowel, quieter than the sentence.

    Its pitch is not one of the sentence's, so a listener mixing both can
    be asked which voice it is hearing at any moment.
    """
    voiced, _ = _vowel("e", f0, secs, 0.0)
    voiced *= _ramp(voiced.size)
    peak = np.abs(voiced).max()
    if peak > 0:
        voiced = voiced / peak
    return (voiced * _HUM_PEAK * 32767).astype(np.int16).tobytes()


def frames(pcm: bytes) -> list[bytes]:
    """Split PCM into whole 20 ms capture frames, dropping any remainder."""
    count = len(pcm) // FRAME_BYTES
    return [pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES] for i in range(count)]


def samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(float)


def words(pcm: bytes) -> list[dict]:
    """Detect spoken words: contiguous runs of audible frames.

    Each entry is {start_secs, end_secs, pitch_hz}, with the pitch taken
    from the middle of the run so a fricative onset cannot pull it.
    """
    signal = samples(pcm)
    if signal.size == 0:
        return []
    energy = _frame_rms(signal)
    if energy.max() <= 0.0:
        return []
    audible = energy >= energy.max() * _SEGMENT_FLOOR_RATIO
    found: list[dict] = []
    start: int | None = None
    for index, loud in enumerate(np.append(audible, False)):
        if loud and start is None:
            start = index
        elif not loud and start is not None:
            if (index - start) * FRAME_SAMPLES >= _MIN_WORD_SECS * SAMPLE_RATE:
                found.append(_describe(signal, start, index))
            start = None
    return found


def pitch_hz(signal: np.ndarray) -> float:
    """Fundamental of a voiced segment by autocorrelation, 0.0 if too short."""
    low, high = _PITCH_RANGE_HZ
    min_lag, max_lag = int(SAMPLE_RATE / high), int(SAMPLE_RATE / low)
    if signal.size < max_lag * 2:
        return 0.0
    centred = signal - signal.mean()
    correlation = np.correlate(centred, centred, mode="full")[centred.size - 1:]
    return SAMPLE_RATE / (min_lag + int(np.argmax(correlation[min_lag:max_lag])))


def band_envelope_correlation(sent: bytes, received: bytes) -> float:
    """How well the received audio tracks what was spoken: 1.0 for a
    perfect match, around zero for audio that has nothing to do with it.

    The two signals are aligned on their overall envelope first (the codec
    adds a few ms of delay), then each octave band's envelope is
    correlated and the worst band is reported: one band collapsing is
    audible even when the rest track perfectly.
    """
    return min(band_envelope_correlations(sent, received).values())


def band_envelope_correlations(sent: bytes, received: bytes) -> dict:
    """Per-band envelope correlation, keyed by band centre in Hz."""
    spoken, heard = _aligned(samples(sent), samples(received))
    return {
        (low + high) // 2: _correlation(_envelope(_band(spoken, low, high)),
                                        _envelope(_band(heard, low, high)))
        for low, high in _BANDS_HZ
    }


def silence_floor_db(pcm: bytes, spoken_words: list[dict]) -> float:
    """Loudest gap between words, in dB relative to the words themselves.

    A codec or a concealment run that fills the pauses with noise shows up
    here and nowhere else.
    """
    signal = samples(pcm)
    if not spoken_words or signal.size == 0:
        return -120.0
    gaps = []
    previous_end = 0.0
    for word in spoken_words:
        gaps.append((previous_end, word["start_secs"]))
        previous_end = word["end_secs"]
    gaps.append((previous_end, signal.size / SAMPLE_RATE))
    loudest = 0.0
    for start, end in gaps:
        # A frame of guard at each edge: word detection is frame-quantised,
        # so the run's own attack and decay sit just outside it.
        first = int(start * SAMPLE_RATE) + FRAME_SAMPLES
        last = int(end * SAMPLE_RATE) - FRAME_SAMPLES
        span = signal[first:max(first, last)]
        if span.size >= FRAME_SAMPLES:
            loudest = max(loudest, float(np.sqrt((span ** 2).mean())))
    reference = float(np.sqrt((signal ** 2).mean()))
    if loudest <= 0.0 or reference <= 0.0:
        return -120.0
    return 20.0 * math.log10(loudest / reference)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

def _silence(secs: float) -> np.ndarray:
    return np.zeros(int(secs * SAMPLE_RATE))


def _word(vowels: tuple, f0: float, onset: bool, rng) -> np.ndarray:
    parts: list[np.ndarray] = []
    if onset:
        parts.append(_fricative(_ONSET_NOISE_SECS, rng))
    phase = 0.0
    for vowel, secs in vowels:
        voiced, phase = _vowel(vowel, f0, secs, phase)
        parts.append(voiced)
    signal = np.concatenate(parts)
    signal *= _ramp(signal.size)
    peak = np.abs(signal).max()
    return signal / peak if peak > 0 else signal


def _vowel(vowel: str, f0: float, secs: float,
           phase0: float) -> tuple[np.ndarray, float]:
    """One vowel as harmonics of f0 weighted by the vowel's formants.

    Phase carries across vowels so a word is one continuous voiced sound
    rather than two spliced ones.
    """
    count = int(secs * SAMPLE_RATE)
    phase = phase0 + 2 * math.pi * f0 * np.arange(count) / SAMPLE_RATE
    orders = np.arange(1, _HARMONICS + 1)
    orders = orders[orders * f0 < SAMPLE_RATE / 2]
    freqs = orders * f0
    gain = np.zeros_like(freqs)
    for centre in _FORMANTS[vowel]:
        gain = np.maximum(gain, _resonance(freqs, centre))
    amplitudes = (0.05 + gain) / orders
    signal = (amplitudes[:, None]
              * np.sin(orders[:, None] * phase[None, :])).sum(axis=0)
    return signal, phase0 + 2 * math.pi * f0 * secs


def _resonance(freqs: np.ndarray, centre: float) -> np.ndarray:
    return 1.0 / np.sqrt(
        1.0 + ((freqs - centre) / _FORMANT_BANDWIDTH_HZ) ** 2)


def _fricative(secs: float, rng) -> np.ndarray:
    count = int(secs * SAMPLE_RATE)
    noise = rng.normal(0.0, 0.35, count)
    return _band(noise, 3000, 9000) * np.linspace(0.3, 1.0, count)


def _ramp(count: int) -> np.ndarray:
    envelope = np.ones(count)
    edge = min(int(_RAMP_SECS * SAMPLE_RATE), count // 2)
    if edge:
        envelope[:edge] = np.linspace(0.0, 1.0, edge)
        envelope[-edge:] = np.linspace(1.0, 0.0, edge)
    return envelope


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def _describe(signal: np.ndarray, first_frame: int, last_frame: int) -> dict:
    start, end = first_frame * FRAME_SAMPLES, last_frame * FRAME_SAMPLES
    middle = signal[start + FRAME_SAMPLES // 2:end]
    return {
        "start_secs": start / SAMPLE_RATE,
        "end_secs": end / SAMPLE_RATE,
        "pitch_hz": pitch_hz(middle),
    }


def _frame_rms(signal: np.ndarray) -> np.ndarray:
    usable = signal[:signal.size - signal.size % FRAME_SAMPLES]
    if usable.size == 0:
        return np.zeros(0)
    return np.sqrt((usable.reshape(-1, FRAME_SAMPLES) ** 2).mean(axis=1))


def _band(signal: np.ndarray, low: float, high: float) -> np.ndarray:
    spectrum = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(signal.size, 1.0 / SAMPLE_RATE)
    inside = (freqs >= low) & (freqs < high)
    return np.fft.irfft(np.where(inside, spectrum, 0.0), n=signal.size)


def _envelope(signal: np.ndarray) -> np.ndarray:
    window = int(_ENVELOPE_WINDOW_SECS * SAMPLE_RATE)
    return np.convolve(np.abs(signal), np.ones(window) / window, mode="same")


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a - a.mean(), b - b.mean()
    scale = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    return float((a * b).sum() / scale) if scale > 0 else 0.0


def _aligned(sent: np.ndarray,
             received: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Trim both signals to the offset where their envelopes line up best.

    The delay is not a known constant: the codec adds a few ms, the jitter
    buffer its fill depth, and a recording taken from a live session
    starts whenever the listener joined. So the lag is found rather than
    assumed, by cross-correlating the two envelopes over their whole
    range.
    """
    return _overlap(sent, received, _lag(_envelope(sent), _envelope(received)))


def _lag(spoken: np.ndarray, heard: np.ndarray) -> int:
    """Samples by which `heard` trails `spoken`, negative if it leads."""
    step = _ALIGN_STEP_SAMPLES
    a, b = spoken[::step], heard[::step]
    a, b = a - a.mean(), b - b.mean()
    width = 1 << int(np.ceil(np.log2(a.size + b.size)))
    correlation = np.fft.irfft(
        np.fft.rfft(a, width) * np.conj(np.fft.rfft(b, width)), width)
    peak = int(np.argmax(correlation))
    if peak > width // 2:
        peak -= width
    return peak * step


def _overlap(sent: np.ndarray, received: np.ndarray,
             offset: int) -> tuple[np.ndarray, np.ndarray]:
    """The two signals with `received` shifted `offset` samples later."""
    if offset >= 0:
        sent = sent[offset:]
    else:
        received = received[-offset:]
    width = min(sent.size, received.size)
    return sent[:width], received[:width]
