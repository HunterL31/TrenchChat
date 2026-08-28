"""
Family interop -- direct messages with a client that is not TrenchChat.

Everything else in this suite is TrenchChat talking to itself, which cannot
show whether a conversation works with Sideband, NomadNet or anything else
speaking LXMF. These use lxmf_peer.py: RNS and LXMF and nothing else, sending
plain messages with no fields at all, over the same hub as every tester.

See docs/testenv-scenarios.md for the matrix these implement.
"""

import json
import subprocess
import sys
from pathlib import Path

from asserts import hold_for, wait_until, ScenarioFailure
from flows import DISCOVERY_TIMEOUT, NEGATIVE_HOLD_SECS
from scenario import scenario

_TESTENV_DIR = Path(__file__).resolve().parents[1]
_PEER = _TESTENV_DIR / "lxmf_peer.py"
_PEER_DIR = _TESTENV_DIR / "data" / "lxmfpeer"

# The bare client joins, announces and waits for a path before it can send.
PEER_TIMEOUT_SECS = 180.0


def bare_peer(*args: str) -> dict:
    """Run the bare LXMF client and return the JSON it reports."""
    result = subprocess.run(
        [sys.executable, str(_PEER), "--data-dir", str(_PEER_DIR), *args],
        capture_output=True, text=True, timeout=PEER_TIMEOUT_SECS,
    )
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not line:
        raise ScenarioFailure(
            f"the bare LXMF client said nothing (exit {result.returncode}): "
            f"{result.stderr.strip()[-400:]}"
        )
    return json.loads(line)


def peer_hash() -> str:
    return bare_peer("identity")["hash"]


@scenario("interop1", "A plain LXMF client's message arrives as a direct message",
          peers="A")
def f1(env):
    """The claim, from the outside.

    A client that has never heard of TrenchChat sends an ordinary LXMF message
    -- content, no fields -- to a tester that holds it as a friend. It has to
    land in the conversation with that sender, which is the whole point of
    carrying conversations in the standard format.
    """
    a, = env.peers("A")
    stranger = peer_hash()
    a.add_friend(stranger)

    sent = bare_peer("send", "--to", a.hash, "--content", "hello from plain lxmf")
    if not sent.get("sent"):
        raise ScenarioFailure(f"the bare client could not send: {sent}")

    wait_until(lambda: any(d["peer_hash"] == stranger for d in a.dms()),
               f"{a.tag} to open a conversation with the bare LXMF client",
               DISCOVERY_TIMEOUT)
    conversation = next(d for d in a.dms() if d["peer_hash"] == stranger)
    wait_until(lambda: "hello from plain lxmf" in a.contents(conversation["hash"]),
               f"{a.tag} to hold the message", DISCOVERY_TIMEOUT)

    if conversation["peer_is_trenchchat"]:
        raise ScenarioFailure("a bare LXMF client was mistaken for TrenchChat")
    return {"peer": stranger[:12]}


@scenario("interop2", "A plain LXMF client is refused unless it is a friend",
          peers="A")
def f2(env):
    """Interoperability is not a way around the gate.

    The same message, from the same client, with the friendship removed. Its
    lack of a TrenchChat envelope is exactly what an attacker would arrange,
    since that is the half carrying a signature -- it must buy nothing.
    """
    a, = env.peers("A")
    stranger = peer_hash()
    a.remove_friend(stranger)

    sent = bare_peer("send", "--to", a.hash, "--content", "let me in")
    if not sent.get("sent"):
        raise ScenarioFailure(f"the bare client could not send: {sent}")

    hold_for(lambda: not any(d["peer_hash"] == stranger for d in a.dms()),
             f"{a.tag} to keep refusing a stranger's message", NEGATIVE_HOLD_SECS)
    return {}


@scenario("interop3", "A direct message reaches a plain LXMF client readably",
          peers="A")
