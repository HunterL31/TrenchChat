"""
Noticing that this node's own connection has come back.

Every catch-up mechanism in the app is driven by hearing *from* somebody:
`PeerAnnounceHandler` fires when a remote peer announces, and that is what
asks them for anything missed. Nothing fires when the local node is the one
that was away, so a client that loses signal in a tunnel and comes out the
other side never asks anyone for what it missed -- it just waits, and whether
it catches up depends on whether some peer happens to announce afterwards.

This watcher supplies the missing signal by polling Reticulum's own interface
state, which is the only place the fact is recorded. Shared-instance client
interfaces are ignored: they are up whenever the local RNS daemon is running
and say nothing about whether this node can reach the mesh.
"""

import threading

import RNS

CHECK_INTERVAL_SECS = 5.0


def _usable(interface) -> bool:
    try:
        if getattr(interface, "detached", False) or not getattr(interface, "online", False):
            return False
        return not RNS.Transport.is_local_client_interface(interface)
    except Exception:
        return False


def any_interface_online() -> bool:
    """True if this node currently has a usable way onto the mesh."""
    try:
        return any(_usable(i) for i in RNS.Transport.interfaces)
    except Exception:
        return False


class LinkWatcher:
    """Calls back once each time this node's connectivity is restored."""

    def __init__(self, on_reconnected, interval: float = CHECK_INTERVAL_SECS):
        self._on_reconnected = on_reconnected
        self._interval = interval
        self._online: bool | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="link-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll(self) -> bool:
        """Sample connectivity once, firing the callback on a return to online.

        The first sample only establishes a baseline. Startup already runs its
        own sync pass, and treating "the app just launched" as a reconnect
        would duplicate it.
        """
        now_online = any_interface_online()
        was_online = self._online
        self._online = now_online

        if was_online is None or now_online == was_online:
            return now_online
        if now_online:
            RNS.log("TrenchChat [link]: connectivity restored — resyncing",
                    RNS.LOG_NOTICE)
            try:
                self._on_reconnected()
            except Exception as e:
                RNS.log(f"TrenchChat [link]: resync on reconnect failed: {e}",
                        RNS.LOG_ERROR)
        else:
            RNS.log("TrenchChat [link]: connectivity lost", RNS.LOG_NOTICE)
        return now_online

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.poll()
