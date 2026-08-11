"""
Shared multi-step action sequences driven by user-facing frontends.

Each user-visible action (create a channel, send a message, edit
permissions...) is more than one core manager call: it's a specific
sequence, sometimes with an outbound permission guard in front of it.
Previously that sequencing lived inline in trenchchat/gui/main_window.py's
_on_* handlers. It now lives here instead, as the single implementation,
so that any frontend driving the backend -- the Qt GUI or a headless
harness -- calls the exact same code and can't silently diverge from it.

These functions take already-constructed manager/storage objects and are
free of any GUI framework dependency.
"""

from trenchchat.core.permissions import (
    CREATE_CHANNEL, KICK, MANAGE_CHANNEL, MANAGE_ROLES, SEND_MESSAGE,
    is_open_join, permissions_from_json,
)


def create_channel(channel_mgr, invite_mgr, name: str, description: str,
                   permissions: dict) -> str:
    """Create a channel and, for invite-only channels, publish the initial
    member list document (without this, the channel has no signed member
    list for peers to validate future updates against)."""
    hash_hex = channel_mgr.create_channel(
        name=name, description=description, permissions=permissions,
    )
    if not is_open_join(permissions):
        invite_mgr.publish_member_list(hash_hex)
    return hash_hex


def join_public_channel(storage, subscription_mgr, channel_hash_hex: str) -> bool:
    """Subscribe to a known open-join channel. Returns False if the channel
    isn't in local storage yet (e.g. never discovered), or if it's
    invite-only -- membership there is granted only via a signed member-list
    document from an admin/owner, never a bare local subscribe. This is a
    second layer behind announce_channel()'s discoverability guard: if an
    invite-only channel's row ever ends up in local storage some other way
    (a stale row from before a permissions change, a future discovery path),
    this still can't be used to self-admit into it."""
    channel = storage.get_channel(channel_hash_hex)
    if channel is None:
        return False
    if not is_open_join(permissions_from_json(channel["permissions"])):
        return False
    subscription_mgr.subscribe(channel_hash_hex, channel["creator_hash"])
    return True


def compute_channel_recipients(storage, subscription_mgr, channel_hash_hex: str,
                               self_hash_hex: str) -> list[str]:
    """
    The delivery target set for a channel, matching how its access mode
    determines recipients:
      - invite-only: every member in the members table
      - open-join:   the live subscriber set, plus self so the caller's
                      own send is stored locally even with zero subscribers

    No permission gate here -- callers that need one (chat message sends
    do; reaction broadcasts don't) apply it themselves. Used for both.
    """
    channel = storage.get_channel(channel_hash_hex)
    perms = permissions_from_json(channel["permissions"]) if channel else {}

    if channel and not is_open_join(perms):
        return [row["identity_hash"] for row in storage.get_members(channel_hash_hex)]

    subs = subscription_mgr.get_subscribers(channel_hash_hex)
    dests = list(subs) if subs else []
    if self_hash_hex not in dests:
        dests.append(self_hash_hex)
    return dests


def compute_send_recipients(storage, subscription_mgr, channel_hash_hex: str,
                            sender_hash_hex: str) -> list[str] | None:
    """
    compute_channel_recipients(), gated on SEND_MESSAGE for non-open
    channels. Returns None if the sender lacks permission to send (the
    caller should treat this as a silent no-op, matching the GUI).
    """
    channel = storage.get_channel(channel_hash_hex)
    perms = permissions_from_json(channel["permissions"]) if channel else {}
    if channel and not is_open_join(perms):
        if not storage.has_permission(channel_hash_hex, sender_hash_hex, SEND_MESSAGE):
            return None

    return compute_channel_recipients(storage, subscription_mgr, channel_hash_hex, sender_hash_hex)


def send_message(storage, subscription_mgr, messaging, channel_hash_hex: str,
                 sender_hash_hex: str, content: str, *,
                 image_data: bytes | None = None,
                 reply_to: str | None = None) -> bool:
    """Compute recipients and send. Returns False (no-op) if the sender
    lacks permission to send on this channel."""
    recipients = compute_send_recipients(
        storage, subscription_mgr, channel_hash_hex, sender_hash_hex
    )
    if recipients is None:
        return False
    messaging.send_message(
        channel_hash_hex=channel_hash_hex,
        content=content,
        subscriber_hashes=recipients,
        image_data=image_data,
        reply_to=reply_to,
    )
    return True


