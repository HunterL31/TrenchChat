"""
Noticing our own link coming back.

Every other catch-up path in the app is triggered by hearing from a remote
peer, so the node that was itself away has nothing to wake it. These cover the
signal that fills that gap.
"""

import RNS

from trenchchat.core.connectivity import LinkWatcher, any_interface_online


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


class TestLinkWatcher:
    def test_fires_when_connectivity_returns(self, monkeypatch):
        fired = []
        iface = FakeInterface(online=True)
        _patch_interfaces(monkeypatch, [iface])
        watcher = LinkWatcher(lambda: fired.append(1))

        watcher.poll()
        assert fired == [], "the first sample is a baseline, not a reconnect"

        iface.online = False
        watcher.poll()
        assert fired == []

        iface.online = True
        watcher.poll()
        assert fired == [1], "a link coming back did not trigger a resync"

    def test_does_not_fire_while_connectivity_is_merely_steady(self, monkeypatch):
        fired = []
        _patch_interfaces(monkeypatch, [FakeInterface(online=True)])
        watcher = LinkWatcher(lambda: fired.append(1))

        for _ in range(5):
            watcher.poll()
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

        watcher.poll()
        iface.online = True
        watcher.poll()
        assert fired == [1]

    def test_a_failing_callback_does_not_kill_the_watcher(self, monkeypatch):
        calls = []

        def explode():
            calls.append(1)
            raise RuntimeError("sync blew up")

        iface = FakeInterface(online=True)
        _patch_interfaces(monkeypatch, [iface])
        watcher = LinkWatcher(explode)

        watcher.poll()
        for _ in range(2):
            iface.online = False
            watcher.poll()
            iface.online = True
            watcher.poll()
        assert len(calls) == 2, "the watcher stopped after its callback raised"
