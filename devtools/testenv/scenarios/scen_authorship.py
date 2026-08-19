"""
Family J -- message integrity: author signatures and attachments.

A synced message reaches you from a peer who usually did not write it. LXMF
authenticates the peer that handed it over and nothing else, so every relayed
message now carries its author's own signature, and a receiver that cannot
verify one drops the message.

That makes verification a delivery dependency, not just a security check: a
receiver who cannot resolve an author's public key loses honest history with
nothing but a log line to show for it. This family exists to find out when
that happens.

See docs/testenv-scenarios.md for the matrix these implement.
"""

import base64
import io
import struct
import zlib

from asserts import all_hold, settle, wait_until, ScenarioFailure
from flows import (
    await_discovery, go_offline, go_online, join_all, public_channel,
    BACKFILL_TIMEOUT, DISCOVERY_TIMEOUT, RECONNECT_TIMEOUT,
)
from scenario import PROBE, scenario

# Long enough for a fresh identity to boot, announce and be announced to.
RESET_SETTLE = 150.0

# Declared 20000x20000 -- 400M pixels, ten times MAX_IMAGE_PIXELS -- in 68
# bytes. The byte cap cannot see this; only reading the header can.
BOMB_WIDTH = BOMB_HEIGHT = 20000


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))


def bomb_png() -> str:
    """A tiny PNG whose header declares an enormous raster, base64-encoded."""
    ihdr = struct.pack(">IIBBBBB", BOMB_WIDTH, BOMB_HEIGHT, 8, 0, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", ihdr)
           + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 10))
           + _png_chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


def small_jpeg() -> str:
    """A real, small image, base64-encoded."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 120, 200)).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


@scenario("J1", "History outlives its author: a relay serves a dead peer's messages")
def j1(env):
    """The author is gone by the time the message is verified.

    C never received these messages directly -- it was offline when they were
    sent, and their author's process is dead before C returns. B relays them,
    but B's signature is not what C checks: C has to verify A's, which means
    resolving A's public key with A unreachable. If that fails the messages
    are dropped with only a warning, and C's transcript is short by two
    messages it will never ask for again.
    """
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "j1-public")

    go_offline(c)

    authored = {f"from-A-{i}" for i in range(2)}
    for content in sorted(authored):
        a.send(ch, content)
    all_hold([b, d], ch, authored, timeout=DISCOVERY_TIMEOUT)

    env.orch.kill(a.tag)
    wait_until(lambda: not a.alive(), "A's process to go away", 60.0)

    go_online(c)

    arrived, elapsed = settle(lambda: c.contents(ch) == authored,
                              "C to accept history authored by a peer that has left",
                              BACKFILL_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(
            f"C holds {sorted(c.contents(ch))}, expected {sorted(authored)} — "
            f"relayed history from an absent author was not accepted"
        )

    authors = {m["sender_hash"] for m in c.messages(ch)}
    if authors != {a.hash}:
        raise ScenarioFailure(
            f"relayed messages are attributed to {authors}, not their author {a.hash}"
        )

    return {"backfill_secs": round(elapsed, 1), "messages": len(authored)}


@scenario("J2", "A peer that never met the author reads their history anyway")
def j2(env):
    """The case J1 cannot reach: no shared history with the author at all.

    D is wiped to a brand-new identity after A is dead, so it has never seen
    A's announce and holds nothing of A in either its own key cache or RNS's.
    The channel stays discoverable because B owns it, not A.

    This ran as a probe first and confirmed the gap it predicted: D backfilled
    everything the live owner wrote and silently lost everything the departed
    author wrote, because resolve_author() had no key to find and
    verify_message() cannot tell "unverifiable" from "forged". Responders now
    send each batch's author keys alongside it, so it is strict.
    """
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(b, [a, c], "j2-public")

    by_a = {"only-A-wrote-this"}
    by_b = {"only-B-wrote-this"}
    a.send(ch, *by_a)
    b.send(ch, *by_b)
    all_hold([b, c], ch, by_a | by_b, timeout=DISCOVERY_TIMEOUT)

    env.orch.kill(a.tag)
    wait_until(lambda: not a.alive(), "A's process to go away", 60.0)

    # A new identity, with no memory of anyone -- including the dead author.
    env.orch.reset_tester(d.tag)
    env.wait_alive(d, RESET_SETTLE)
    d.forget_hash()

    await_discovery([d], ch, RESET_SETTLE)
    join_all([d], ch, b)

    got_b, b_secs = settle(lambda: by_b <= d.contents(ch),
                           "D to backfill what the live owner authored",
                           BACKFILL_TIMEOUT)
    got_a, a_secs = settle(lambda: by_a <= d.contents(ch),
                           "D to backfill what the departed peer authored",
                           BACKFILL_TIMEOUT)

    if not got_b:
        raise ScenarioFailure(
            "D backfilled nothing at all, so this measures join and backfill "
            "rather than author verification"
        )
    if not got_a:
        raise ScenarioFailure(
            f"D holds {sorted(d.contents(ch))} — a departed author's history "
            f"was dropped as unverifiable, so it is unreadable to everyone who "
            f"joins after they leave"
        )
    return {
        "live_author_secs": round(b_secs, 1),
        "departed_author_secs": round(a_secs, 1),
        "held": sorted(d.contents(ch)),
    }


@scenario("J3", "An image attachment survives the trip and stays fetchable")
def j3(env):
    """The signature covers the attachment, so the two travel together.

    author_digest() hashes the image bytes, which means a stripped or altered
    attachment invalidates the whole message rather than quietly arriving
    without its picture. This is the honest path: a real image, unmodified,
    accepted and readable at the far end.
    """
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "j3-public")

    a.send(ch, "with-picture", image_data_b64=small_jpeg())
    all_hold([b, c, d], ch, {"with-picture"}, timeout=DISCOVERY_TIMEOUT)

    for peer in (b, c, d):
        row = peer.message_by_content(ch, "with-picture")
        if not row["has_image"]:
            raise ScenarioFailure(f"{peer.tag} received the message without its image")
        if row["image_stripped"]:
            raise ScenarioFailure(f"{peer.tag} stripped an image that was never hostile")
        status = peer.message_image_status(ch, row["message_id"])
        if status != 200:
            raise ScenarioFailure(f"{peer.tag} cannot serve the stored image: {status}")

    return {"receivers": 3}


@scenario("J4", "A decompression-bomb image never reaches the wire")
def j4(env):
    """68 bytes declaring 400 million pixels.

    The byte cap cannot see this: the payload is tiny and what it expands to
    is the problem. The sender's own API is the gate -- prepare_image() fails
    closed now rather than forwarding bytes it could not re-encode -- so the
    message should go out as text with nothing attached, and no receiver
    should ever have to decide what to do with it.
    """
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "j4-public")

    a.send(ch, "hostile-attachment", image_data_b64=bomb_png())
    all_hold([a, b, c, d], ch, {"hostile-attachment"}, timeout=DISCOVERY_TIMEOUT)

    carried = {p.tag: p.message_by_content(ch, "hostile-attachment")["has_image"]
               for p in (a, b, c, d)}
    if any(carried.values()):
        raise ScenarioFailure(f"a decompression bomb was attached and delivered: {carried}")

    return {"delivered_as_text_to": sorted(carried)}
