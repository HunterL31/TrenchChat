"""
Shared test utilities for TrenchChat integration tests.
"""

import time
import RNS

from tests.conftest import TestPeer
from trenchchat.core.permissions import is_open_join, permissions_from_json
from trenchchat.core.storage import Storage


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.2,
             msg: str = "condition") -> bool:
    """
    Poll predicate() until it returns truthy or timeout expires.
    Returns True if the predicate was satisfied, False on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def wait_for_message(storage: Storage, channel_hash: str, message_id: str,
                     timeout: float = 10.0) -> bool:
    """Wait until a specific message_id appears in storage for the given channel."""
    return wait_for(
        lambda: storage.message_exists(message_id),
        timeout=timeout,
        msg=f"message {message_id[:12]}… in channel {channel_hash[:12]}…",
    )


def delivery_dest_hash_hex(identity_hash_hex: str) -> str:
    """
    Compute the LXMF delivery destination hash for a given identity hash hex.
    """
    identity_hash = bytes.fromhex(identity_hash_hex)
    return RNS.Destination.hash(identity_hash, "lxmf", "delivery").hex()


def wait_for_subscriber(peer: TestPeer, channel_hash: str, subscriber_identity_hex: str,
                        timeout: float = 10.0) -> bool:
    """
    Wait until a peer's SubscriptionManager has a specific subscriber.

    subscriber_identity_hex: the subscriber's identity hash hex.
    SubscriptionManager stores subscribers as identity hashes (after the
    source_hash → identity resolution fix in subscription.py).
    """
    return wait_for(
        lambda: subscriber_identity_hex in peer.subscription_mgr.get_subscribers(channel_hash),
        timeout=timeout,
        msg=f"subscriber {subscriber_identity_hex[:12]}… on channel {channel_hash[:12]}…",
    )


def wait_for_member(storage: Storage, channel_hash: str, identity_hex: str,
                    timeout: float = 10.0) -> bool:
    """Wait until an identity appears in the members table for a channel."""
    return wait_for(
        lambda: storage.is_member(channel_hash, identity_hex),
        timeout=timeout,
        msg=f"member {identity_hex[:12]}… in channel {channel_hash[:12]}…",
    )


def wait_for_channel(storage: Storage, channel_hash: str,
                     timeout: float = 10.0) -> bool:
    """Wait until a channel appears in storage."""
    return wait_for(
        lambda: storage.get_channel(channel_hash) is not None,
        timeout=timeout,
        msg=f"channel {channel_hash[:12]}… in storage",
    )


def announce_and_wait(peer: TestPeer, wait: float = 0.1):
    """
    Announce the peer's delivery destination and owned channels.

    With TestTransport, peers are immediately reachable without network
    path resolution, so the wait is minimal. The announce still fires
    PeerAnnounceHandler callbacks for any peers that have registered
    announce handlers.
    """
    peer.announce()
    time.sleep(wait)


def get_subscriber_hashes(peer: TestPeer, channel_hash: str) -> list[str]:
    """
    Return all subscriber/member identity hashes for a channel,
    suitable for passing to Messaging.send_message().

    For public channels: uses SubscriptionManager's in-memory set.
    For invite-only channels: uses the members table.
    Includes the channel owner if known.
    """
    channel = peer.storage.get_channel(channel_hash)
    if channel is None:
        return []

    hashes: set[str] = set()

    if is_open_join(permissions_from_json(channel["permissions"])):
        hashes.update(peer.subscription_mgr.get_subscribers(channel_hash))
        # Always include the creator so they receive their own messages
        hashes.add(channel["creator_hash"])
    else:
        for row in peer.storage.get_members(channel_hash):
            hashes.add(row["identity_hash"])

    return list(hashes)


def identity_known(peer_hex: str) -> bool:
    """
    Return True if the given identity's LXMF delivery destination is
    locally known (i.e. the identity has been registered in this process).

    With TestTransport, all peers created in the same test are immediately
    reachable since their identities are registered locally when the
    LXMFRouter is created.
    """
    try:
        identity_hash = bytes.fromhex(peer_hex)
        delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
        return RNS.Identity.recall(delivery_dest_hash) is not None
    except Exception:
        return False


def wait_for_path(peer_hex: str, timeout: float = 10.0) -> bool:
    """
    Wait until a peer's identity is locally known.

    With TestTransport, this is immediately true for all peers created
    in the same test. The function is kept for API compatibility.
    """
    return wait_for(
        lambda: identity_known(peer_hex),
        timeout=timeout,
        msg=f"identity {peer_hex[:12]}…",
    )
