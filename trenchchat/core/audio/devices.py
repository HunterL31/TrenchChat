"""
PortAudio device enumeration and resolution for the voice pipeline.

Devices are stored in config by name (None means the system default).
Resolution happens at pipeline start, so a device that has been unplugged
falls back to the default instead of erroring the voice session.
"""

import RNS


def list_devices() -> dict:
    """Input/output device names, or empty lists with a reason when the
    audio stack is unavailable on this machine."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
    except Exception as e:
        return {"available": False, "reason": str(e),
                "input": [], "output": []}
    inputs: list[str] = []
    outputs: list[str] = []
    for dev in devices:
        name = dev.get("name", "")
        if not name:
            continue
        if dev.get("max_input_channels", 0) > 0 and name not in inputs:
            inputs.append(name)
        if dev.get("max_output_channels", 0) > 0 and name not in outputs:
            outputs.append(name)
    return {"available": True, "reason": "", "input": inputs,
            "output": outputs}


def resolve_device(configured: str | int | None, kind: str) -> int | None:
    """Map a configured device to a PortAudio index sounddevice accepts.

    kind is "input" or "output". None means the system default; so does a
    name or index that no longer matches a present device with channels in
    that direction — falling back keeps voice working after an unplug.
    """
    if configured is None:
        return None
    try:
        import sounddevice as sd
        devices = sd.query_devices()
    except Exception as e:
        RNS.log(f"TrenchChat [voice]: device query failed ({e}); "
                f"using default {kind}", RNS.LOG_WARNING)
        return None
    channels_key = f"max_{kind}_channels"
    if isinstance(configured, int):
        if 0 <= configured < len(devices) and \
                devices[configured].get(channels_key, 0) > 0:
            return configured
    else:
        for index, dev in enumerate(devices):
            if dev.get("name", "") == configured and \
                    dev.get(channels_key, 0) > 0:
                return index
    RNS.log(f"TrenchChat [voice]: configured {kind} device "
            f"{configured!r} not found; using default", RNS.LOG_WARNING)
    return None
