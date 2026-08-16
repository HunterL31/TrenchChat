"""
Named link profiles for the dev test environment's connection shaper.

Each profile describes one kind of link a real TrenchChat client might run
over -- a LoRa radio, a 1200-baud packet link, a satellite hop -- reduced to
the four numbers the shaper needs to reproduce it on a loopback socket.
"""

from dataclasses import dataclass, replace

LORA_BANDWIDTH_HZ = 125_000
LORA_CODING_RATE = 5

CUSTOM_PROFILE_NAME = "custom"
DEFAULT_PROFILE_NAME = "broadband"


def lora_bitrate(spreading_factor: int, bandwidth_hz: int = LORA_BANDWIDTH_HZ,
                 coding_rate: int = LORA_CODING_RATE) -> int:
    """On-air LoRa bitrate in bps, by the same formula RNodeInterface uses."""
    symbol_time = (2 ** spreading_factor) / (bandwidth_hz / 1000)
    return round(spreading_factor * ((4.0 / coding_rate) / symbol_time) * 1000)


@dataclass(frozen=True)
class LinkProfile:
    """One simulated link: a bitrate cap plus delay, jitter and frame loss."""

    name: str
    label: str
    description: str
    bitrate_bps: int = 0
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0

    @property
    def shaped(self) -> bool:
        """Whether this profile changes the stream at all."""
        return self.bitrate_bps > 0 or self.latency_ms > 0 or self.loss_pct > 0

    def summary(self) -> str:
        """Short one-line description for the UI tooltip."""
        if not self.shaped:
            return "unshaped"
        parts = []
        if self.bitrate_bps > 0:
            parts.append(f"{self.bitrate_bps / 1000:.1f} kbps")
        if self.latency_ms > 0:
            jitter = f" ± {self.jitter_ms:g}" if self.jitter_ms > 0 else ""
            parts.append(f"{self.latency_ms:g}{jitter} ms")
        if self.loss_pct > 0:
            parts.append(f"{self.loss_pct:g}% loss")
        return " · ".join(parts)

    def as_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "description": self.description,
            "bitrate_bps": self.bitrate_bps, "latency_ms": self.latency_ms,
            "jitter_ms": self.jitter_ms, "loss_pct": self.loss_pct,
            "summary": self.summary(),
        }


LINK_PROFILES: dict[str, LinkProfile] = {
    p.name: p for p in (
        LinkProfile(
            name="broadband", label="Broadband",
            description="No shaping at all -- the environment's original behaviour.",
        ),
        LinkProfile(
            name="satellite", label="Satellite",
            description="Plenty of bandwidth, punishing round trips.",
            bitrate_bps=256_000, latency_ms=800.0, jitter_ms=40.0, loss_pct=0.5,
        ),
        LinkProfile(
            name="serial", label="Serial 9600 baud",
            description="A wired serial link, as SerialInterface defaults to.",
            bitrate_bps=9600, latency_ms=10.0, jitter_ms=2.0,
        ),
        LinkProfile(
            name="lora_fast", label="LoRa SF7 / 125 kHz",
            description="Short-range LoRa: the fastest spreading factor RNode ships.",
            bitrate_bps=lora_bitrate(7), latency_ms=60.0, jitter_ms=20.0, loss_pct=1.0,
        ),
        LinkProfile(
            name="lora_long", label="LoRa SF10 / 125 kHz",
            description="Long-range LoRa: slow, and slow enough to hurt.",
            bitrate_bps=lora_bitrate(10), latency_ms=120.0, jitter_ms=40.0, loss_pct=3.0,
        ),
        LinkProfile(
            name="packet_radio", label="Packet radio (AX.25 1200)",
            description="1200-baud VHF packet, with the collisions to match.",
            bitrate_bps=1200, latency_ms=300.0, jitter_ms=100.0, loss_pct=5.0,
        ),
        LinkProfile(
            name="lossy", label="Flaky link",
            description="Fast but unreliable -- exercises retry, hints and sync.",
            bitrate_bps=62_500, latency_ms=250.0, jitter_ms=150.0, loss_pct=15.0,
        ),
        LinkProfile(
            name=CUSTOM_PROFILE_NAME, label="Custom...",
            description="Hand-set bitrate, latency, jitter and loss.",
            bitrate_bps=lora_bitrate(7), latency_ms=100.0, jitter_ms=25.0, loss_pct=2.0,
        ),
    )
}

DEFAULT_PROFILE = LINK_PROFILES[DEFAULT_PROFILE_NAME]


def resolve(name: str, bitrate_bps: int | None = None, latency_ms: float | None = None,
            jitter_ms: float | None = None, loss_pct: float | None = None) -> LinkProfile:
    """Look up a profile by name, applying any caller-supplied overrides."""
    base = LINK_PROFILES.get(name)
    if base is None:
        raise ValueError(f"unknown link profile: {name}")

    overrides = {}
    if bitrate_bps is not None:
        overrides["bitrate_bps"] = max(0, int(bitrate_bps))
    if latency_ms is not None:
        overrides["latency_ms"] = max(0.0, float(latency_ms))
    if jitter_ms is not None:
        overrides["jitter_ms"] = max(0.0, float(jitter_ms))
    if loss_pct is not None:
        overrides["loss_pct"] = min(100.0, max(0.0, float(loss_pct)))
    return replace(base, **overrides) if overrides else base
