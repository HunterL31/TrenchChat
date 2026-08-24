"""
Choosing an LXMF propagation node to hand offline direct messages to.

A channel survives an absent member because any other member can serve them
the gap later. A conversation has nobody else in it, so an offline peer's
messages have to be left somewhere until they collect them: an LXMF
propagation node, which stores the message and hands it over when they ask.

Two things follow from that, and both are the reason this module exists:

  * LXMF refuses to send a propagated message unless an outbound node is
    configured -- it fails the message rather than queueing it -- so one has
    to be chosen before the first offline send, not after.
  * LXMF tracks nodes only for its own propagation-node mode. A client that
    merely *uses* a node has to notice them announcing and pick one itself.

A node sees a message's ciphertext, its size, and both endpoints' addresses;
it can never read the content, which is encrypted end to end to the recipient.
Preferring the fewest hops keeps that exposure as local as the mesh allows.
"""

import threading
import time

import RNS

# A node not heard from in this long is assumed gone. Propagation nodes
# announce far less often than peers do, so this is generous.
NODE_TTL_SECS = 6 * 3600

# Nodes remembered at once. Announces are free to send, so the TTL alone
# bounds nothing.
MAX_TRACKED_NODES = 64


class PropagationNodes:
    """Propagation nodes heard on the mesh, and which one we send through."""

    def __init__(self, config, router, ttl_secs: float = NODE_TTL_SECS) -> None:
        self._config = config
        self._router = router
        self._ttl = ttl_secs
        # destination hash hex -> (hops, last_heard)
        self._nodes: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._selected: str | None = None
        self._callbacks: list = []
        self._apply_pinned()

    # --- public API ---

    def add_selection_callback(self, cb) -> None:
        """Register a callback invoked with (node_hex: str | None) on a change."""
        self._callbacks.append(cb)

    def record_node(self, destination_hash_hex: str, hops: int) -> None:
        """Note a propagation node announce, and reconsider our choice."""
        now = time.time()
        with self._lock:
            if (len(self._nodes) >= MAX_TRACKED_NODES
                    and destination_hash_hex not in self._nodes):
                oldest = min(self._nodes, key=lambda k: self._nodes[k][1])
                del self._nodes[oldest]
            self._nodes[destination_hash_hex] = (hops, now)
        self.reselect()

    def known_nodes(self) -> list[dict]:
        """Every node still within the TTL, nearest first."""
        now = time.time()
        with self._lock:
            entries = [
                {"hash": node, "hops": hops, "last_heard": heard,
                 "selected": node == self._selected}
                for node, (hops, heard) in self._nodes.items()
                if now - heard < self._ttl
            ]
        entries.sort(key=lambda e: (e["hops"], -e["last_heard"]))
        return entries

    @property
    def selected(self) -> str | None:
        """The node offline direct messages currently go through."""
        return self._selected

    @property
    def pinned(self) -> str:
        """The user's chosen node, or empty when selection is automatic."""
        return self._config.outbound_propagation_node

    def pin(self, node_hex: str) -> bool:
        """Fix the node to use, or pass an empty string to go back to automatic.

        Returns False for a malformed hash.
        """
        try:
            self._config.outbound_propagation_node = node_hex
        except ValueError:
            return False
        if self._config.outbound_propagation_node:
            self._apply_pinned()
        else:
            self._selected = None
            self.reselect()
        return True

    def reselect(self) -> str | None:
        """Re-run the choice. A pinned node always wins."""
        if self._config.outbound_propagation_node:
            self._apply_pinned()
            return self._selected
        best = next(iter(self.known_nodes()), None)
        if best is None or best["hash"] == self._selected:
            return self._selected
        self._select(best["hash"])
        return self._selected

    def prune(self) -> None:
        """Drop nodes not heard from within the TTL."""
        now = time.time()
        with self._lock:
            stale = [n for n, (_, heard) in self._nodes.items()
                     if now - heard >= self._ttl]
            for node in stale:
                del self._nodes[node]
        if self._selected in stale:
            self._selected = None
            self.reselect()

    # --- private ---

    def _apply_pinned(self) -> None:
        pinned = self._config.outbound_propagation_node
        if pinned and pinned != self._selected:
            self._select(pinned)

    def _select(self, node_hex: str) -> None:
        try:
            self._router.set_outbound_propagation_node(bytes.fromhex(node_hex))
        except (ValueError, AttributeError) as e:
            RNS.log(f"TrenchChat [propagation]: could not select node "
                    f"{node_hex[:16]}…: {e}", RNS.LOG_WARNING)
            return
        self._selected = node_hex
        RNS.log(f"TrenchChat [propagation]: sending offline messages through "
                f"{node_hex[:16]}…", RNS.LOG_NOTICE)
        for cb in self._callbacks:
            try:
                cb(node_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [propagation]: callback error: {e}",
                        RNS.LOG_ERROR)
