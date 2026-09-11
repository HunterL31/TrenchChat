"""
The transport-neutral facade every manager sends and receives through.

A manager hands Router a peer's identity hash and a field dict; Router picks
the path to that peer, asks it to carry the message and reports back what it
did. Nothing above this line knows whether the bytes went over Reticulum, and
nothing below it knows what the fields mean.

What stays here is what holds on any path: unwrapping the protocol envelope,
the per-sender ceiling on inbound control messages, and dispatching to the
managers. What is Reticulum's own lives in network/lxmf_transport.py.
"""

import threading

import RNS

from trenchchat.config import Config
from trenchchat.core.protocol import F_MSG_TYPE, is_protocol_envelope, unpack_fields
from trenchchat.network.base import (
    InboundMessage, SendState, Transport, TransportLimits,
)
from trenchchat.network.lxmf_transport import (
    LXMFTransport, REANNOUNCE_INTERVAL_SECS, allow_rate,
)

# Per-sender ceiling on inbound control messages.
CONTROL_RATE_WINDOW_SECS = 60.0
CONTROL_RATE_BURST = 60
CONTROL_RATE_MAX_SENDERS = 512


class Router:
    """Routes messages between the managers and the path that carries them."""

    def __init__(self, config: Config, identity, storagepath: str | None = None,
                 transport: Transport | None = None):
        """
        identity: trenchchat.core.identity.Identity instance
        (passed in to avoid circular imports)
        storagepath: override for the LXMF message store directory
        transport: the path to use instead of building the Reticulum one
        """
        self._config = config
        self._delivery_callbacks: list = []
        self._outbound_callbacks: list = []
        self._announce_callbacks: list = []
        # source identity hex -> recent control-message timestamps
        self._control_rate: dict[str, list] = {}
        self._control_rate_lock = threading.Lock()

        self._peer_appeared_callbacks: list = []
        self._identity_resolved_callbacks: list = []
        self._channel_discovered_callbacks: list = []
        self._user_discovered_callbacks: list = []
        self._node_discovered_callbacks: list = []
        self._propagation_node_callbacks: list = []
        self._path_changed_callbacks: list = []

        self._transport = transport or LXMFTransport(config, identity, storagepath)
        self._transport.set_inbound_callback(self._on_inbound)
        self._transport.set_peer_event_callbacks(
            peer_appeared=self._fire_peer_appeared,
            identity_resolved=self._fire_identity_resolved,
            channel_discovered=self._fire_channel_discovered,
            user_discovered=self._fire_user_discovered,
            node_discovered=self._fire_node_discovered,
            propagation_node_heard=self._fire_propagation_node_heard,
            path_changed=self._fire_path_changed,
        )

    # --- delivery ---

    def _on_inbound(self, message: InboundMessage) -> None:
        """Every authenticated message from every path arrives here."""
        if not self._unwrap_envelope(message):
            return
        if not self._allow_control_message(message):
            return
        self._dispatch(message)

    def _unwrap_envelope(self, message: InboundMessage) -> bool:
        """Replace message.fields with the TrenchChat dict inside its envelope.

        Handlers downstream only ever see unwrapped fields; the registry's
        numbers never appear as LXMF field keys on the wire. A message with
        no envelope of ours -- a direct message, or any other client's
        traffic -- passes through untouched and unmarked; one that claims
        the envelope with an unreadable payload is dropped. A path that
        carries the field dict as it is says so itself, and is left alone.
        """
        if message.trenchchat_protocol:
            return True
        fields = message.fields or {}
        inner = unpack_fields(fields)
        if inner is not None:
            message.fields = inner
            message.trenchchat_protocol = True
            return True
        if is_protocol_envelope(fields):
            RNS.log(
                f"TrenchChat: dropped message with unreadable protocol "
                f"envelope from {message.source_hex[:16]}…",
                RNS.LOG_WARNING,
            )
            return False
        return True

    def _allow_control_message(self, message: InboundMessage) -> bool:
        """Throttle control messages per sender.

        Chat messages are exempt; a limit there would drop conversation.
        """
        fields = message.fields or {}
        if F_MSG_TYPE not in fields:
            return True
        sender = message.source_hex
        if not sender:
            return True

        burst = self._transport.limits_for(sender).control_messages_per_minute
        if not allow_rate(self._control_rate, self._control_rate_lock, sender,
                          CONTROL_RATE_WINDOW_SECS, burst,
                          CONTROL_RATE_MAX_SENDERS):
            RNS.log(
                f"TrenchChat: rate-limited control messages from {sender[:16]}…",
                RNS.LOG_WARNING,
            )
            return False
        return True

    def _dispatch(self, message: InboundMessage) -> None:
        for cb in self._delivery_callbacks:
            try:
                cb(message)
            except Exception as e:
                RNS.log(f"TrenchChat: delivery callback error: {e}", RNS.LOG_ERROR)

    def add_delivery_callback(self, callback) -> None:
        """Register a callback invoked with an InboundMessage for every message."""
        if callback not in self._delivery_callbacks:
            self._delivery_callbacks.append(callback)

    def remove_delivery_callback(self, callback) -> None:
        """Stop delivering inbound messages to this callback."""
        if callback in self._delivery_callbacks:
            self._delivery_callbacks.remove(callback)

    def add_outbound_callback(self, callback) -> None:
        """Register a callback invoked with (dest_identity_hex: str) on every
        outbound send. Used by PresenceBeacon to suppress redundant beacons --
        must never be treated as evidence a peer received anything."""
        if callback not in self._outbound_callbacks:
            self._outbound_callbacks.append(callback)

    # --- send ---

    def send(self, dest_hex: str, fields: dict, content: str = "", *,
             on_delivered=None, on_failed=None, propagated: bool = False,
             envelope: bool = True) -> SendState:
        """Send one message to a peer named by identity hash.

        fields is the TrenchChat field dict. envelope=False sends the keys
        exactly as given, which is what a direct message does so a client that
        is not TrenchChat can read it. on_delivered and on_failed are called
        with the destination hex when the path has news of the message.

        NO_PATH means the peer could not be addressed: the caller queues for
        retry, hints a missed delivery, or falls back to a propagation node.
        """
        state = self._transport.send(
            dest_hex, fields, content, on_delivered=on_delivered,
            on_failed=on_failed, propagated=propagated, envelope=envelope,
        )
        if state is not SendState.NO_PATH:
            self._notify_outbound(dest_hex)
        return state

    def can_reach(self, dest_hex: str) -> bool:
        """Whether a send to this peer can be addressed right now."""
        return self._transport.can_reach(dest_hex)

    def request_path(self, dest_hex: str) -> None:
        """Ask the network where a peer is. Never blocks, never waits."""
        self._transport.request_path(dest_hex)

    def limits_for(self, dest_hex: str) -> TransportLimits:
        """The budgets a message to this peer travels under."""
        return self._transport.limits_for(dest_hex)

    def drain(self, timeout: float) -> int:
        """Wait for outbound messages to stop moving. Returns how many settled."""
        return self._transport.drain(timeout)

    def _notify_outbound(self, dest_hex: str) -> None:
        for cb in self._outbound_callbacks:
            try:
                cb(dest_hex)
            except Exception as e:
                RNS.log(f"TrenchChat: outbound callback error: {e}", RNS.LOG_ERROR)

    # --- peer keys ---

    def public_key_for(self, peer_hex: str) -> bytes | None:
        """A peer's public key, if any path knows it."""
        return self._transport.public_key_for(peer_hex)

    def resolve_address(self, address_hex: str) -> str | None:
        """The identity hash behind a path-specific address, if it is known yet.

        Requests whatever the path needs to learn it, so asking again later
        answers. None until then.
        """
        return self._transport.resolve_address(address_hex)

    # --- peer events ---

    def add_peer_appeared_callback(self, callback) -> None:
        """callback(peer_hex: str, interface): a peer was heard from."""
        if callback not in self._peer_appeared_callbacks:
            self._peer_appeared_callbacks.append(callback)

    def add_identity_resolved_callback(self, callback) -> None:
        """callback(peer_hex: str): a peer's identity became known."""
        if callback not in self._identity_resolved_callbacks:
            self._identity_resolved_callbacks.append(callback)

    def add_channel_discovered_callback(self, callback) -> None:
        """callback(channel_hash_hex, creator_hex, metadata, interface)."""
        if callback not in self._channel_discovered_callbacks:
            self._channel_discovered_callbacks.append(callback)

    def add_user_discovered_callback(self, callback) -> None:
        """callback(peer_hex, display_name, interface): a TrenchChat peer."""
        if callback not in self._user_discovered_callbacks:
            self._user_discovered_callbacks.append(callback)

    def add_node_discovered_callback(self, callback) -> None:
        """callback(node_hash_hex, display_name, interface): a Nomad node."""
        if callback not in self._node_discovered_callbacks:
            self._node_discovered_callbacks.append(callback)

    def add_propagation_node_heard_callback(self, callback) -> None:
        """callback(node_hash_hex, hops): a propagation node announced."""
        if callback not in self._propagation_node_callbacks:
            self._propagation_node_callbacks.append(callback)

    def add_path_changed_callback(self, callback) -> None:
        """callback(peer_hex, path): the path to a peer changed.

        Registered now and fired by nothing: every peer is on the Reticulum
        path until there is a second one to move to.
        """
        if callback not in self._path_changed_callbacks:
            self._path_changed_callbacks.append(callback)

    @staticmethod
    def _fire(callbacks: list, label: str, *args) -> None:
        for cb in callbacks:
            try:
                cb(*args)
            except Exception as e:
                RNS.log(f"TrenchChat: {label} callback error: {e}", RNS.LOG_ERROR)

    def _fire_peer_appeared(self, peer_hex: str, iface) -> None:
        self._fire(self._peer_appeared_callbacks, "peer appeared", peer_hex, iface)

    def _fire_identity_resolved(self, peer_hex: str) -> None:
        self._fire(self._identity_resolved_callbacks, "identity resolved", peer_hex)

    def _fire_channel_discovered(self, channel_hash_hex: str, creator_hex: str,
                                 metadata: dict, iface) -> None:
        self._fire(self._channel_discovered_callbacks, "channel discovered",
                   channel_hash_hex, creator_hex, metadata, iface)

    def _fire_user_discovered(self, peer_hex: str, display_name: str,
                              iface) -> None:
        self._fire(self._user_discovered_callbacks, "user discovered",
                   peer_hex, display_name, iface)

    def _fire_node_discovered(self, node_hex: str, display_name: str,
                              iface) -> None:
        self._fire(self._node_discovered_callbacks, "node discovered",
                   node_hex, display_name, iface)

    def _fire_propagation_node_heard(self, node_hex: str, hops: int) -> None:
        self._fire(self._propagation_node_callbacks, "propagation node",
                   node_hex, hops)

    def _fire_path_changed(self, peer_hex: str, path: str) -> None:
        self._fire(self._path_changed_callbacks, "path changed", peer_hex, path)

    # --- announce ---

    def add_announce_callback(self, callback) -> None:
        """callback(attached_interface): announce whatever else this node owns.

        Called as part of announce_all, so the first-contact answer and the
        re-announce heartbeat carry owned channels without the transport
        having to know what a channel is.
        """
        if callback not in self._announce_callbacks:
            self._announce_callbacks.append(callback)

    def announce(self, attached_interface=None) -> None:
        """Announce this node's own address."""
        self._transport.announce(attached_interface=attached_interface)

    def announce_user(self, attached_interface=None) -> None:
        """Announce that this identity runs TrenchChat."""
        self._transport.announce_user(attached_interface=attached_interface)

    def announce_all(self, attached_interface=None) -> None:
        """Announce this node, its TrenchChat aspect and everything it owns."""
        self.announce(attached_interface=attached_interface)
        self.announce_user(attached_interface=attached_interface)
        self._fire(self._announce_callbacks, "announce", attached_interface)

    def register_channel(self, channel_hash_hex: str, aspect_name: str) -> None:
        """Take ownership of a channel's address on the path."""
        self._transport.register_channel(channel_hash_hex, aspect_name)

    def announce_channel(self, channel_hash_hex: str, app_data: bytes,
                         attached_interface=None) -> None:
        """Announce one owned channel with its discovery metadata."""
        self._transport.announce_channel(channel_hash_hex, app_data,
                                         attached_interface=attached_interface)

    def start_reannounce(self, interval_secs: float = REANNOUNCE_INTERVAL_SECS
                         ) -> None:
        """Re-announce on a timer, and answer peers met since the last tick."""
        start = getattr(self._transport, "start_reannounce", None)
        if start is not None:
            self._transport.set_announce_all(self.announce_all)
            start(interval_secs)

    def first_contact_tick(self, now: float | None = None) -> bool:
        """Send a queued answer to peers met since the last tick."""
        tick = getattr(self._transport, "first_contact_tick", None)
        return bool(tick(now)) if tick is not None else False

    def set_display_name(self, display_name: str) -> None:
        """Update the display name this node announces."""
        self._transport.set_display_name(display_name)

    def release_quarantined(self, identity_hex: str) -> None:
        """Re-deliver anything held for an identity that has just resolved."""
        self._transport.release_quarantined(identity_hex)

    # --- propagation ---

    def enable_propagation(self) -> None:
        """Host a propagation node on this instance."""
        self._transport.enable_propagation()

    def disable_propagation(self) -> None:
        """Stop hosting a propagation node."""
        self._transport.disable_propagation()

    @property
    def outbound_propagation_node(self) -> bytes | None:
        """The node this client leaves offline direct messages with, if any."""
        return self._transport.outbound_propagation_node

    def set_outbound_propagation_node(self, destination_hash: bytes) -> None:
        """Choose the node to leave offline direct messages with."""
        self._transport.set_outbound_propagation_node(destination_hash)

    def request_propagation_sync(self) -> bool:
        """Collect anything a propagation node is holding for us.

        False if no node is configured, or the request could not be started.
        """
        return self._transport.request_propagation_sync()

    def propagation_sync_state(self) -> int:
        """The transfer state of the last collection attempt."""
        return self._transport.propagation_sync_state()

    # --- lifecycle ---

    def stop(self) -> None:
        """Persist state and tear the path down. Safe to call twice."""
        self._transport.stop()

    @property
    def transport(self) -> Transport:
        """The path this router sends over. For wiring and teardown only."""
        return self._transport
