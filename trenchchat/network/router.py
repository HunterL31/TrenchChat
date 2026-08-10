"""
Manages the LXMFRouter lifecycle, propagation node enable/disable,
and wires the propagation filter into the inbound delivery callback.

Inbound authentication
----------------------
LXMF signs every message with the sender's Ed25519 key, but a failed
signature is *not* fatal inside LXMF: ``LXMessage.unpack_from_bytes`` only
records the outcome on ``signature_validated`` / ``unverified_reason``, and
``LXMRouter`` invokes the delivery callback regardless.  ``source_hash`` is
attacker-chosen wire data, so any handler that derives a sender identity from
it is trusting an unauthenticated value unless the signature flag is checked
first.  This module is the single choke point where that check happens: no
message reaches a delivery callback unless its signature validated.

Messages whose source identity is not yet known (``SOURCE_UNKNOWN``) are not
forgeries -- we simply have not received the sender's announce yet -- so they
are held in a bounded quarantine while a path request is issued, and
re-validated from their original packed bytes once the identity resolves.
"""

import time
import threading

import RNS
import LXMF
import msgpack

from pathlib import Path
from trenchchat import APP_NAME, APP_ASPECT_USER
from trenchchat.config import Config, DATA_DIR
from trenchchat.network.prop_filter import PropagationFilter

_MESSAGE_STORE_PATH = str(DATA_DIR / "messagestore")

# Quarantine bounds for messages awaiting sender-identity resolution.  Both a
# per-sender and a global cap apply so the quarantine cannot itself be used as
# a memory-exhaustion vector by a peer that never announces.
QUARANTINE_TTL_SECS = 300
QUARANTINE_MAX_PER_SENDER = 8
QUARANTINE_MAX_TOTAL = 128


def delivery_hash_for_identity(identity_hash: bytes) -> bytes:
    """Return the LXMF delivery destination hash for an identity hash.

    The identity hash and the delivery destination hash are different values;
    inbound ``source_hash`` is the latter.  Callers holding an identity hash
    must convert before comparing against anything derived from the wire.
    """
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
        # When acting as a propagation node, filter before storing.
        if self._config.propagation_enabled:
            if not self._filter.allows(message):
                return

        if not self._authenticate(message):
            return

        self._dispatch(message)

    def _dispatch(self, message: LXMF.LXMessage):
        for cb in self._delivery_callbacks:
            try:
                cb(message)
            except Exception as e:
                RNS.log(f"TrenchChat: delivery callback error: {e}", RNS.LOG_ERROR)

    # --- inbound authentication ---

    def _authenticate(self, message: LXMF.LXMessage) -> bool:
        """Return True only if the message's LXMF signature validated.

        A message whose signature is present but wrong is a forgery attempt and
        is dropped outright.  A message whose source identity is simply not
        known yet is held for re-validation rather than discarded.
        """
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

        Without the sender's identity LXMF cannot check the signature, so the
        message is neither trusted nor discarded.  A path request is issued;
        release happens from ``release_quarantined`` when the announce lands.
        Messages with no packed representation cannot be re-validated later and
        are dropped immediately.
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

        Called when a peer announce resolves an identity we were waiting on.
        Each held message is re-unpacked from its original bytes so LXMF
        re-runs the signature check against the newly recalled identity --
        arrival of a path is not itself evidence the message was genuine.
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
