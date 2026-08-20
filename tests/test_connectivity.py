"""
Noticing our own link coming back.

Every other catch-up path in the app is triggered by hearing from a remote
peer, so the node that was itself away has nothing to wake it. These cover the
signal that fills that gap.
"""

import RNS

from trenchchat.core.connectivity import (
    MIN_RESYNC_INTERVAL_SECS, OFFLINE_SAMPLES_BEFORE_OUTAGE,
    LinkWatcher, any_interface_online,
)


class FakeInterface:
    def __init__(self, online=True, detached=False, local=False):
        self.online = online
        self.detached = detached
        self.local = local


def _patch_interfaces(monkeypatch, interfaces):
    monkeypatch.setattr(RNS.Transport, "interfaces", interfaces)
    monkeypatch.setattr(RNS.Transport, "is_local_client_interface",
                        staticmethod(lambda i: getattr(i, "local", False)))


class TestOnlineDetection:
    def test_only_a_usable_interface_counts(self, monkeypatch):
        _patch_interfaces(monkeypatch, [FakeInterface(online=True)])
        assert any_interface_online()

        _patch_interfaces(monkeypatch, [FakeInterface(online=False)])
        assert not any_interface_online()

        _patch_interfaces(monkeypatch, [FakeInterface(online=True, detached=True)])
        assert not any_interface_online()

    def test_a_shared_instance_client_is_not_connectivity(self, monkeypatch):
        """The local RNS daemon being up says nothing about reaching the mesh.

        A shared-instance client interface is online whenever rnsd is running,
        so counting it would report connectivity for a node with no radio and
        no network at all.
        """
        _patch_interfaces(monkeypatch, [FakeInterface(online=True, local=True)])
        assert not any_interface_online()

    def test_a_broken_interface_object_does_not_crash_the_check(self, monkeypatch):
        class Exploding:
            @property
            def online(self):
                raise RuntimeError("interface is being torn down")

        _patch_interfaces(monkeypatch, [Exploding(), FakeInterface(online=True)])
        assert any_interface_online()


def _go_offline(watcher, iface, at: float = 0.0) -> None:
    """Take the link down for long enough to count as an outage."""
    iface.online = False
    for i in range(OFFLINE_SAMPLES_BEFORE_OUTAGE):
        watcher.poll(now=at + i)


class TestLinkWatcher:
    def test_fires_when_connectivity_returns(self, monkeypatch):
        fired = []
        iface = FakeInterface(online=True)
        _patch_interfaces(monkeypatch, [iface])
        watcher = LinkWatcher(lambda: fired.append(1))

        watcher.poll(now=0.0)
        assert fired == [], "the first sample is a baseline, not a reconnect"

        _go_offline(watcher, iface, at=1.0)
        assert fired == []

        iface.online = True
        watcher.poll(now=10.0)
        assert fired == [1], "a link coming back did not trigger a resync"

    def test_a_brief_flap_is_not_an_outage(self, monkeypatch):
        """interface.online for a TCP client follows the socket to whichever
        hub it is attached to, so a hub restart must not make every client
        attached to it fan out a full resync."""
        fired = []
        iface = FakeInterface(online=True)
        _patch_interfaces(monkeypatch, [iface])
        watcher = LinkWatcher(lambda: fired.append(1))
        watcher.poll(now=0.0)

        for i in range(10):
            iface.online = (i % 2 == 0)
            watcher.poll(now=float(i + 1))

        assert fired == [], "a flapping link drove a resync"

    def test_resyncs_are_spaced_out(self, monkeypatch):
        fired = []
        iface = FakeInterface(online=True)
        _patch_interfaces(monkeypatch, [iface])
        watcher = LinkWatcher(lambda: fired.append(1))
        watcher.poll(now=0.0)

        _go_offline(watcher, iface, at=1.0)
        iface.online = True
        watcher.poll(now=10.0)
        assert fired == [1]

        _go_offline(watcher, iface, at=20.0)
        iface.online = True
        watcher.poll(now=30.0)
        assert fired == [1], "a second resync ran inside the minimum interval"

        _go_offline(watcher, iface, at=40.0)
        iface.online = True
        watcher.poll(now=10.0 + MIN_RESYNC_INTERVAL_SECS + 1)
        assert fired == [1, 1], "no resync ran after the interval elapsed"

    def test_does_not_fire_while_connectivity_is_merely_steady(self, monkeypatch):
        fired = []
        _patch_interfaces(monkeypatch, [FakeInterface(online=True)])
        watcher = LinkWatcher(lambda: fired.append(1))

        for i in range(5):
            watcher.poll(now=float(i))
        assert fired == [], "a steady link asked its peers for history repeatedly"

    def test_startup_on_a_dead_link_still_fires_when_it_comes_up(self, monkeypatch):
        """Launching with no connectivity, then getting some, is a reconnect.

        The startup sync pass runs three seconds in and finds nobody. Without
        this the app would never ask again.
        """
        fired = []
        iface = FakeInterface(online=False)
        _patch_interfaces(monkeypatch, [iface])
        watcher = LinkWatcher(lambda: fired.append(1))

        for i in range(OFFLINE_SAMPLES_BEFORE_OUTAGE):
            watcher.poll(now=float(i))
        iface.online = True
        watcher.poll(now=10.0)
        assert fired == [1]

    def test_a_failing_callback_does_not_kill_the_watcher(self, monkeypatch):
        calls = []

        def explode():
            calls.append(1)
            raise RuntimeError("sync blew up")

        iface = FakeInterface(online=True)
        _patch_interfaces(monkeypatch, [iface])
        watcher = LinkWatcher(explode, min_resync_interval=0.0)
        watcher.poll(now=0.0)
        for round_ in range(2):
            base = 100.0 * (round_ + 1)
            _go_offline(watcher, iface, at=base)
            iface.online = True
            watcher.poll(now=base + 50)
        assert len(calls) == 2, "the watcher stopped after its callback raised"
