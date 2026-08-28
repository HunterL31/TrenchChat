"""
Choosing an LXMF propagation node to hand offline direct messages to.

A channel survives an absent member because any other member can serve them
the gap later. A conversation has nobody else in it, so an offline peer's
messages have to be left somewhere until they collect them: an LXMF
propagation node, which stores the message and hands it over when they ask.

Three things follow from that, and each is a reason this module exists:

  * LXMF refuses to send a propagated message unless an outbound node is
    configured -- it fails the message rather than queueing it -- so one has
    to be chosen before the first offline send, not after.
  * LXMF tracks nodes only for its own propagation-node mode. A client that
    merely *uses* a node has to notice them announcing and pick one itself.
  * A node announces when propagation is switched on, and not again on a
    timer. A client that forgets its node on restart may therefore never hear
    of one again, so the choice is remembered across restarts rather than
    rediscovered.

And held mail is *pulled*: nothing arrives because a node has it. Something
has to ask, which is what PropagationCollector below is for.

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
        self._restore()

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

    def _restore(self) -> None:
        """Start from the user's pin, or from what we last settled on.

        Without the second half, a restart leaves this client with no node
        until one is switched on somewhere -- which is the only time a node
        announces at all.
        """
        self._apply_pinned()
        if self._selected is None and self._config.last_propagation_node:
            self._select(self._config.last_propagation_node)

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
        if self._config.last_propagation_node != node_hex:
            self._config.last_propagation_node = node_hex
        RNS.log(f"TrenchChat [propagation]: sending offline messages through "
                f"{node_hex[:16]}…", RNS.LOG_NOTICE)
        for cb in self._callbacks:
            try:
                cb(node_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [propagation]: callback error: {e}",
                        RNS.LOG_ERROR)


# How often to ask while recently back. A sender's message can still be on its
# way to the node when we ask -- LXMF makes them generate a proof-of-work stamp
# first, which takes seconds -- so one empty answer means nothing.
SETTLING_ASK_INTERVAL_SECS = 15.0

# How long that lasts after coming back or choosing a node. Long enough to
# outlast a stamp and a transfer, short enough not to be a habit.
SETTLING_WINDOW_SECS = 180.0

# How often to ask once settled. A sender whose presence view says we are away
# propagates even while we are up, and no node ever pushes -- so asking has to
# continue, just not at the settling rate: each attempt is a link.
STEADY_ASK_INTERVAL_SECS = 300.0


class PropagationCollector:
    """Asks the selected node for mail held for us.

    Propagated messages are pulled, never pushed, so without this a message
    left with a node stays there. Collected messages arrive through the
    ordinary delivery callback and are authenticated like any other.

    Driven by a frontend timer (tick), by a node being selected, and by the
    link coming back -- the same three moments a channel would resync at. The
    last two open a settling window, because arriving back is exactly when a
    message is most likely to be mid-flight to the node rather than already
    sitting on it: asking once on return and then not again for five minutes
    loses precisely the message that was being sent as we returned.
    """

    def __init__(self, router, identity, nodes,
                 settling_interval_secs: float = SETTLING_ASK_INTERVAL_SECS,
                 settling_window_secs: float = SETTLING_WINDOW_SECS,
                 steady_interval_secs: float = STEADY_ASK_INTERVAL_SECS) -> None:
        self._router = router
        self._identity = identity
        self._nodes = nodes
        self._settling_interval = settling_interval_secs
        self._settling_window = settling_window_secs
        self._steady_interval = steady_interval_secs
        self._last_ask = 0.0
        # Opened on first use rather than here, so the window is measured from
        # when this actually starts running.
        self._settling_until: float | None = None

    def collect_now(self, now: float | None = None) -> bool:
        """Ask immediately, and keep asking often for a while.

        False when no node is selected, or the ask could not be started.
        """
        now = time.time() if now is None else now
        self._settling_until = now + self._settling_window
        return self._ask(now)

    def tick(self, now: float | None = None) -> bool:
        """Ask if enough time has passed. Returns True if an ask was made."""
        now = time.time() if now is None else now
        if self._settling_until is None:
            # A process that has just started is the "recently back" case:
            # mail may have been left for it while it was gone, and no node
            # ever says so. Waiting a full steady interval before looking
            # would strand exactly that message.
            self._settling_until = now + self._settling_window
        if self._nodes.selected is None:
            return False
        due = (self._settling_interval if now < self._settling_until
               else self._steady_interval)
        if now - self._last_ask < due:
            return False
        return self._ask(now)

    def _ask(self, now: float) -> bool:
        self._last_ask = now
        if self._nodes.selected is None:
            self._nodes.reselect()
        started = self._router.request_propagation_sync(self._identity.rns_identity)
        if started:
            RNS.log("TrenchChat [propagation]: asked the node for held messages",
                    RNS.LOG_DEBUG)
        return started
