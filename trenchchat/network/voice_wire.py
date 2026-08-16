"""
Wire format for voice frame packets sent over RNS Links.

This is the link-plane counterpart of trenchchat/core/protocol.py: the
single place voice packet types and layouts are defined, deliberately
dependency-free. Packets are fixed binary, not msgpack — every packet must
fit a single link MDU (431 bytes on rns 1.4.2) and audio framing overhead
matters at 25 packets per second per stream.

Layouts:
    VP_HELLO  : u8 type | u8 version | u8 codec_id | 16B channel_hash
    VP_ACCEPT : u8 type | u8 version
    VP_AUDIO  : u8 type | u16be seq | u8 frame_count |
                frame_count x (u8 len | len bytes of encoded audio)
    VP_BYE    : u8 type

seq is the sequence number of the first frame in the bundle; frame i has
sequence (seq + i) mod 2^16, and its timestamp is seq * VOICE_FRAME_MS.
"""

import struct

VOICE_WIRE_VERSION = 1

VP_HELLO = 0x01
VP_ACCEPT = 0x02
VP_AUDIO = 0x03
VP_BYE = 0x04

CODEC_OPUS = 0x00

VOICE_FRAME_MS = 20
VOICE_FRAMES_PER_PACKET = 2
VOICE_MAX_FRAME_BYTES = 200
VOICE_MAX_FRAMES_PER_PACKET = 8
CHANNEL_HASH_LEN = 16

SEQ_MODULUS = 1 << 16


def pack_hello(channel_hash: bytes, codec_id: int = CODEC_OPUS) -> bytes:
    if len(channel_hash) != CHANNEL_HASH_LEN:
        raise ValueError("channel hash must be 16 bytes")
    return struct.pack("!BBB", VP_HELLO, VOICE_WIRE_VERSION, codec_id) + channel_hash


def unpack_hello(payload: bytes) -> tuple[int, int, bytes]:
    """Returns (version, codec_id, channel_hash). Raises ValueError."""
    if len(payload) != 3 + CHANNEL_HASH_LEN or payload[0] != VP_HELLO:
        raise ValueError("malformed hello packet")
    _, version, codec_id = struct.unpack("!BBB", payload[:3])
    return version, codec_id, payload[3:]


def pack_accept() -> bytes:
    return struct.pack("!BB", VP_ACCEPT, VOICE_WIRE_VERSION)


def unpack_accept(payload: bytes) -> int:
    """Returns the version. Raises ValueError."""
    if len(payload) != 2 or payload[0] != VP_ACCEPT:
        raise ValueError("malformed accept packet")
    return payload[1]


def pack_bye() -> bytes:
    return struct.pack("!B", VP_BYE)


def pack_audio(seq: int, frames: list[bytes]) -> bytes:
    if not frames or len(frames) > VOICE_MAX_FRAMES_PER_PACKET:
        raise ValueError("frame count out of range")
    parts = [struct.pack("!BHB", VP_AUDIO, seq % SEQ_MODULUS, len(frames))]
    for frame in frames:
        if not frame or len(frame) > VOICE_MAX_FRAME_BYTES:
            raise ValueError("frame size out of range")
        parts.append(struct.pack("!B", len(frame)))
        parts.append(frame)
    return b"".join(parts)


def unpack_audio(payload: bytes) -> tuple[int, list[bytes]]:
    """Returns (seq, frames). Raises ValueError on malformed input."""
    if len(payload) < 4 or payload[0] != VP_AUDIO:
        raise ValueError("malformed audio packet")
    _, seq, frame_count = struct.unpack("!BHB", payload[:4])
    if not 0 < frame_count <= VOICE_MAX_FRAMES_PER_PACKET:
        raise ValueError("frame count out of range")
    frames: list[bytes] = []
    offset = 4
    for _ in range(frame_count):
        if offset >= len(payload):
            raise ValueError("truncated audio packet")
        frame_len = payload[offset]
        offset += 1
        if frame_len == 0 or frame_len > VOICE_MAX_FRAME_BYTES:
            raise ValueError("frame size out of range")
        frame = payload[offset:offset + frame_len]
        if len(frame) != frame_len:
            raise ValueError("truncated audio packet")
        frames.append(frame)
        offset += frame_len
    if offset != len(payload):
        raise ValueError("trailing bytes in audio packet")
    return seq, frames


def packet_type(payload: bytes) -> int:
    if not payload:
        raise ValueError("empty packet")
    return payload[0]
