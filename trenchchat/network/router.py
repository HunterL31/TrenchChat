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

# Ceiling on path requests issued for unknown quarantine sources. These fire
# before authentication -- source_hash is attacker-chosen wire data -- so
# without a bound each unsigned packet turns into a broadcast on the shared
# mesh, one for one.
PATH_REQUEST_WINDOW_SECS = 60.0
PATH_REQUEST_BURST = 12
PATH_REQUEST_MAX_SOURCES = 256

# Global ceiling on quarantine path requests, whatever source they claim. The
# per-source bucket is keyed on wire data an unauthenticated sender chooses, so
# rotating it makes every bucket fresh and restores the one-broadcast-per-packet
# amplification the per-source limit exists to prevent. This is the bound that
# actually holds; the per-source one only paces a single honest peer.
PATH_REQUEST_GLOBAL_BURST = 60


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
        self._outbound_callbacks: list = []
        # source_hash hex -> list of (received_at, LXMessage) awaiting identity
        self._quarantine: dict[str, list] = {}
        self._quarantine_lock = threading.Lock()
        # source_hash hex -> recent control-message timestamps
        self._control_rate: dict[str, list] = {}
        self._control_rate_lock = threading.Lock()
        # source_hash hex -> recent quarantine path-request timestamps
        self._path_request_rate: dict[str, list] = {}
        # Its own bucket, never the per-source one: key eviction there could
        # drop the global counter and reset the ceiling it enforces.
        self._path_request_global: dict[str, list] = {}
        self._path_request_lock = threading.Lock()

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
        if not self._allow_rate(self._control_rate, self._control_rate_lock, sender,
                                CONTROL_RATE_WINDOW_SECS, CONTROL_RATE_BURST,
                                CONTROL_RATE_MAX_SENDERS):
            RNS.log(
                f"TrenchChat: rate-limited control messages from {sender[:16]}…",
                RNS.LOG_WARNING,
            )
            return False
        return True

    @staticmethod
    def _allow_rate(bucket: dict[str, list], lock: threading.Lock, key: str,
                    window_secs: float, burst: int, max_keys: int) -> bool:
        """Sliding-window rate limit, bounded in the number of keys it tracks."""
        now = time.time()
        with lock:
            times = bucket.setdefault(key, [])
            times[:] = [t for t in times if now - t < window_secs]
            if len(times) >= burst:
                return False
            times.append(now)
            if len(bucket) > max_keys:
                for stale, stamps in list(bucket.items()):
                    if not stamps or now - stamps[-1] > window_secs:
                        del bucket[stale]
                # Eviction by age alone never fires under key rotation, where
                # every entry is fresh -- and past max_keys the scan above then
                # runs on every inbound packet, under this lock, on the
                # delivery thread. Drop oldest-first until the cap holds.
                while len(bucket) > max_keys:
                    oldest = min(bucket, key=lambda k: bucket[k][-1] if bucket[k] else 0)
                    del bucket[oldest]
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
        # Global bucket first: source_hex is unauthenticated wire data here, so
        # the per-source bucket below cannot bound a sender that varies it.
        if not self._allow_rate(self._path_request_global, self._path_request_lock,
                                "all", PATH_REQUEST_WINDOW_SECS,
                                PATH_REQUEST_GLOBAL_BURST, 1):
            return
        if not self._allow_rate(self._path_request_rate, self._path_request_lock,
                                source_hex, PATH_REQUEST_WINDOW_SECS,
                                PATH_REQUEST_BURST, PATH_REQUEST_MAX_SOURCES):
            return
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
            # Released messages count against the same throttle as any other
            # inbound control message; otherwise a peer can park a burst while
            # unknown and have it all delivered at once on announce.
            if not self._allow_control_message(revalidated):
                continue
            self._dispatch(revalidated)

    def add_delivery_callback(self, callback):
        if callback not in self._delivery_callbacks:
            self._delivery_callbacks.append(callback)

    def remove_delivery_callback(self, callback):
        if callback in self._delivery_callbacks:
            self._delivery_callbacks.remove(callback)

    def add_outbound_callback(self, callback):
        """Register a callback invoked with (dest_identity_hex: str) on every
        outbound send. Used by PresenceBeacon to suppress redundant beacons --
        must never be treated as evidence a peer received anything."""
        if callback not in self._outbound_callbacks:
            self._outbound_callbacks.append(callback)

    # --- send ---

    def send(self, message: LXMF.LXMessage):
        self._router.handle_outbound(message)
        self._notify_outbound(message)

    def _notify_outbound(self, message: LXMF.LXMessage) -> None:
        identity = getattr(message.destination, "identity", None)
        if identity is None:
            return
        dest_hex = identity.hash.hex()
        for cb in self._outbound_callbacks:
            try:
                cb(dest_hex)
            except Exception as e:
                RNS.log(f"TrenchChat: outbound callback error: {e}", RNS.LOG_ERROR)

    def stop(self) -> None:
        """Persist LXMF state and tear down delivery destinations.

        LXMF registers this as an atexit hook, but RNS exits the process with
        os._exit, which skips atexit entirely -- so shutdown has to call it.
        Safe to call twice; LXMF guards against re-entry.
        """
        try:
            self._router.exit_handler()
        except Exception as e:
            RNS.log(f"TrenchChat: LXMF shutdown error: {e}", RNS.LOG_ERROR)

    # --- propagation node ---

    def enable_propagation(self):
        try:
            limit_kb = self._config.propagation_storage_limit_mb * 1024
            self._router.set_message_storage_limit(kilobytes=limit_kb)
            self._router.enable_propagation()
            self._install_propagation_filter()
            self._config.propagation_enabled = True
            RNS.log("TrenchChat: propagation node enabled", RNS.LOG_NOTICE)
            if self._filter.relays_nothing():
                RNS.log(
                    "TrenchChat: propagation node is in allowlist mode with an "
                    "empty allowlist — it will relay nothing until channels are "
                    "added or the filter is set to 'all'",
                    RNS.LOG_WARNING,
                )
        except Exception as e:
            RNS.log(f"TrenchChat: failed to enable propagation node: {e}", RNS.LOG_ERROR)
            raise

    def _install_propagation_filter(self) -> None:
        """Apply the channel filter where messages enter the propagation store.

        The filter's verdict used to be returned from the delivery callback,
        whose return value LXMF ignores -- so the Settings option presented as
        controlling what this node relays for other people controlled nothing.
        This is the ingest LXMF actually calls, so refusing here is what keeps
        a message out of the store.
        """
        if getattr(self._router, "_tc_filter_installed", False):
            return
        original = self._router.lxmf_propagation

        def _filtered(lxmf_data, *args, **kwargs):
            if not self._filter.allows_packed(lxmf_data):
                return None
            return original(lxmf_data, *args, **kwargs)

        self._router.lxmf_propagation = _filtered
        self._router._tc_filter_installed = True

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
            # LXMF's own setter rejects anything that isn't a valid
            # destination hash, so it has no way to clear the node.
            self._router.outbound_propagation_node = None
            if self._router.outbound_propagation_link is not None:
                self._router.outbound_propagation_link.teardown()
                self._router.outbound_propagation_link = None

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
