"""
Family social -- reactions, emoji, presence and identity.

Everything here rides on top of a channel rather than being one: a reaction
targets a message, an avatar and a display name travel with an identity, and
a friend entry never leaves the device. The recurring question is which of
these have a recovery path when a peer misses the live broadcast -- chat
messages do, and the rest are worth checking rather than assuming.

See docs/testenv-scenarios.md for the matrix these implement.
"""

import base64
import hashlib
import io

from asserts import all_hold, hold_for, settle, wait_until, ScenarioFailure
from flows import (
    go_offline, go_online, invite_only_channel, public_channel,
    DISCOVERY_TIMEOUT, NEGATIVE_HOLD_SECS,
)
from scenario import PROBE, scenario

# AvatarManager.SEND_RATE_LIMIT_SECS is 60; allow a margin over one window.
AVATAR_RATE_LIMIT_TIMEOUT = 100.0


def _tiny_png(colour: tuple[int, int, int] = (200, 60, 60)) -> str:
    """A small solid-colour PNG, base64 encoded, for avatar and emoji payloads."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), colour).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _emoji_hash(peer, channel_hash: str, message_id: str) -> str:
    """A reaction needs an emoji hash; a unicode emoji hashes to a stable id."""
    return hashlib.sha256("👍".encode()).hexdigest()[:32]


def _reaction_count(peer, channel_hash: str, message_id: str) -> int:
    for m in peer.messages(channel_hash):
        if m["message_id"] == message_id:
            return sum(r["count"] for r in m.get("reactions", []))
    return 0


def _first_message_id(peer, channel_hash: str) -> str:
    messages = peer.messages(channel_hash)
    if not messages:
        raise ScenarioFailure(f"{peer.tag} holds no messages to react to")
    return messages[0]["message_id"]


@scenario("social1", "A reaction reaches every subscriber including the owner")
def f1(env):
    """The owner is in the broadcast subscriber payload; before it was, the
    owner alone never saw reactions, since reactions have no sync fallback."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "f1-public")

    a.send(ch, "react-to-me")
    all_hold([b, c, d], ch, {"react-to-me"}, timeout=DISCOVERY_TIMEOUT)
    msg_id = _first_message_id(b, ch)

    b.react(ch, msg_id, _emoji_hash(b, ch, msg_id))
    for peer in (a, c, d):
        wait_until(lambda peer=peer: _reaction_count(peer, ch, msg_id) == 1,
                   f"{peer.tag} to see the reaction", DISCOVERY_TIMEOUT)
    return {}


@scenario("social2", "Removing a reaction clears it everywhere")
def f2(env):
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "f2-public")

    a.send(ch, "react-to-me")
    all_hold([b, c], ch, {"react-to-me"}, timeout=DISCOVERY_TIMEOUT)
    msg_id = _first_message_id(b, ch)
    emoji = _emoji_hash(b, ch, msg_id)

    b.react(ch, msg_id, emoji)
    wait_until(lambda: _reaction_count(a, ch, msg_id) == 1, "the reaction to land",
               DISCOVERY_TIMEOUT)

    b.unreact(ch, msg_id, emoji)
    wait_until(lambda: _reaction_count(a, ch, msg_id) == 0, "the reaction to clear",
               DISCOVERY_TIMEOUT)
    wait_until(lambda: _reaction_count(c, ch, msg_id) == 0, "C to see it clear",
               DISCOVERY_TIMEOUT)
    return {}


@scenario("social3", "A reaction sent while a peer is offline is never backfilled", kind=PROBE)
def f3(env):
    """Matrix row social3. Chat messages have three recovery mechanisms; reactions
    have none, so an offline peer should miss one permanently."""
    a, b, d = env.peers("A", "B", "D")
    ch = public_channel(a, [b, d], "f3-public")

    a.send(ch, "react-to-me")
    all_hold([b, d], ch, {"react-to-me"}, timeout=DISCOVERY_TIMEOUT)
    msg_id = _first_message_id(b, ch)

    go_offline(d)
    b.react(ch, msg_id, _emoji_hash(b, ch, msg_id))
    wait_until(lambda: _reaction_count(a, ch, msg_id) == 1,
               "the reaction to land on a peer that stayed online", DISCOVERY_TIMEOUT)

    go_online(d)
    recovered, secs = settle(lambda: _reaction_count(d, ch, msg_id) == 1,
                             "D to recover the reaction it missed", 90.0)
    notes = {"recovered": recovered, "secs": round(secs, 1) if recovered else None,
             "d_count": _reaction_count(d, ch, msg_id)}
    if recovered:
        notes["surprise"] = "reactions do have a recovery path after all"
    return notes


