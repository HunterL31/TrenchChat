"""
Family H -- live group voice.

Voice runs on two planes and only the first is reachable from the pytest
suite: signalling is LXMF, but frames travel over a full mesh of real RNS
Links, one per participant pair, each authorised on its own
VP_HELLO/VP_ACCEPT handshake. `tests/fake_voice.py` doubles that transport;
nothing in `tests/` dials a real link.

`smoke_test.py` proves two processes can stream to each other. What is left
uncovered, and what this family is for, is everything that needs three or
four: a full mesh rather than a single pair, roster convergence across
peers that learn of each other indirectly, and the states the design
document is explicit about — "unreachable" being shown rather than hidden,
a demoted participant being cut off by the re-authorisation sweep.

Voice needs a fast link. These run on broadband profiles; H11 is the one
deliberate exception.

See docs/voice.md and docs/testenv-scenarios.md.
"""

import time

from asserts import (
    all_hold, hold_for, settle, voice_rosters, voice_rosters_agree,
    wait_until, ScenarioFailure,
)
from flows import (
    go_offline, go_online, invite_only_channel, public_channel, set_link_profile,
    BROADBAND, LORA_FAST, DISCOVERY_TIMEOUT, NEGATIVE_HOLD_SECS,
)
from scenario import PROBE, scenario

SEND_MESSAGE = "send_message"
INVITE = "invite"
KICK = "kick"
MANAGE_ROLES = "manage_roles"
VOICE_CHAT = "voice_chat"

ADMIN_WITH_VOICE = [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, VOICE_CHAT]
MEMBER_WITH_VOICE = [SEND_MESSAGE, VOICE_CHAT]
MEMBER_NO_VOICE = [SEND_MESSAGE]

# A mesh link is dialled, identified and accepted before frames flow, and the
# lexicographically larger identity waits 10s before dialling back.
MESH_TIMEOUT = 90.0

# VoiceManager in the testenv runs a 10s state refresh and a 30s roster TTL
# (backend_core shortens both), so anything driven by expiry needs room for it.
ROSTER_TTL_TIMEOUT = 120.0

# Long enough for the tone pipeline to produce a usable quality sample.
TONE_WINDOW_SECS = 8.0

# 20 ms Opus frames, so ~50 a second. Measured at 384 over an 8 s window on a
# broadband mesh (H9); used as the denominator for a delivery ratio, since
# loss_pct only counts gaps between frames that actually arrived.
EXPECTED_FRAMES = TONE_WINDOW_SECS * 48

STREAMING = "streaming"
UNREACHABLE = "unreachable"
SELF = "self"


def _join_voice_all(peers, channel_hash: str) -> None:
    for p in peers:
        if not p.join_voice(channel_hash):
            raise ScenarioFailure(f"{p.tag} was refused voice on {channel_hash[:12]}")
        wait_until(lambda p=p: p.voice_status()["channel"] == channel_hash,
                   f"{p.tag} to report itself in the voice session")


def _await_mesh(peers, channel_hash: str, timeout: float = MESH_TIMEOUT) -> float:
    """Every peer streaming to every other peer.

    The roster naming a peer only means signalling reached us; `streaming` is
    the only value that means an authorised link is actually carrying frames.
    """
    def meshed() -> bool:
        for p in peers:
            states = p.voice_link_states(channel_hash)
            for other in peers:
                want = SELF if other is p else STREAMING
                if states.get(other.hash) != want:
                    return False
        return True

    return wait_until(meshed, f"{[p.tag for p in peers]} to form a full voice mesh",
                      timeout)


@scenario("H1", "Three participants form a full voice mesh")
def h1(env):
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "h1-voice")

    _join_voice_all([a, b, c], ch)
    voice_rosters_agree([a, b, c], ch, [a, b, c], timeout=MESH_TIMEOUT)
    elapsed = _await_mesh([a, b, c], ch)
    return {"mesh_secs": round(elapsed, 1), "participants": 3}


