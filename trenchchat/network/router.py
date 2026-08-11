"""
Manages the LXMFRouter lifecycle, propagation node enable/disable,
and wires the propagation filter into the inbound delivery callback.

This module is the single choke point for inbound authentication: no message
reaches a delivery callback unless its LXMF signature validated.  Messages
whose source identity is not yet known are held in a bounded quarantine and
re-validated from their packed bytes once the identity resolves.
"""

import time
import threading

import RNS
import LXMF
import msgpack

from pathlib import Path
from trenchchat import APP_NAME, APP_ASPECT_USER
from trenchchat.config import Config, DATA_DIR
from trenchchat.core.protocol import F_MSG_TYPE
from trenchchat.network.prop_filter import PropagationFilter

_MESSAGE_STORE_PATH = str(DATA_DIR / "messagestore")

# Quarantine bounds for messages awaiting sender-identity resolution.  Both a
# per-sender and a global cap apply so the quarantine cannot itself be used as
# a memory-exhaustion vector by a peer that never announces.
QUARANTINE_TTL_SECS = 300
QUARANTINE_MAX_PER_SENDER = 8
QUARANTINE_MAX_TOTAL = 128

# Per-sender ceiling on inbound control messages.
CONTROL_RATE_WINDOW_SECS = 60.0
CONTROL_RATE_BURST = 60
CONTROL_RATE_MAX_SENDERS = 512


def delivery_hash_for_identity(identity_hash: bytes) -> bytes:
    """Return the LXMF delivery destination hash for an identity hash."""
    return RNS.Destination.hash(identity_hash, "lxmf", "delivery")


