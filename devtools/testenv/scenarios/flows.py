"""
Shared multi-step setup used by more than one family.

Nothing here asserts a scenario's subject -- it only gets peers into the
state a scenario starts from, with every intermediate step waited on rather
than slept through.
"""

from asserts import (
    discovered_hashes, joined_hashes, settle, subscribers_converged, wait_until,
    ScenarioFailure,
)

# Announces drive discovery and, on a public channel, backfill. worker.py runs
# the heartbeat at 10s, so anything announce-triggered needs room for more than
# one cycle.
DISCOVERY_TIMEOUT = 60.0
BACKFILL_TIMEOUT = 90.0

# A link drop takes 5-15s to restore; a process restart is slower still.
RECONNECT_TIMEOUT = 90.0

# How long a "never arrives" claim is held open before it counts as proven.
NEGATIVE_HOLD_SECS = 15.0

# A dropped MT_SUBSCRIBE is only recoverable by joining again (see
# _await_registration), so wait a couple of announce cycles, then re-join.
JOIN_REGISTER_TIMEOUT = 25.0
JOIN_ATTEMPTS = 3


def await_discovery(peers, channel_hash: str, timeout: float = DISCOVERY_TIMEOUT) -> None:
    for p in peers:
        wait_until(lambda p=p: channel_hash in discovered_hashes(p),
                   f"{p.tag} to discover the channel", timeout)


def join_all(peers, channel_hash: str, owner=None) -> None:
    """Join every peer, and wait for the owner to have registered them.

    Joining only sets the joiner's own state; the owner learns of it from an
    inbound MT_SUBSCRIBE that arrives separately. Until that lands the owner
    addresses its sends to a set the joiner isn't in, so any fan-out assertion
    made before this point is testing subscribe latency, not fan-out.
    """
    await_discovery(peers, channel_hash)
    for p in peers:
        if not p.join(channel_hash):
            raise ScenarioFailure(f"{p.tag} failed to join {channel_hash[:12]}")
        wait_until(lambda p=p: channel_hash in joined_hashes(p),
                   f"{p.tag} to show the channel as joined")
    if owner is not None:
        for p in peers:
            _await_registration(p, owner, channel_hash)


def _await_registration(joiner, owner, channel_hash: str) -> None:
    """Wait for the owner to register a joiner, re-issuing the join if needed.

    subscribe() sends MT_SUBSCRIBE through SubscriptionManager._send_raw, which
    drops the message outright when the joiner's path to the owner is not yet
    resolved -- no queue, no retry, and nothing else ever re-sends it. The
    owner then never learns of the subscriber, and the joiner is silently
    absent from every send. Re-issuing the join is the only way back, so the
    harness does it rather than stalling a scenario's setup on a cold path.
    """
    for attempt in range(JOIN_ATTEMPTS):
        registered, _ = settle(
            lambda: joiner.hash in owner.subscribers(channel_hash),
            f"{owner.tag} to register {joiner.tag} as a subscriber",
            JOIN_REGISTER_TIMEOUT,
        )
        if registered:
            return
        if attempt < JOIN_ATTEMPTS - 1:
            joiner.join(channel_hash)
    raise ScenarioFailure(
        f"{owner.tag} never registered {joiner.tag} as a subscriber after "
        f"{JOIN_ATTEMPTS} joins"
    )


def public_channel(owner, joiners, name: str) -> str:
    """Create a public channel and get everyone fully mutually aware of it.

    Returns once every peer's subscriber set names every other peer, which is
    the precondition for any of them addressing a send to the rest.
    """
    channel_hash = owner.create_channel(name, "public")
    join_all(joiners, channel_hash, owner)
    subscribers_converged([owner, *joiners], channel_hash, timeout=DISCOVERY_TIMEOUT)
    return channel_hash


def go_offline(peer) -> None:
    peer.go_offline()
    wait_until(lambda: not peer.net_status()["online"], f"{peer.tag}'s link to drop")


def go_online(peer) -> None:
    peer.go_online()
    wait_until(lambda: peer.net_status()["online"], f"{peer.tag}'s link to come back",
               RECONNECT_TIMEOUT)
