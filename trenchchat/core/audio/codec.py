"""
Audio codec abstraction and the Opus implementation.

Codecs are duck-typed: encode() takes one frame of 16-bit mono PCM and
returns encoded bytes; decode() takes encoded bytes, or None to invoke
packet-loss concealment. Decoders are stateful, so receivers keep one
codec instance per remote sender. Importing this module requires the
opuslib binding (and a system libopus); callers go through
trenchchat.core.audio.audio_available() first.
"""

from trenchchat.core.audio.libpath import ensure_voice_libs_findable

# opuslib runs find_library at import, so the search hook must be in
# place before the import happens — local import first, deliberately.
ensure_voice_libs_findable()

import opuslib

from trenchchat.network.voice_wire import CODEC_OPUS

OPUS_SAMPLE_RATE = 48000
OPUS_FRAME_MS = 20
OPUS_CHANNELS = 1
OPUS_DEFAULT_BITRATE = 16000

FRAME_SAMPLES = OPUS_SAMPLE_RATE * OPUS_FRAME_MS // 1000
FRAME_PCM_BYTES = FRAME_SAMPLES * 2  # int16 mono


class OpusCodec:
    codec_id = CODEC_OPUS
    frame_ms = OPUS_FRAME_MS
    sample_rate = OPUS_SAMPLE_RATE

    def __init__(self, bitrate: int = OPUS_DEFAULT_BITRATE):
        self._encoder = opuslib.Encoder(
            OPUS_SAMPLE_RATE, OPUS_CHANNELS, opuslib.APPLICATION_VOIP)
        self._encoder.bitrate = bitrate
        self._decoder = opuslib.Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS)

    def encode(self, pcm: bytes) -> bytes:
        return self._encoder.encode(pcm, FRAME_SAMPLES)

    def decode(self, data: bytes | None) -> bytes:
        if data is None:
            return self._decoder.decode(b"", FRAME_SAMPLES, decode_fec=False)
        return self._decoder.decode(data, FRAME_SAMPLES, decode_fec=False)