@scenario("H2", "A late joiner learns the occupants and they learn it")
def h2(env):
    """The joiner is announced by its own voice_join; it learns who is already
    there only because each occupant unicasts one voice_state back. With three
    peers already in, that reply path has to work three times over."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "h2-voice")

    _join_voice_all([a, b, c], ch)
    _await_mesh([a, b, c], ch)

    _join_voice_all([d], ch)
    elapsed = voice_rosters_agree([a, b, c, d], ch, [a, b, c, d],
                                  timeout=MESH_TIMEOUT)
    mesh = _await_mesh([a, b, c, d], ch)
    return {"roster_secs": round(elapsed, 1), "mesh_secs": round(mesh, 1),
            "participants": 4}


@scenario("H3", "Leaving voice drops the peer from every roster")
def h3(env):
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "h3-voice")

    _join_voice_all([a, b, c], ch)
    voice_rosters_agree([a, b, c], ch, [a, b, c], timeout=MESH_TIMEOUT)

    if not c.leave_voice():
        raise ScenarioFailure("C's leave_voice was refused")
    elapsed = voice_rosters_agree([a, b], ch, [a, b], timeout=MESH_TIMEOUT)

    if c.voice_status()["channel"] is not None:
        raise ScenarioFailure("C still reports itself in a voice session")
    return {"drop_secs": round(elapsed, 1)}


@scenario("H4", "A participant killed mid-call expires from the roster", kind=PROBE)
def h4(env):
    """A clean leave sends voice_leave; a killed process sends nothing, so the
    only thing that removes it is the roster TTL. Measures how long the other
    participants keep showing someone who is gone."""
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "h4-voice")

    _join_voice_all([a, b, c], ch)
    _await_mesh([a, b, c], ch)

    env.orch.kill(c.tag)
    wait_until(lambda: not c.alive(), "C's process to die")

    expired, elapsed = settle(
        lambda: set(a.voice_link_states(ch)) == {a.hash, b.hash},
        "the killed participant to expire from A's roster", ROSTER_TTL_TIMEOUT,
    )
    notes = {
        "expired": expired,
        "expiry_secs": round(elapsed, 1) if expired else None,
        "a_roster": voice_rosters([a, b], ch)["A"],
    }
    if not expired:
        notes["surprise"] = "a killed participant never expired from the roster"
    return notes


@scenario("H5", "A link-dropped participant is shown unreachable, not hidden")
def h5(env):
    """docs/voice.md is explicit that "in voice but unreachable" is an honest
    state to surface rather than hide, so the roster must keep the entry and
    downgrade its link_state."""
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "h5-voice")

    _join_voice_all([a, b, c], ch)
    _await_mesh([a, b, c], ch)

    go_offline(c)
    downgraded, elapsed = settle(
        lambda: a.voice_link_states(ch).get(c.hash) not in (STREAMING, None),
        "A to stop reporting the dropped peer as streaming", ROSTER_TTL_TIMEOUT,
    )
    state = a.voice_link_states(ch).get(c.hash)
    if not downgraded:
        raise ScenarioFailure(
            f"A still reports a link-dropped participant as {state!r}: "
            f"{voice_rosters([a, b], ch)}"
        )
    if state is None:
        raise ScenarioFailure(
            "the dropped participant vanished from the roster instead of being "
            "shown unreachable -- docs/voice.md calls for surfacing this state"
        )

    go_online(c)
    _await_mesh([a, b, c], ch)
    return {"downgraded_to": state, "downgrade_secs": round(elapsed, 1)}


@scenario("H6", "voice_chat denied blocks a join, granted allows it")
def h6(env):
    """The mirror pair, like B10/B11: a refusal only means something if the
    grant demonstrably works. Invite-only, since open-join needs no row."""
    a, c = env.peers("A", "C")
    ch = invite_only_channel(a, [c], "h6-voice",
                             permissions=(ADMIN_WITH_VOICE, MEMBER_NO_VOICE))
    wait_until(lambda: VOICE_CHAT not in c.permissions(ch)["member"],
               "C to see voice_chat withheld", DISCOVERY_TIMEOUT)

    if c.join_voice(ch):
        raise ScenarioFailure("a member without voice_chat was allowed to join voice")
    hold_for(lambda: c.voice_status()["channel"] is None,
             "C to stay out of the voice session", NEGATIVE_HOLD_SECS)

    a.set_permissions(ch, admin=ADMIN_WITH_VOICE, member=MEMBER_WITH_VOICE)
    wait_until(lambda: VOICE_CHAT in c.permissions(ch)["member"],
               "C to see the voice_chat grant", DISCOVERY_TIMEOUT)

    if not c.join_voice(ch):
        raise ScenarioFailure("a member holding voice_chat was still refused")
    wait_until(lambda: c.voice_status()["channel"] == ch,
               "C to be in the voice session after the grant")
    return {"refused_then_allowed": True}


@scenario("H7", "A channel predating voice fails closed until permissions are re-saved")
def h7(env):
    """docs/voice.md: channels created before the feature have no voice_chat
    entry, so non-owner members are denied until an owner re-saves. Modelled by
    a permissions dict with the key absent -- the owner must still pass."""
    a, c = env.peers("A", "C")
    ch = invite_only_channel(a, [c], "h7-voice",
                             permissions=(ADMIN_WITH_VOICE, MEMBER_NO_VOICE))

    if not a.join_voice(ch):
        raise ScenarioFailure("the owner was denied voice on its own channel")
    a.leave_voice()

    if c.join_voice(ch):
        raise ScenarioFailure("a member was allowed voice on a pre-voice channel")

    a.set_permissions(ch, admin=ADMIN_WITH_VOICE, member=MEMBER_WITH_VOICE)
    wait_until(lambda: VOICE_CHAT in c.permissions(ch)["member"],
               "the re-saved permissions to reach C", DISCOVERY_TIMEOUT)
    if not c.join_voice(ch):
        raise ScenarioFailure("re-saving permissions did not admit the member")
    return {"owner_always_passes": True}


@scenario("H8", "Revoking voice_chat mid-call cuts the participant off")
def h8(env):
    """docs/voice.md promises the re-authorisation sweep cuts a demoted
    participant off in about a second. tests/test_adversarial.py checks this
    against the transport double; this is the same claim over real links."""
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "h8-voice",
                             permissions=(ADMIN_WITH_VOICE, MEMBER_WITH_VOICE))

    _join_voice_all([a, b, c], ch)
    _await_mesh([a, b, c], ch)

    a.set_permissions(ch, admin=ADMIN_WITH_VOICE, member=MEMBER_NO_VOICE)
    cut, elapsed = settle(
        lambda: a.voice_link_states(ch).get(c.hash) != STREAMING,
        "the revoked participant to stop streaming to A", ROSTER_TTL_TIMEOUT,
    )
    if not cut:
        raise ScenarioFailure(
            f"a participant whose voice_chat was revoked kept streaming: "
            f"{voice_rosters([a, b], ch)}"
        )
    return {"cutoff_secs": round(elapsed, 1),
            "b_still_streaming": b.voice_link_states(ch).get(a.hash) == STREAMING}


@scenario("H9", "Three-way tone streaming reports real receive quality", kind=PROBE)
def h9(env):
    """smoke_test.py measures loss and jitter for one pair. A full mesh is the
    case the bandwidth budget in docs/voice.md is actually about: each speaker
    uploads to N-1 peers at once."""
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "h9-voice")

    _join_voice_all([a, b, c], ch)
    _await_mesh([a, b, c], ch)

    for p in (a, b, c):
        status, body = p.set_test_tone(True)
        if status != 200:
            raise ScenarioFailure(f"{p.tag} has no tone pipeline: {status} {body}")
        p.set_voice_muted(False)

    # A fixed window, not a poll: the measurement is how much arrived over a
    # known interval, so ending early would understate it.
    time.sleep(TONE_WINDOW_SECS)

    quality = {}
    for p in (a, b, c):
        stats = p.voice_status().get("stats", {})
        rx = stats.get("rx_quality", {}) or {}
        quality[p.tag] = {
            senders: {k: v for k, v in metrics.items()
                      if k in ("received", "lost", "loss_pct", "jitter_ms")}
            for senders, metrics in rx.items()
        }
    for p in (a, b, c):
        p.set_test_tone(False)

    heard = {tag: len(q) for tag, q in quality.items()}
    notes = {"senders_heard": heard, "rx_quality": quality}
    if any(n < 2 for n in heard.values()):
        notes["surprise"] = "a participant did not receive frames from both peers"
    return notes


@scenario("H10", "Voice traffic does not disturb text delivery")
def h10(env):
    """Voice frames are unreliable link packets on a separate destination, so
    chat should be unaffected while a mesh is streaming. Worth pinning: they
    share the same interface and the same bandwidth."""
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "h10-voice")

    _join_voice_all([a, b, c], ch)
    _await_mesh([a, b, c], ch)
    for p in (a, b, c):
        p.set_test_tone(True)
        p.set_voice_muted(False)

    expected = {f"h10-{i}" for i in range(5)}
    for content in sorted(expected):
        a.send(ch, content)
    latency = all_hold([b, c], ch, expected, timeout=MESH_TIMEOUT)

    for p in (a, b, c):
        p.set_test_tone(False)
    return {"delivery_secs": {k: round(v, 1) for k, v in latency.items()}}


@scenario("H11", "Voice over a link too slow for it degrades honestly", kind=PROBE)
def h11(env):
    """docs/voice.md says voice is not viable over LoRa or packet radio and
    that the UI should surface that rather than mask it. Records what actually
    happens rather than asserting a number: the useful outcome is an honest
    link_state, not a working call."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "h11-voice")
    profile = set_link_profile(env, b, LORA_FAST)

    joined_a = a.join_voice(ch)
    joined_b = b.join_voice(ch)

    streaming, elapsed = settle(
        lambda: a.voice_link_states(ch).get(b.hash) == STREAMING,
        "a mesh link to come up over a LoRa-class link", 60.0,
    )
    state = a.voice_link_states(ch).get(b.hash)

    # link_state alone does not answer the question the doc raises. A link
    # that comes up still has to carry 16 kbps of Opus over a 5.5 kbps wire,
    # so measure what actually arrives rather than trusting the state string.
    quality = {}
    if streaming:
        for p in (a, b):
            p.set_test_tone(True)
            p.set_voice_muted(False)
        time.sleep(TONE_WINDOW_SECS)
        for p in (a, b):
            rx = (p.voice_status().get("stats", {}) or {}).get("rx_quality", {}) or {}
            quality[p.tag] = {
                k: {m: v[m] for m in ("received", "lost", "loss_pct", "jitter_ms")
                    if m in v}
                for k, v in rx.items()
            }
            p.set_test_tone(False)

    notes = {
        "profile": profile,
        "both_joined": joined_a and joined_b,
        "link_state": state,
        "reached_streaming": streaming,
        "link_up_secs": round(elapsed, 1) if streaming else None,
        "rx_quality": quality,
    }
    if state is None:
        notes["surprise"] = "the peer vanished from the roster instead of showing a state"
    elif streaming:
        # loss_pct counts gaps between frames that arrived, so a starved link
        # -- where most frames never make it onto the wire at all -- reads as
        # 0% loss. Delivery ratio against the frame rate is what shows it.
        received = [m.get("received", 0)
                    for peer in quality.values() for m in peer.values()]
        worst_ratio = min(received, default=0) / EXPECTED_FRAMES
        worst_loss = max((m.get("loss_pct", 0.0)
                          for peer in quality.values() for m in peer.values()),
                         default=0.0)
        notes["frames_expected"] = int(EXPECTED_FRAMES)
        notes["worst_frames_received"] = min(received, default=0)
        notes["worst_delivery_ratio"] = round(worst_ratio, 3)
        notes["worst_loss_pct"] = worst_loss
        # loss_pct implies (100 - loss)% of the audio arrived. Flag whenever
        # the frames actually delivered fall far short of what it claims --
        # that gap is the metric failing, whatever its absolute value.
        implied_ratio = 1.0 - (worst_loss / 100.0)
        if worst_ratio < 0.5 and implied_ratio - worst_ratio > 0.25:
            notes["surprise"] = (
                f"{min(received, default=0)} of ~{int(EXPECTED_FRAMES)} frames "
                f"arrived ({worst_ratio:.0%}), while loss_pct of {worst_loss:.1f}% "
                f"implies {implied_ratio:.0%} -- the metric the UI is meant to "
                f"show cannot see a starved link, and link_state reads 'streaming'"
            )

    for p in (a, b):
        p.leave_voice()
    set_link_profile(env, b, BROADBAND)
    return notes