class Router:
    def __init__(self, config: Config, identity, storagepath: str | None = None):
        """
        identity: trenchchat.core.identity.Identity instance
        (passed in to avoid circular imports)
        storagepath: override for the LXMF message store directory
        """
        self._config = config
        self._identity = identity
        self._filter = PropagationFilter(config)
        self._delivery_callbacks: list = []
        # source_hash hex -> list of (received_at, LXMessage) awaiting identity
        self._quarantine: dict[str, list] = {}
        self._quarantine_lock = threading.Lock()
        # source_hash hex -> recent control-message timestamps
        self._control_rate: dict[str, list] = {}
        self._control_rate_lock = threading.Lock()

        self._router = LXMF.LXMRouter(
            storagepath=storagepath or _MESSAGE_STORE_PATH,
            identity=identity.rns_identity,
            name=config.propagation_node_name or None,
        )

        # Register our delivery destination with the router.
        self._delivery_dest = self._router.register_delivery_identity(
            identity.rns_identity,
            display_name=config.display_name,
        )

        # Register a dedicated trenchchat.user destination so TrenchChat peers
        # can be distinguished from generic LXMF clients on the network.
        self._user_dest = RNS.Destination(
            identity.rns_identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT_USER,
        )

        self._router.register_delivery_callback(self._on_message_received)

        # Configure outbound propagation node if set.
        if config.outbound_propagation_node:
            try:
                node_hash = bytes.fromhex(config.outbound_propagation_node)
                self._router.set_outbound_propagation_node(node_hash)
            except ValueError:
                RNS.log("TrenchChat: invalid outbound propagation node hash in config",
                        RNS.LOG_WARNING)

        # Enable propagation node mode if configured.
        if config.propagation_enabled:
            self.enable_propagation()

    # --- delivery ---

    def _on_message_received(self, message: LXMF.LXMessage):
        """Called by LXMFRouter for every inbound message."""
        # The filter governs what we store and forward for others; it must
        # not gate messages addressed to us.
        if (self._config.propagation_enabled
                and not self._addressed_to_us(message)
                and not self._filter.allows(message)):
            return

        if not self._authenticate(message):
            return

        if not self._allow_control_message(message):
            return

        self._dispatch(message)

    def _allow_control_message(self, message: LXMF.LXMessage) -> bool:
        """Throttle control messages per sender.

        Chat messages are exempt; a limit there would drop conversation.
        """
        fields = getattr(message, "fields", None) or {}
        if F_MSG_TYPE not in fields:
            return True
        if not message.source_hash:
            return True

        sender = message.source_hash.hex()
        now = time.time()
        with self._control_rate_lock:
            times = self._control_rate.setdefault(sender, [])
            times[:] = [t for t in times if now - t < CONTROL_RATE_WINDOW_SECS]
            if len(times) >= CONTROL_RATE_BURST:
                RNS.log(
                    f"TrenchChat: rate-limited control messages from "
                    f"{sender[:16]}…",
                    RNS.LOG_WARNING,
                )
                return False
            times.append(now)
            if len(self._control_rate) > CONTROL_RATE_MAX_SENDERS:
                for stale, stamps in list(self._control_rate.items()):
                    if not stamps or now - stamps[-1] > CONTROL_RATE_WINDOW_SECS:
                        del self._control_rate[stale]
        return True

    def _addressed_to_us(self, message: LXMF.LXMessage) -> bool:
        """True if this message was delivered to our own LXMF destination."""
        try:
            return message.destination_hash == self._delivery_dest.hash
        except AttributeError:
            return False

    def _dispatch(self, message: LXMF.LXMessage):
        for cb in self._delivery_callbacks:
            try:
                cb(message)
            except Exception as e:
                RNS.log(f"TrenchChat: delivery callback error: {e}", RNS.LOG_ERROR)

    # --- inbound authentication ---

    def _authenticate(self, message: LXMF.LXMessage) -> bool:
        """Return True only if the message's LXMF signature validated."""
        if getattr(message, "signature_validated", False):
            return True

        reason = getattr(message, "unverified_reason", None)
        source_hex = message.source_hash.hex() if message.source_hash else "<none>"

        if reason == LXMF.LXMessage.SOURCE_UNKNOWN:
            self._quarantine_message(message)
            return False

        RNS.log(
            f"TrenchChat: dropped inbound message with invalid signature, "
            f"claimed source {source_hex[:16]}…",
            RNS.LOG_WARNING,
        )
        return False

    def _quarantine_message(self, message: LXMF.LXMessage):
        """Hold a message whose sender identity is not yet known.

        Messages with no packed representation cannot be re-validated later,
        so they are dropped rather than held.
        """
        if not message.source_hash or not getattr(message, "packed", None):
            RNS.log(
                "TrenchChat: dropped unverifiable message with unknown source",
                RNS.LOG_WARNING,
            )
            return

        source_hex = message.source_hash.hex()
        now = time.time()

        with self._quarantine_lock:
            self._prune_quarantine_locked(now)
            queued = self._quarantine.setdefault(source_hex, [])
            if len(queued) >= QUARANTINE_MAX_PER_SENDER:
                queued.pop(0)
            total = sum(len(v) for v in self._quarantine.values())
            if total >= QUARANTINE_MAX_TOTAL:
                oldest_key = min(
                    self._quarantine,
                    key=lambda k: self._quarantine[k][0][0] if self._quarantine[k] else now,
                )
                self._quarantine[oldest_key].pop(0)
                if not self._quarantine[oldest_key]:
                    del self._quarantine[oldest_key]
                queued = self._quarantine.setdefault(source_hex, [])
            queued.append((now, message))

        RNS.log(
            f"TrenchChat: quarantined message from unknown source "
            f"{source_hex[:16]}… pending identity resolution",
            RNS.LOG_DEBUG,
        )
        try:
            RNS.Transport.request_path(message.source_hash)
        except Exception as e:
            RNS.log(f"TrenchChat: path request failed for {source_hex[:16]}…: {e}",
                    RNS.LOG_DEBUG)

    def _prune_quarantine_locked(self, now: float):
        for key in list(self._quarantine.keys()):
            kept = [(ts, m) for ts, m in self._quarantine[key]
                    if now - ts < QUARANTINE_TTL_SECS]
            if kept:
                self._quarantine[key] = kept
            else:
                del self._quarantine[key]

    def release_quarantined(self, identity_hash_hex: str):
        """Re-validate and dispatch messages held for a now-known identity.

        Each message is re-unpacked from its original bytes so LXMF re-runs
        the signature check against the newly recalled identity.
        """
        try:
            source_hash = delivery_hash_for_identity(bytes.fromhex(identity_hash_hex))
        except ValueError:
            return
        source_hex = source_hash.hex()

        with self._quarantine_lock:
            self._prune_quarantine_locked(time.time())
            held = self._quarantine.pop(source_hex, [])

        for _ts, message in held:
            try:
                revalidated = LXMF.LXMessage.unpack_from_bytes(message.packed)
            except Exception as e:
                RNS.log(f"TrenchChat: could not re-validate quarantined message: {e}",
                        RNS.LOG_WARNING)
                continue
            if revalidated is None or not getattr(revalidated, "signature_validated", False):
                RNS.log(
                    f"TrenchChat: dropped quarantined message from {source_hex[:16]}… "
                    f"— signature still invalid after identity resolution",
                    RNS.LOG_WARNING,
                )
                continue
            self._dispatch(revalidated)

    def add_delivery_callback(self, callback):
        if callback not in self._delivery_callbacks:
            self._delivery_callbacks.append(callback)

    def remove_delivery_callback(self, callback):
        if callback in self._delivery_callbacks:
            self._delivery_callbacks.remove(callback)

    # --- send ---

    def send(self, message: LXMF.LXMessage):
        self._router.handle_outbound(message)

    # --- propagation node ---

    def enable_propagation(self):
        try:
            limit_kb = self._config.propagation_storage_limit_mb * 1024
            self._router.set_message_storage_limit(kilobytes=limit_kb)
            self._router.enable_propagation()
            self._config.propagation_enabled = True
            RNS.log("TrenchChat: propagation node enabled", RNS.LOG_NOTICE)
        except Exception as e:
            RNS.log(f"TrenchChat: failed to enable propagation node: {e}", RNS.LOG_ERROR)
            raise

    def disable_propagation(self):
        self._router.disable_propagation()
        self._config.propagation_enabled = False
        RNS.log("TrenchChat: propagation node disabled", RNS.LOG_NOTICE)

    def set_outbound_propagation_node(self, hex_hash: str | None):
        self._config.outbound_propagation_node = hex_hash
        if hex_hash:
            node_hash = bytes.fromhex(hex_hash)
            self._router.set_outbound_propagation_node(node_hash)
            self._router.request_messages_from_propagation_node(
                self._identity.rns_identity
            )
        else:
            self._router.set_outbound_propagation_node(None)

    def sync_from_propagation_node(self):
        """Manually trigger a sync pull from the configured propagation node."""
        if self._config.outbound_propagation_node:
            self._router.request_messages_from_propagation_node(
                self._identity.rns_identity
            )

    def set_display_name(self, display_name: str) -> None:
        """Update the display name broadcast in LXMF delivery announces."""
        self._delivery_dest.display_name = display_name
        self._config.display_name = display_name

    # --- announce ---

    def announce(self, attached_interface=None) -> None:
        """Announce our LXMF delivery destination.

        If attached_interface is given the announce is sent only on that
        interface; otherwise it is broadcast on all interfaces.
        """
        self._router.announce(self._delivery_dest.hash,
                              attached_interface=attached_interface)

    def announce_user(self, attached_interface=None) -> None:
        """Announce our trenchchat.user destination with the current display name.

        This allows other TrenchChat instances to identify us as a TrenchChat
        peer and add us to their user directory for discovery and invite lookup.
        If attached_interface is given the announce is sent only on that
        interface; otherwise it is broadcast on all interfaces.
        """
        app_data = msgpack.packb(
            {"name": self._config.display_name or ""},
            use_bin_type=True,
        )
        self._user_dest.announce(app_data=app_data,
                                 attached_interface=attached_interface)

    @property
    def lxmf_router(self) -> LXMF.LXMRouter:
        return self._router

    @property
    def delivery_destination(self):
        return self._delivery_dest