def update_membership(storage, invite_mgr, channel_hash_hex: str, actor_hash_hex: str, *,
                      remove_members: list[bytes] | None = None,
                      add_admins: list[bytes] | None = None,
                      remove_admins: list[bytes] | None = None) -> bool:
    """
    Outbound guard + publish: filters each requested change down to what
    actor_hash_hex is actually permitted to do (KICK for removals,
    MANAGE_ROLES for role changes) before calling publish_member_list.
    Core-side enforcement in publish_member_list is still the real
    security boundary; this mirrors the GUI's pre-flight guard so a
    disallowed change never even reaches the wire.

    Returns False if everything requested was filtered out (no permission,
    or nothing was requested), so callers with no other feedback loop --
    e.g. an API response -- can tell an unauthorized request apart from one
    that actually took effect, instead of both looking like success.
    """
    can_kick = storage.has_permission(channel_hash_hex, actor_hash_hex, KICK)
    can_manage_roles = storage.has_permission(channel_hash_hex, actor_hash_hex, MANAGE_ROLES)

    remove_members = list(remove_members or []) if can_kick else []
    add_admins = list(add_admins or []) if can_manage_roles else []
    remove_admins = list(remove_admins or []) if can_manage_roles else []

    if not (remove_members or add_admins or remove_admins):
        return False

    invite_mgr.publish_member_list(
        channel_hash_hex,
        remove_members=remove_members or None,
        add_admins=add_admins or None,
        remove_admins=remove_admins or None,
    )
    return True


def edit_channel_permissions(storage, invite_mgr, channel_hash_hex: str,
                             actor_hash_hex: str, new_perms: dict) -> bool:
    """Returns False (no-op) if the actor lacks MANAGE_CHANNEL."""
    if not storage.has_permission(channel_hash_hex, actor_hash_hex, MANAGE_CHANNEL):
        return False
    storage.set_channel_permissions(channel_hash_hex, new_perms)
    invite_mgr.broadcast_permissions(channel_hash_hex)
    return True


def leave_channel(storage, subscription_mgr, channel_hash_hex: str) -> bool:
    """Returns False if the channel isn't in local storage."""
    channel = storage.get_channel(channel_hash_hex)
    if channel is None:
        return False
    subscription_mgr.unsubscribe(channel_hash_hex, channel["creator_hash"])
    return True


# ---------------------------------------------------------------------------
# Servers
#
# A server is a collection of channels sharing one membership and one role
# assignment. Most existing actions above need no server-specific variant:
# has_permission(), get_members() and the tenure lookups all resolve a channel
# to its owning server inside Storage, so compute_channel_recipients(),
# send_message() and update_membership() are already correct when handed a
# server channel or a server hash.
# ---------------------------------------------------------------------------


def create_server(server_mgr, invite_mgr, name: str, description: str = "",
                  permissions: dict | None = None) -> str:
    """Create a server and publish its initial member list document.

    Mirrors create_channel(): without that first signed document there is no
    root for _validate_document's trusted-signer chain to build on.
    """
    server_hash = server_mgr.create_server(
        name=name, description=description, permissions=permissions,
    )
    invite_mgr.publish_member_list(server_hash)
    return server_hash


def create_channel_in_server(storage, channel_mgr, invite_mgr,
                             server_hash_hex: str, actor_hash_hex: str,
                             name: str, description: str = "") -> str | None:
    """Create a channel inside a server.

    Returns None if the server is unknown or the actor lacks CREATE_CHANNEL
    (a silent no-op, matching compute_send_recipients). The channel inherits
    the server's permissions and membership; re-publishing the server's
    document is what carries the new channel to every member.
    """
    if storage.get_server(server_hash_hex) is None:
        return None
    if not storage.has_permission(server_hash_hex, actor_hash_hex, CREATE_CHANNEL):
        return None
    channel_hash = channel_mgr.create_channel(
        name=name, description=description,
        permissions=storage.get_server_permissions(server_hash_hex),
        server_hash=server_hash_hex,
    )
    invite_mgr.publish_member_list(server_hash_hex)
    return channel_hash


def edit_server_permissions(storage, invite_mgr, server_hash_hex: str,
                            actor_hash_hex: str, new_perms: dict) -> bool:
    """Server analogue of edit_channel_permissions.

    set_server_permissions mirrors the new dict into every child channel row
    in one transaction, so the direct channels.permissions readers across core
    stay correct.
    """
    if not storage.has_permission(server_hash_hex, actor_hash_hex, MANAGE_CHANNEL):
        return False
    storage.set_server_permissions(server_hash_hex, new_perms)
    invite_mgr.broadcast_permissions(server_hash_hex)
    return True


def leave_server(storage, subscription_mgr, server_hash_hex: str) -> bool:
    """Unsubscribe from every channel in a server.

    Local only, like leave_channel: membership rows are left for the next
    accepted document to replace.
    """
    if storage.get_server(server_hash_hex) is None:
        return False
    for row in storage.get_server_channels(server_hash_hex):
        subscription_mgr.unsubscribe(row["hash"], row["creator_hash"])
    return True
