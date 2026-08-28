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

from trenchchat.core.node_browser import parse_nomad_url
from trenchchat.core.permissions import (
    CREATE_CHANNEL, KICK, MANAGE_CHANNEL, MANAGE_ROLES, SEND_MESSAGE,
    VOICE_CHAT, is_open_join, permissions_from_json,
)

MAX_THEME_NAME_LEN = 64


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


def join_voice_channel(storage, voice_mgr, channel_hash_hex: str,
                       self_hash_hex: str) -> bool:
    """Outbound VOICE_CHAT guard + delegate to VoiceManager.join_voice.

    Returns False (silent no-op) if the channel is unknown, the caller lacks
    voice_chat on a non-open-join channel, or the join itself fails (already
    in a session, session full). Core-side enforcement in VoiceManager is
    still the real security boundary; this mirrors the GUI pre-flight guard.
    """
    channel = storage.get_channel(channel_hash_hex)
    if channel is None:
        return False
    perms = permissions_from_json(channel["permissions"])
    if not is_open_join(perms):
        if not storage.has_permission(channel_hash_hex, self_hash_hex, VOICE_CHAT):
            return False
    return voice_mgr.join_voice(channel_hash_hex)


def leave_voice_channel(voice_mgr) -> bool:
    """Returns False if not currently in a voice session."""
    if voice_mgr.current_channel is None:
        return False
    voice_mgr.leave_voice()
    return True


def set_voice_muted(voice_mgr, muted: bool) -> None:
    voice_mgr.set_muted(muted)


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


def leave_server(storage, subscription_mgr, server_hash_hex: str,
                 my_hex: str) -> bool:
    """Leave a server: unsubscribe from every channel and drop local membership.

    Local only. Removing our own membership row is what actually takes the
    server off /servers -- unsubscribing the channels alone left the row in
    place, so a "left" server reappeared and could never be cleared.
    """
    if storage.get_server(server_hash_hex) is None:
        return False
    for row in storage.get_server_channels(server_hash_hex):
        subscription_mgr.unsubscribe(row["hash"], row["creator_hash"])
    storage.remove_member(server_hash_hex, my_hex)
    return True


def channel_roster_hexes(storage, subscription_mgr,
                         channel_hash_hex: str) -> list[str]:
    """Identity hashes making up a channel's roster, for presence and quality.

    Open-join channels keep no members table -- their roster is the subscriber
    list -- so presence and link quality derived from members alone read empty
    forever. Invite-only channels keep using their membership.
    """
    channel = storage.get_channel(channel_hash_hex)
    if channel is None:
        return []
    perms = permissions_from_json(channel["permissions"])
    if is_open_join(perms):
        return sorted(subscription_mgr.get_subscribers(channel_hash_hex))
    return [row["identity_hash"] for row in storage.get_members(channel_hash_hex)]


# ---------------------------------------------------------------------------
# Friends and direct messages
#
# A direct message needs both peers to hold the other as an accepted friend.
# Every function here is a thin sequence over FriendsManager and
# DirectMessageManager; the gate itself lives in those managers, because it has
# to hold for a peer calling in over the network, not only for a frontend.
# ---------------------------------------------------------------------------


def send_friend_request(friends_mgr, peer_hash_hex: str, note: str = "",
                        nickname: str = "") -> bool:
    """Ask a peer to add us. False for a malformed hash or our own."""
    return friends_mgr.send_friend_request(peer_hash_hex, note=note,
                                           nickname=nickname)


def accept_friend_request(friends_mgr, peer_hash_hex: str,
                          nickname: str = "") -> bool:
    """False if that peer has not asked us.

    Words the peer sent while unaccepted are filed by FriendsManager's message
    filer, which every route to accepted goes through -- see
    file_message_requests.
    """
    return friends_mgr.accept_friend_request(peer_hash_hex, nickname=nickname)


