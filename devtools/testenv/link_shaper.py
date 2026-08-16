"""
Bandwidth, latency and loss shaper for the dev test environment.

Each tester dials a listener here instead of dialling the hub directly, so
one client's link can be made to behave like a LoRa radio or a 1200-baud
packet link while the others stay fast. Reticulum itself has no latency or
loss setting, and its `bitrate` config key only paces announces rather than
throttling a socket, so the shaping has to happen on the wire.

The stream is split on RNS's HDLC flag byte and shaped a frame at a time,
which keeps whole packets intact: dropping a frame loses exactly one packet,
the way a real radio would, instead of corrupting the stream.
"""

import asyncio
import random
import threading

from link_profiles import DEFAULT_PROFILE, LinkProfile

HDLC_FLAG = 0x7E

READ_CHUNK_BYTES = 65536
FRAME_QUEUE_MAX = 1024

# A frame this long without a closing flag means the stream isn't framed the
# way we expect; forward it rather than stalling the link forever.
MAX_PARTIAL_FRAME_BYTES = 262144


def split_frames(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split an HDLC stream into whole frames plus the unconsumed tail.

    Frames are delimited by 0x7E and RNS escapes every 0x7E in the payload,
    so each flag byte is always a boundary. Frames are returned with their
    delimiters intact, so concatenating them reproduces the input exactly.
    """
    frames = []
    start = 0
    while True:
        opening = buf.find(HDLC_FLAG, start)
        if opening < 0:
            break
        closing = buf.find(HDLC_FLAG, opening + 1)
        if closing < 0:
            break
        frames.append(buf[start:closing + 1])
        start = closing + 1
    return frames, buf[start:]


def schedule(now: float, channel_free_at: float, frame_len: int,
             profile: LinkProfile, jitter_frac: float = 0.0) -> tuple[float, float]:
    """When to deliver a frame, and when the channel is free again.

    Carrying `channel_free_at` forward is what makes this a store-and-forward
    channel rather than a per-frame sleep: back-to-back frames queue behind
    each other, and latency is only paid by a frame that finds the channel
    idle -- exactly how a real link pipelines a burst.
    """
    serialize = frame_len * 8 / profile.bitrate_bps if profile.bitrate_bps > 0 else 0.0
    free_at = max(now, channel_free_at) + serialize
    latency_ms = profile.latency_ms + profile.jitter_ms * jitter_frac
    return free_at + max(0.0, latency_ms) / 1000.0, free_at


class _Shaper:
    """One tester's listener, forwarding its connection to the hub."""

    def __init__(self, tag: str, upstream_host: str, upstream_port: int,
                 profile: LinkProfile = DEFAULT_PROFILE):
        self.tag = tag
        self.profile = profile
        self._upstream = (upstream_host, upstream_port)
        self._rand = random.Random(f"trenchchat-shaper-{tag}")
        self.bytes_up = 0
        self.bytes_down = 0
        self.frames_dropped = 0

    def stats(self) -> dict:
        return {
            "bytes_up": self.bytes_up, "bytes_down": self.bytes_down,
            "frames_dropped": self.frames_dropped,
        }

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(*self._upstream)
        except OSError:
            writer.close()
            return

        outbound: asyncio.Queue = asyncio.Queue(FRAME_QUEUE_MAX)
        inbound: asyncio.Queue = asyncio.Queue(FRAME_QUEUE_MAX)
        tasks = [
            asyncio.ensure_future(self._pump_in(reader, outbound, upstream=True)),
            asyncio.ensure_future(self._pump_out(up_writer, outbound)),
            asyncio.ensure_future(self._pump_in(up_reader, inbound, upstream=False)),
            asyncio.ensure_future(self._pump_out(writer, inbound)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for stream in (writer, up_writer):
                try:
                    stream.close()
                except OSError:
                    pass

    async def _pump_in(self, reader: asyncio.StreamReader, queue: asyncio.Queue,
                       upstream: bool) -> None:
        """Read as fast as the peer sends, framing into the queue.

        This must never block on the queue. RNS sets TCP_USER_TIMEOUT to 24s,
        so a tester whose data sits unacknowledged that long tears the link
        down -- a shaped link has to buffer in memory, not in the socket.
        """
        buf = b""
        while True:
            chunk = await reader.read(READ_CHUNK_BYTES)
            if not chunk:
                return
            if upstream:
                self.bytes_up += len(chunk)
            else:
                self.bytes_down += len(chunk)

            buf += chunk
            frames, buf = split_frames(buf)
            if len(buf) > MAX_PARTIAL_FRAME_BYTES:
                frames.append(buf)
                buf = b""
            for frame in frames:
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    self.frames_dropped += 1

    async def _pump_out(self, writer: asyncio.StreamWriter, queue: asyncio.Queue) -> None:
        """Deliver queued frames on the current profile's schedule."""
        loop = asyncio.get_event_loop()
        channel_free_at = 0.0
        while True:
            frame = await queue.get()
            profile = self.profile

            if profile.loss_pct > 0 and self._rand.random() * 100.0 < profile.loss_pct:
                self.frames_dropped += 1
                continue

            if profile.shaped:
                jitter = self._rand.uniform(-1.0, 1.0) if profile.jitter_ms > 0 else 0.0
                deliver_at, channel_free_at = schedule(
                    loop.time(), channel_free_at, len(frame), profile, jitter,
                )
                delay = deliver_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)

            writer.write(frame)
            await writer.drain()


class ShaperPool:
    """Every tester's shaper, running on one background asyncio loop.

    The orchestrator drives this from plain sync code -- both before uvicorn's
    loop exists and from inside its request handlers -- so the pool owns a
    thread and a loop of its own rather than borrowing uvicorn's.
    """

    def __init__(self, upstream_host: str, upstream_port: int):
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._shapers: dict[str, _Shaper] = {}
        self._servers: list[asyncio.AbstractServer] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self, listeners: dict[str, int], host: str = "127.0.0.1") -> None:
        """Bind one listener per tag and block until all of them are up."""
        if self._thread is not None:
            return
        self._shapers = {
            tag: _Shaper(tag, self._upstream_host, self._upstream_port)
            for tag in listeners
        }
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(listeners, host, ready),
            daemon=True, name="link-shaper",
        )
        self._thread.start()
        ready.wait(timeout=10.0)

    def _run(self, listeners: dict[str, int], host: str, ready: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._bind_all(listeners, host))
        finally:
            ready.set()
        self._loop.run_forever()

    async def _bind_all(self, listeners: dict[str, int], host: str) -> None:
        for tag, port in listeners.items():
            shaper = self._shapers[tag]
            self._servers.append(
                await asyncio.start_server(shaper.handle, host, port)
            )

    def stop(self) -> None:
        if self._loop is None or self._thread is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop = None
        self._thread = None
        self._servers = []

    def set_profile(self, tag: str, profile: LinkProfile) -> bool:
        """Retune a tester's link. Takes effect on its next frame."""
        shaper = self._shapers.get(tag)
        if shaper is None:
            return False
        shaper.profile = profile
        return True

    def profile(self, tag: str) -> LinkProfile:
        shaper = self._shapers.get(tag)
        return shaper.profile if shaper is not None else DEFAULT_PROFILE

    def reset_profiles(self) -> None:
        for shaper in self._shapers.values():
            shaper.profile = DEFAULT_PROFILE
            shaper.bytes_up = 0
            shaper.bytes_down = 0
            shaper.frames_dropped = 0

    def stats(self, tag: str) -> dict:
        shaper = self._shapers.get(tag)
        return shaper.stats() if shaper is not None else {
            "bytes_up": 0, "bytes_down": 0, "frames_dropped": 0,
        }