def f3(env):
    """The other direction, and the reason for the envelope.

    TrenchChat's additions have to travel where another client will ignore
    them, and the words have to travel where it will show them. So the bare
    client must receive the text in the ordinary content, and see only LXMF's
    own custom-payload fields alongside it -- never TrenchChat's field numbers,
    which mean other things in the standard registry.
    """
    a, = env.peers("A")
    stranger = peer_hash()
    a.add_friend(stranger)

    # It has to be listening when the message arrives: nothing here runs a
    # propagation node, so this is a live delivery.
    listener = subprocess.Popen(
        [sys.executable, str(_PEER), "--data-dir", str(_PEER_DIR),
         "listen", "--seconds", "45"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # The listener announces on startup; give that time to reach the hub
        # before addressing it, or the send has no path and queues instead.
        wait_until(lambda: any(e["identity_hash"] == stranger
                               for e in a.directory()),
                   f"{a.tag} to hear the bare LXMF client", DISCOVERY_TIMEOUT)
        conversation = a.open_dm(stranger)
        a.send_dm(stranger, "hello from trenchchat")
        stdout, stderr = listener.communicate(timeout=PEER_TIMEOUT_SECS)
    finally:
        if listener.poll() is None:
            listener.kill()

    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    if not line:
        raise ScenarioFailure(f"the bare client reported nothing: {stderr[-400:]}")
    received = json.loads(line)["received"]
    ours = [m for m in received if m["content"] == "hello from trenchchat"]
    if not ours:
        raise ScenarioFailure(
            f"the bare LXMF client never received the message: {received}")

    # 0xFB/0xFC are LXMF's own custom-payload fields, and 0x06 its image field.
    # Anything below 0x80 outside those would be TrenchChat squatting on a
    # number that means something else to this client.
    allowed = {0x06, 0xFB, 0xFC}
    intruders = [f for f in ours[0]["fields"] if f not in allowed]
    if intruders:
        raise ScenarioFailure(
            f"a direct message carried non-standard LXMF fields: {intruders}")
    return {"conversation": conversation[:12], "fields": ours[0]["fields"]}


@scenario("interop4", "A plain LXMF client can ask to be added by messaging",
          peers="A")
def f4(env):
    """The only way in, for a client that has no friend request to send.

    interop2 proves a stranger's message never reaches a conversation. This is
    the other half of that: it must not vanish either, or a client speaking
    only plain LXMF could never start a conversation with anyone who had not
    already added it out of band -- and would be told it was delivered.
    """
    a, = env.peers("A")
    stranger = peer_hash()
    a.remove_friend(stranger)
    a.decline_friend_request(stranger)

    sent = bare_peer("send", "--to", a.hash, "--content", "is this thing on")
    if not sent.get("sent"):
        raise ScenarioFailure(f"the bare client could not send: {sent}")

    wait_until(lambda: stranger in a.incoming_request_hashes(),
               f"{a.tag} to hold the stranger's message as a request",
               DISCOVERY_TIMEOUT)
    request = next(r for r in a.friend_requests()["incoming"]
                   if r["identity_hash"] == stranger)
    if request["message"] != "is this thing on":
        raise ScenarioFailure(
            f"the held request lost its words: {request['message']!r}")
    if request["from_trenchchat"]:
        raise ScenarioFailure("a bare LXMF client was mistaken for TrenchChat")

    # Holding it grants nothing until the user says so.
    if stranger in a.friend_hashes():
        raise ScenarioFailure("a held message made the sender a friend")
    if any(d["peer_hash"] == stranger for d in a.dms()):
        raise ScenarioFailure("a held message opened a conversation")

    if not a.accept_friend_request(stranger):
        raise ScenarioFailure("the held request could not be accepted")

    wait_until(lambda: any(d["peer_hash"] == stranger for d in a.dms()),
               f"{a.tag} to open the conversation on accepting", DISCOVERY_TIMEOUT)
    conversation = next(d for d in a.dms() if d["peer_hash"] == stranger)
    if "is this thing on" not in a.contents(conversation["hash"]):
        raise ScenarioFailure(
            "the words that asked to be heard were not filed into the "
            f"conversation: {a.contents(conversation['hash'])}")
    if stranger in a.incoming_request_hashes():
        raise ScenarioFailure("the request survived being accepted")
    return {"peer": stranger[:12]}
