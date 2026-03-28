"""
Manages the LXMFRouter lifecycle, propagation node enable/disable,
and wires the propagation filter into the inbound delivery callback.
"""

import RNS
import LXMF
import msgpack

from pathlib import Path
from trenchchat import APP_NAME, APP_ASPECT_USER
from trenchchat.config import Config, DATA_DIR
from trenchchat.network.prop_filter import PropagationFilter

_MESSAGE_STORE_PATH = str(DATA_DIR / "messagestore")


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

        for cb in self._delivery_callbacks:
            try:
                cb(message)
            except Exception as e:
                RNS.log(f"TrenchChat: delivery callback error: {e}", RNS.LOG_ERROR)

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
