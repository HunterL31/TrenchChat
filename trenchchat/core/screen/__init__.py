"""
Screen share: capture, tile encoding, and the manager over the direct plane.

Nothing outside this package imports mss; the probe here confines a missing
display or an unsupported session (Wayland) to a reported reason, the way
core/audio does for a missing sound library. Pillow and numpy are ordinary
runtime dependencies and are imported where they are used.
"""


def screen_available() -> tuple[bool, str]:
    """Whether this machine can capture a screen, and why not if it cannot."""
    from trenchchat.core.screen.capture import probe_capture

    return probe_capture()