def file_message_requests(friends_mgr, direct_mgr, messaging,
                          peer_hash_hex: str) -> int:
    """Move a newly accepted peer's held messages into their conversation.

    Wired into FriendsManager rather than called by each caller: a peer reaches
    accepted through the handshake, through a plain add, and through asking
    someone who had already asked them, and words left behind on any of those
    routes would be invisible with no way to get them back.

    Returns how many were filed.
    """
    held = friends_mgr.take_message_requests(peer_hash_hex)
    if not held:
        return 0
    conversation = direct_mgr.open_conversation(peer_hash_hex)
    if conversation is None:
        return 0
    if any(row["from_trenchchat"] for row in held):
        direct_mgr.note_trenchchat_peer(peer_hash_hex)
    for row in held:
        messaging.store_held_message(
            conversation, peer_hash_hex, row["body"], row["received_at"])
    return len(held)


def decline_friend_request(friends_mgr, peer_hash_hex: str) -> bool:
    """False if that peer has not asked us."""
    return friends_mgr.decline_friend_request(peer_hash_hex)


def open_dm(direct_mgr, peer_hash_hex: str) -> str | None:
    """The conversation with a peer, created on first use.

    None when they are not an accepted friend -- a silent no-op, matching
    compute_send_recipients.
    """
    return direct_mgr.open_conversation(peer_hash_hex)


def send_direct_message(direct_mgr, messaging, peer_hash_hex: str, content: str, *,
                        image_data: bytes | None = None,
                        reply_to: str | None = None) -> str | None:
    """Send a direct message. Returns its id, or None if the peer is not a friend."""
    if not direct_mgr.may_dm(peer_hash_hex):
        return None
    return messaging.send_direct(
        peer_hash_hex, content, reply_to=reply_to, image_data=image_data,
    )


def dm_recipients(direct_mgr, conversation_hash_hex: str,
                  trenchchat_only: bool = False) -> list[str] | None:
    """The delivery target set for a conversation: its other half, and nobody else.

    The counterpart of compute_channel_recipients, for the reaction broadcast
    path. None when the address is not a conversation we hold.

    trenchchat_only returns an empty list for a peer running another LXMF
    client: a reaction means nothing to one, and would arrive as an empty
    message. The reaction is still recorded locally, it simply is not sent.
    """
    peer = direct_mgr.peer_for(conversation_hash_hex)
    if peer is None or not direct_mgr.may_dm(peer):
        return None
    if trenchchat_only and not direct_mgr.peer_is_trenchchat(conversation_hash_hex):
        return []
    return [peer]


def conversation_recipients(storage, subscription_mgr, direct_mgr,
                            channel_hash_hex: str, self_hash_hex: str,
                            trenchchat_only: bool = False) -> list[str]:
    """Recipients for any address, conversation or channel.

    Lets a caller that does not know which kind it holds -- a reaction
    broadcast, say -- ask once. Pass trenchchat_only for traffic only
    TrenchChat understands, so it is not sent to another LXMF client.
    """
    if direct_mgr is not None and direct_mgr.is_conversation(channel_hash_hex):
        return dm_recipients(direct_mgr, channel_hash_hex,
                             trenchchat_only=trenchchat_only) or []
    return compute_channel_recipients(storage, subscription_mgr, channel_hash_hex,
                                      self_hash_hex)


# ---------------------------------------------------------------------------
# Settings
#
# The avatar has its own dedicated entry point (AvatarManager.set_avatar) and
# isn't part of the apply_settings surface below.
# ---------------------------------------------------------------------------


def set_display_name(router, display_name: str) -> None:
    """Change our display name and re-announce so peers learn it promptly.

    Re-announces both the LXMF delivery destination -- whose app_data carries
    the name peers recall via resolve_display_name -- and the trenchchat.user
    destination that feeds peer directories, so the change propagates without
    waiting for the next periodic reannounce.
    """
    router.set_display_name(display_name)
    router.announce()
    router.announce_user()


def read_settings(config) -> dict:
    """Snapshot of the propagation-node settings exposed through Settings."""
    return {
        "propagation_enabled": config.propagation_enabled,
        "propagation_node_name": config.propagation_node_name,
        "propagation_storage_limit_mb": config.propagation_storage_limit_mb,
        "outbound_propagation_node": config.outbound_propagation_node,
    }


