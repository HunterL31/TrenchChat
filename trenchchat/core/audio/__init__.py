"""
Audio layer for voice chat: codec, jitter buffer, mixer, and pipelines.

Nothing outside this package imports sounddevice, numpy, or opuslib; the
probes here confine missing system libraries (libopus, libportaudio, no
sound device) to a reported reason instead of an import error elsewhere.
"""


def audio_available() -> tuple[bool, str]:
    """Whether full capture/encode/playback support is importable."""
    try:
        import sounddevice  # noqa: F401
    except Exception as e:
        return False, f"sounddevice unavailable: {e}"
    try:
        import numpy  # noqa: F401
    except Exception as e:
        return False, f"numpy unavailable: {e}"
    try:
        from trenchchat.core.audio.codec import OpusCodec  # noqa: F401
    except Exception as e:
        return False, f"opus codec unavailable: {e}"
    return True, ""


def create_pipeline(config, on_encoded, on_speaking_self):
    """Build the device pipeline, or None (with a logged reason) if the
    audio stack isn't available on this machine."""
    import RNS

    available, reason = audio_available()
    if not available:
        RNS.log(f"TrenchChat [voice]: {reason}", RNS.LOG_WARNING)
        return None
    from trenchchat.core.audio.engine import AudioPipeline
    return AudioPipeline(config, on_encoded, on_speaking_self)