@scenario("social4", "Presence flips to offline when a peer drops")
def f4(env):
    a, b, c = env.peers("A", "B", "C")
    public_channel(a, [b, c], "f4-public")

    wait_until(lambda: a.presence(b.hash).get("is_online"), "A to see B online",
               DISCOVERY_TIMEOUT)

    go_offline(b)
    elapsed = wait_until(lambda: not a.presence(b.hash).get("is_online"),
                         "A to see B go offline", 120.0)
    return {"detected_secs": round(elapsed, 1)}


@scenario("social5", "An avatar propagates and can be removed")
def f5(env):
    a, b, c = env.peers("A", "B", "C")
    public_channel(a, [b, c], "f5-public")

    a.set_avatar(_tiny_png())
    for peer in (b, c):
        wait_until(lambda peer=peer: bool(peer.peer_avatar(a.hash).get("avatar_data_b64")),
                   f"{peer.tag} to receive the avatar", DISCOVERY_TIMEOUT)

    # AvatarManager allows one change per SEND_RATE_LIMIT_SECS and answers 429
    # until it elapses, so the removal has to wait the window out.
    wait_until(lambda: a.try_remove_avatar() == 200,
               "the avatar rate limit to elapse so the removal is accepted",
               AVATAR_RATE_LIMIT_TIMEOUT, interval=5.0)

    removed, secs = settle(
        lambda: not b.peer_avatar(a.hash).get("avatar_data_b64"),
        "B to drop the removed avatar", 90.0,
    )
    if not removed:
        raise ScenarioFailure("a removed avatar was still held by a peer")
    return {"removal_secs": round(secs, 1)}


@scenario("social6", "A display-name change propagates to the directory")
def f6(env):
    a, b, c = env.peers("A", "B", "C")
    public_channel(a, [b, c], "f6-public")

    a.set_display_name("Renamed Tester")
    for peer in (b, c):
        wait_until(
            lambda peer=peer: any(
                e["identity_hash"] == a.hash and e["display_name"] == "Renamed Tester"
                for e in peer.directory()
            ),
            f"{peer.tag}'s directory to show the new name", DISCOVERY_TIMEOUT,
        )
    return {}


@scenario("social7", "A friend entry stays on the device that made it")
def f7(env):
    a, b, c = env.peers("A", "B", "C")
    public_channel(a, [b, c], "f7-public")

    a.add_friend(b.hash, nickname="Nickname Only A Sees")
    wait_until(lambda: any(f["identity_hash"] == b.hash for f in a.friends()),
               "A to hold the friend entry")

    hold_for(lambda: not b.friends() and not c.friends(),
             "the friend entry to stay local to A", NEGATIVE_HOLD_SECS)
    a.remove_friend(b.hash)
    wait_until(lambda: not a.friends(), "A to drop the friend entry")
    return {}


@scenario("social8", "A reply and a reaction on it resolve the same way everywhere")
def f8(env):
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "f8-public")

    a.send(ch, "original")
    all_hold([b, c], ch, {"original"}, timeout=DISCOVERY_TIMEOUT)
    original_id = _first_message_id(b, ch)

    b.send(ch, "the-reply", reply_to=original_id)
    all_hold([a, c], ch, {"original", "the-reply"}, timeout=DISCOVERY_TIMEOUT)

    def reply_row(peer):
        return next((m for m in peer.messages(ch) if m["content"] == "the-reply"), None)

    for peer in (a, c):
        row = reply_row(peer)
        if row is None or row["reply_to"] != original_id:
            raise ScenarioFailure(f"{peer.tag} resolved reply_to as "
                                  f"{row and row['reply_to']}, expected {original_id}")

    reply_id = reply_row(c)["message_id"]
    c.react(ch, reply_id, _emoji_hash(c, ch, reply_id))
    wait_until(lambda: _reaction_count(a, ch, reply_id) == 1,
               "the reaction on the reply to reach A", DISCOVERY_TIMEOUT)
    return {}