def apply_settings(config, router, updates: dict) -> None:
    """
    Apply a partial settings update, same order as the Settings dialog's
    _on_accept: plain config writes first, then the router-backed field
    (propagation_enabled) last, so a router failure doesn't leave the simple
    fields unwritten. There is no rollback on a partial failure, matching
    Config's save-per-setter behaviour.

    Only keys present in updates are touched. outbound_propagation_node is
    read-only here: PropagationNodes.pin() owns it, because choosing a node
    means telling the live router, not only writing the setting.
    """
    if "propagation_node_name" in updates:
        config.propagation_node_name = updates["propagation_node_name"]
    if "propagation_storage_limit_mb" in updates:
        config.propagation_storage_limit_mb = updates["propagation_storage_limit_mb"]

    if "propagation_enabled" in updates:
        enabled = updates["propagation_enabled"]
        if enabled and not config.propagation_enabled:
            router.enable_propagation()
        elif not enabled and config.propagation_enabled:
            router.disable_propagation()


def read_ui_theme(config) -> dict:
    """The stored UI theme object, empty when never set. Interpreted client-side."""
    return config.ui_theme


def set_ui_theme(config, theme: dict) -> None:
    """Replace the stored UI theme object wholesale. Contents are not validated."""
    config.ui_theme = theme


def read_ui_theme_library(config) -> dict:
    """The saved themes by name, empty when none were saved. Interpreted client-side."""
    return config.ui_theme_library


def save_ui_theme_to_library(config, name: str, theme: dict) -> None:
    """
    Save a theme under a name, overwriting any theme already saved there.
    Raises ValueError if the name is empty or longer than MAX_THEME_NAME_LEN
    once stripped. Theme contents are not validated.
    """
    config.save_ui_theme(_validate_theme_name(name), theme)


def delete_ui_theme_from_library(config, name: str) -> bool:
    """Remove a saved theme. False when no theme is stored under that name."""
    return config.delete_ui_theme(name.strip())


def _validate_theme_name(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        raise ValueError("theme name must not be empty")
    if len(stripped) > MAX_THEME_NAME_LEN:
        raise ValueError(f"theme name must be at most {MAX_THEME_NAME_LEN} characters")
    return stripped


# ---------------------------------------------------------------------------
# Nomad Network page browsing
# ---------------------------------------------------------------------------


def browse_nomad_url(node_browser, url: str, *,
                     current_node_hex: str | None = None) -> dict:
    """Parse a nomad URL and start the fetch it names.

    Accepts "<hash>:/page/x.mu", a relative ":/page/x.mu" (resolved against
    current_node_hex), or a bare "<hash>" (meaning /page/index.mu). Returns
    {"fetch_id", "node_hash", "path", "kind"}. Raises ValueError for a
    malformed URL or a relative URL with no current node.
    """
    node_hex, path = parse_nomad_url(url)
    if node_hex is None:
        if not current_node_hex:
            raise ValueError("relative url with no current node")
        node_hex = current_node_hex
    if path.startswith("/file/"):
        kind = "file"
        fetch_id = node_browser.fetch_file(node_hex, path)
    else:
        kind = "page"
        fetch_id = node_browser.fetch_page(node_hex, path)
    return {"fetch_id": fetch_id, "node_hash": node_hex, "path": path,
            "kind": kind}


def set_node_hosting(node_browser, *, enabled: bool | None = None,
                     node_name: str | None = None) -> dict:
    """Apply a partial nomad hosting update and return the new status."""
    if node_name is not None and not node_name.strip() and enabled:
        raise ValueError("node name must not be empty")
    return node_browser.set_hosting(enabled=enabled, node_name=node_name)


def friends_with_pages(friends_mgr, node_browser) -> list[dict]:
    """The friends list, each entry carrying "nomad_node_hash" when that
    friend's node has been heard on the mesh (None otherwise), so a client
    can offer to open their page."""
    friends = friends_mgr.get_friends()
    for friend in friends:
        node = node_browser.node_for_identity(friend["identity_hash"])
        friend["nomad_node_hash"] = node["node_hash"] if node else None
    return friends
