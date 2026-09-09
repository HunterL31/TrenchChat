"""
Shared test utilities for TrenchChat integration tests.
"""

import time
import RNS

from tests.conftest import TestPeer, signing_identity
from trenchchat.core.authorship import sign_message
from trenchchat.core.permissions import (
    PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER, permissions_from_json,
)
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


def know_channel(peer: TestPeer, channel_hash: str, owner: TestPeer) -> None:
    """Give a peer the channel row and a subscription, and no membership.

    The state a peer is in when it knows a channel exists but was never
    admitted to it, which is what an adversarial test needs: mirror_members
    would make it a member and invert the thing under test.
    """
    row = owner.storage.get_channel(channel_hash)
    peer.storage.upsert_channel(
        channel_hash, row["name"], row["description"], row["creator_hash"],
        permissions=permissions_from_json(row["permissions"]),
        created_at=row["created_at"])
    peer.storage.subscribe(channel_hash)


def _has_tenure(peer: TestPeer, channel_hash: str, identity_hash: str) -> bool:
    return peer.storage._fetchone(
        "SELECT 1 FROM membership_tenure WHERE channel_hash = ? "
        "AND identity_hash = ? LIMIT 1",
        (channel_hash, identity_hash),
    ) is not None


def clear_tenure(peer: TestPeer, channel_hash: str) -> None:
    """Drop every tenure interval a peer holds for a channel."""
    peer.storage._conn.execute(
        "DELETE FROM membership_tenure WHERE channel_hash = ?", (channel_hash,))
    peer.storage._conn.commit()


def mirror_members(channel_hash: str, owner: TestPeer, *peers: TestPeer,
                   description: str = "", joined_at: float | None = None,
                   member_role: str = ROLE_MEMBER, tenure: bool = True) -> str:
    """Give every peer the channel, a subscription, member rows and tenure.

    This is the state the real invite -> join_request -> member_list
    handshake converges on, written directly so setup is synchronous: a test
    migrated onto it cannot inherit a race it did not mean to test.

    Calls compose. The roster written is everyone the owner already holds
    plus the peers named here, so adding a third peer later still leaves all
    three knowing about each other, which is what the handshake would do.

    An explicit joined_at replaces each peer's tenure for the channel rather
    than adding beside it: a second open interval would keep an identity "a
    member" across a kick, which is what publish_member_list's own intervals
    exist to record.

    tenure=False leaves the tenure table empty, which is the state a peer
    bootstrapped from a roster (or one predating the feature) is really in.

    Tests whose subject *is* the member-list document, tenure or the invite
    flow build their own state instead; see tests/test_invites.py.
    """
    row = owner.storage.get_channel(channel_hash)
    name = row["name"]
    created_at = row["created_at"]
    perms = permissions_from_json(row["permissions"])
    tenure_at = created_at if joined_at is None else joined_at

    roster: dict[str, tuple[str, str]] = {
        m["identity_hash"]: (m["display_name"], m["role"])
        for m in owner.storage.get_members(channel_hash)
    }
    roster[owner.identity.hash_hex] = (owner.identity.display_name, ROLE_OWNER)
    for peer in peers:
        roster[peer.identity.hash_hex] = (peer.identity.display_name, member_role)

    for peer in (owner, *peers):
        if joined_at is not None:
            clear_tenure(peer, channel_hash)
        if peer is not owner:
            peer.storage.upsert_channel(
                channel_hash, name, description or row["description"],
                row["creator_hash"], permissions=perms, created_at=created_at)
            peer.storage.subscribe(channel_hash)
        for identity_hash, (display_name, role) in roster.items():
            peer.storage.upsert_member(channel_hash, identity_hash,
                                       display_name, role=role)
            if tenure and (joined_at is not None
                           or not _has_tenure(peer, channel_hash, identity_hash)):
                # Without an explicit joined_at this only fills gaps:
                # publish_member_list writes the authoritative intervals, and
                # a second open one beside them would keep an identity "a
                # member" across a kick.
                peer.storage.open_tenure(channel_hash, identity_hash, tenure_at)
    return channel_hash


def seed_channel(owner: TestPeer, members=(), *, name: str = "ch",
                 description: str = "", permissions: dict | None = None,
                 joined_at: float | None = None,
                 member_role: str = ROLE_MEMBER) -> str:
    """Create a channel on *owner* and mirror membership onto *members*."""
    channel_hash = owner.channel_mgr.create_channel(
        name, description, permissions=dict(permissions or PRESET_PRIVATE))
    return mirror_members(channel_hash, owner, *members,
                          description=description, joined_at=joined_at,
                          member_role=member_role)


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
    """Every member identity hash for a channel, for Messaging.send_message()."""
    if peer.storage.get_channel(channel_hash) is None:
        return []
    return [row["identity_hash"] for row in peer.storage.get_members(channel_hash)]


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


def wait_for_roster(peer: TestPeer, channel_hash: str, identity_hex: str,
                    timeout: float = 10.0) -> bool:
    """Wait until an identity appears in a peer's voice roster for a channel."""
    return wait_for(
        lambda: any(e["identity_hash"] == identity_hex
                    for e in peer.voice_mgr.get_roster(channel_hash)),
        timeout=timeout,
        msg=f"voice roster entry {identity_hex[:12]}… on {channel_hash[:12]}…",
    )


def wait_for_rx_frames(peer: TestPeer, sender_hex: str, count: int = 1,
                       timeout: float = 10.0) -> bool:
    """Wait until a peer has received at least count frames from a sender."""
    return wait_for(
        lambda: peer.voice_mgr.frame_stats()["rx_frames"].get(sender_hex, 0) >= count,
        timeout=timeout,
        msg=f"{count} voice frames from {sender_hex[:12]}…",
    )


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


def sign_as(sender_hex: str, channel_hash_hex: str, message_id: str,
            timestamp: float, content: str, reply_to: str | None = None,
            last_seen_id: str | None = None,
            image_data: bytes | None = None,
            manifest: dict | None = None) -> bytes | None:
    """Sign fabricated history as the peer who supposedly authored it.

    Tests seed transcripts by writing straight to storage or by hand-building
    sync rows, which skips the signing that Messaging.send_message does. A row
    without a signature is unverifiable and is correctly withheld and rejected,
    so fixtures have to produce what a real client produces. Returns None for
    an identity no peer_factory peer owns, which is what an unsigned row is
    meant to look like.
    """
    identity = signing_identity(sender_hex)
    if identity is None:
        return None
    return sign_message(identity, channel_hash_hex, message_id, timestamp,
                        content, reply_to, last_seen_id, image_data, manifest)