@scenario("social9", "A reply and a reaction resolve identically on an invite-only channel")
def f9(env):
    """social8's claim, re-made where recipients come from the member list
    rather than the subscriber set and tenure gates what each member holds.
    Every leg is a different author, so the conversation only converges if
    member sends fan out correctly in both directions."""
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "f9-private")

    a.send(ch, "original")
    all_hold([b, c], ch, {"original"}, timeout=DISCOVERY_TIMEOUT)
    original_id = _first_message_id(b, ch)

    b.send(ch, "the-reply", reply_to=original_id)
    all_hold([a, c], ch, {"original", "the-reply"}, timeout=DISCOVERY_TIMEOUT)

    def reply_row(peer):
        return next((m for m in peer.messages(ch) if m["content"] == "the-reply"), None)

    for peer in (a, c):
        row = reply_row(peer)
        if row is None or row["reply_to"] != original_id:
            raise ScenarioFailure(f"{peer.tag} resolved reply_to as "
                                  f"{row and row['reply_to']}, expected {original_id}")

    reply_id = reply_row(c)["message_id"]
    c.react(ch, reply_id, _emoji_hash(c, ch, reply_id))
    for peer in (a, b):
        wait_until(lambda peer=peer: _reaction_count(peer, ch, reply_id) == 1,
                   f"{peer.tag} to see the reaction on the reply", DISCOVERY_TIMEOUT)
    return {}


@scenario("social10", "A custom emoji reaction carries its image to every peer")
def f10(env):
    """A custom emoji is a sha256 reaction key plus an image only the importer
    holds; receivers must fetch the image over MT_EMOJI_REQUEST before they
    can render what the count already shows."""
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "f10-public")

    a.send(ch, "react-to-me")
    all_hold([b, c], ch, {"react-to-me"}, timeout=DISCOVERY_TIMEOUT)
    msg_id = _first_message_id(b, ch)

    imported = b.import_emoji("testenv-flag", _tiny_png((30, 120, 200)))
    if not imported.get("ok"):
        raise ScenarioFailure(f"emoji import refused: {imported}")
    emoji_hash = imported["emoji_hash"]

    b.react(ch, msg_id, emoji_hash)
    for peer in (a, c):
        wait_until(lambda peer=peer: _reaction_count(peer, ch, msg_id) == 1,
                   f"{peer.tag} to count the custom reaction", DISCOVERY_TIMEOUT)

    def holds_image(peer) -> bool:
        return any(e["emoji_hash"] == emoji_hash and e["image_data_b64"]
                   for e in peer.emojis())

    latency = {}
    for peer in (a, c):
        latency[peer.tag] = round(wait_until(
            lambda peer=peer: holds_image(peer),
            f"{peer.tag} to fetch the emoji image", 90.0), 1)
    return {"image_fetch_secs": latency}


# A client that has just started is unhearable until its next announce, and a
# real one announces every few hours. Slow enough that a scenario can observe
# the window; short enough that the run does not sit in it.
QUIET_HEARTBEAT_SECS = 600.0


@scenario("social11", "A peer that announces rarely can still reach a stranger",
          peers="AB")
def f11(env):
    """The case a person actually hit, and the reason it was invisible here.

    Every tester announces every 10s, so meeting one is instant and nothing
    ever exercises first contact. A real client announces every few hours:
    until a peer has heard it, that peer cannot recall its identity, cannot
    verify its signature, and quarantines its first message until it expires.
    Invites vanished exactly this way, and relaunching the client -- which
    announces once at startup -- was what made them appear.

    So A is slowed to a real client's cadence and restarted, then B is wiped so
    it has never heard anybody. B announces on startup; A must answer that with
    an announce of its own, or its invite is dropped at B's end and this fails.
    """
    a, b = env.peers("A", "B")

    env.orch.set_heartbeat(a.tag, QUIET_HEARTBEAT_SECS)
    env.wait_alive(a)
    ch = a.create_channel("f11-invite", access="invite")

    # B forgets every identity it has ever heard, A's included.
    env.orch.reset_tester(b.tag)
    env.wait_alive(b)
    b.forget_hash()
    wait_until(lambda: b.hash is not None, f"{b.tag} to come back with an identity",
               DISCOVERY_TIMEOUT)

    # A learns B from B's own announce; nothing has told B about A.
    wait_until(lambda: any(e["identity_hash"] == b.hash for e in a.directory()),
               f"{a.tag} to hear the restarted {b.tag}", DISCOVERY_TIMEOUT)

    a.invite(ch, b.hash)
    wait_until(lambda: any(i["channel_hash_hex"] == ch for i in b.invites()),
               f"{b.tag} to receive an invite from a peer it had never heard",
               DISCOVERY_TIMEOUT)
    return {}
