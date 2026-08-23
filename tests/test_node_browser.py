"""
NodeBrowserManager: URL parsing, announce ingestion, the fetch state
machine against a fake transport, caches, bookmarks, and the hosted pages
directory scan (the serve-side traversal boundary).
"""

import os

import pytest

from trenchchat.config import Config
from trenchchat.core.node_browser import (
    DEFAULT_INDEX_MU, NodeBrowserManager, parse_nomad_url,
)
from trenchchat.core.storage import Storage

from tests.fake_node import FakeNodeRegistry, FakeNodeTransport
from tests.helpers import wait_for

NODE_A = "aa" * 16
NODE_B = "bb" * 16


@pytest.fixture
def registry():
    return FakeNodeRegistry()


@pytest.fixture
def manager(tmp_path, registry):
    config = Config(data_dir=tmp_path)
    storage = Storage(db_path=tmp_path / "storage.db")
    transport = FakeNodeTransport("11" * 16, registry, node_hex=NODE_A)
    mgr = NodeBrowserManager(None, storage, config, transport=transport)
    yield mgr
    transport.join_threads()
    storage.close()


def _events_of(manager):
    events = []
    manager.add_fetch_callback(
        lambda fid, node, path, status, progress, reason:
        events.append((fid, node, path, status, reason)))
    return events


# ---------------------------------------------------------------------------
# parse_nomad_url
# ---------------------------------------------------------------------------

def test_parse_full_url():
    assert parse_nomad_url(f"{NODE_A}:/page/x.mu") == (NODE_A, "/page/x.mu")


def test_parse_relative_url():
    assert parse_nomad_url(":/page/x.mu") == (None, "/page/x.mu")
    assert parse_nomad_url("/page/x.mu") == (None, "/page/x.mu")


def test_parse_bare_hash_means_index():
    assert parse_nomad_url(NODE_A) == (NODE_A, "/page/index.mu")
    assert parse_nomad_url(f"{NODE_A}:") == (NODE_A, "/page/index.mu")


def test_parse_uppercase_hash_normalised():
    assert parse_nomad_url(NODE_A.upper()) == (NODE_A, "/page/index.mu")


@pytest.mark.parametrize("url", [
    "",
    "nothex:/page/x.mu",
    "abcd:/page/x.mu",
    f"{NODE_A}:/etc/passwd",
    f"{NODE_A}:/page/../x.mu",
    ":/nope",
    "https://example.com/",
])
def test_parse_rejects_malformed(url):
    with pytest.raises(ValueError):
        parse_nomad_url(url)


# ---------------------------------------------------------------------------
# Announce ingestion
# ---------------------------------------------------------------------------

def test_record_node_announce_persists_and_notifies(manager):
    seen = []
    manager.add_node_callback(lambda node, name: seen.append((node, name)))
    manager.record_node_announce(NODE_A, "Node A")
    assert seen == [(NODE_A, "Node A")]
    rows = manager.known_nodes()
    assert [r["node_hash"] for r in rows] == [NODE_A]
    assert rows[0]["display_name"] == "Node A"


def test_record_node_announce_refreshes_name_keeps_first_seen(manager):
    manager.record_node_announce(NODE_A, "Old name")
    first_seen = manager.known_nodes()[0]["first_seen"]
    manager.record_node_announce(NODE_A, "New name")
    row = manager.known_nodes()[0]
    assert row["display_name"] == "New name"
    assert row["first_seen"] == first_seen


def test_record_node_announce_ignores_bad_hash(manager):
    manager.record_node_announce("not-hex", "Evil")
    manager.record_node_announce("ab", "Evil")
    assert manager.known_nodes() == []


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _host(registry, node_hex, providers):
    host = FakeNodeTransport("99" * 16, registry, node_hex=node_hex)
    host.start_hosting("Host", providers)
    return host


def test_fetch_page_done_caches_and_notifies(manager, registry):
    _host(registry, NODE_B, {"/page/index.mu": lambda: b"# hello"})
    events = _events_of(manager)
    fetch_id = manager.fetch_page(NODE_B, "/page/index.mu")
    assert (fetch_id, NODE_B, "/page/index.mu", "queued", None) in events
    assert wait_for(lambda: any(e[3] == "done" for e in events))
    row = manager.get_cached_page(NODE_B, "/page/index.mu")
    assert bytes(row["content"]) == b"# hello"


def test_fetch_failure_surfaces_reason(manager, registry):
    events = _events_of(manager)
    manager.fetch_page(NODE_B, "/page/index.mu")   # nobody hosts NODE_B
    assert wait_for(lambda: any(e[3] == "failed" for e in events))
    failed = next(e for e in events if e[3] == "failed")
    assert failed[4] == "timeout"
    assert manager.get_cached_page(NODE_B, "/page/index.mu") is None


def test_fetch_file_uses_file_cache(manager, registry):
    _host(registry, NODE_B, {"/file/data.bin": lambda: b"\x00\x01"})
    events = _events_of(manager)
    manager.fetch_file(NODE_B, "/file/data.bin")
    assert wait_for(lambda: any(e[3] == "done" for e in events))
    assert bytes(manager.get_cached_file(NODE_B, "/file/data.bin")["content"]) \
        == b"\x00\x01"
    assert manager.get_cached_page(NODE_B, "/file/data.bin") is None


def test_fetch_dedups_in_flight(manager, registry):
    _host(registry, NODE_B, {"/page/index.mu": lambda: b"x"})
    first = manager.fetch_page(NODE_B, "/page/index.mu")
    second = manager.fetch_page(NODE_B, "/page/index.mu")
    assert first == second


def test_fetch_validates_input(manager):
    with pytest.raises(ValueError):
        manager.fetch_page("nothex", "/page/index.mu")
    with pytest.raises(ValueError):
        manager.fetch_page(NODE_B, "/page/../etc")
    with pytest.raises(ValueError):
        manager.fetch_page(NODE_B, "/file/data.bin")   # wrong kind
    with pytest.raises(ValueError):
        manager.fetch_file(NODE_B, "/page/index.mu")


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

def test_bookmark_roundtrip(manager):
    manager.add_bookmark(NODE_A, "/page/index.mu", "Home")
    marks = manager.bookmarks()
    assert [(m["node_hash"], m["path"], m["label"]) for m in marks] == [
        (NODE_A, "/page/index.mu", "Home")]
    assert manager.remove_bookmark(NODE_A, "/page/index.mu") is True
    assert manager.bookmarks() == []
    assert manager.remove_bookmark(NODE_A, "/page/index.mu") is False


def test_bookmark_validates_input(manager):
    with pytest.raises(ValueError):
        manager.add_bookmark("nothex", "/page/index.mu", "x")
    with pytest.raises(ValueError):
        manager.add_bookmark(NODE_A, "/etc/passwd", "x")


# ---------------------------------------------------------------------------
# Hosting
# ---------------------------------------------------------------------------

def test_enable_hosting_creates_default_index(manager):
    status = manager.set_hosting(enabled=True, node_name="My Node")
    assert status["enabled"] is True
    assert status["node_name"] == "My Node"
    index = manager.pages_root / "pages" / "index.mu"
    assert index.read_text(encoding="utf-8") == DEFAULT_INDEX_MU
    assert {p["path"] for p in status["pages"]} == {"/page/index.mu"}
    transport = manager._transport
    assert transport.hosting_name == "My Node"
    assert transport.providers["/page/index.mu"]() == \
        DEFAULT_INDEX_MU.encode("utf-8")


def test_enable_hosting_defaults_node_name(manager):
    status = manager.set_hosting(enabled=True)
    assert status["node_name"].endswith("node")


def test_disable_hosting_stops_transport(manager):
    manager.set_hosting(enabled=True, node_name="My Node")
    status = manager.set_hosting(enabled=False)
    assert status["enabled"] is False
    assert manager._transport.hosting_name is None


def test_hosting_scan_skips_dotfiles(manager):
    manager.set_hosting(enabled=True, node_name="n")
    (manager.pages_root / "pages" / ".secret.mu").write_text("hidden")
    status = manager.refresh_hosted_pages()
    assert {p["path"] for p in status["pages"]} == {"/page/index.mu"}


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_hosting_scan_skips_symlink_escape(manager, tmp_path):
    manager.set_hosting(enabled=True, node_name="n")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (manager.pages_root / "pages" / "leak.mu").symlink_to(outside)
    status = manager.refresh_hosted_pages()
    assert {p["path"] for p in status["pages"]} == {"/page/index.mu"}


def test_hosting_refresh_picks_up_new_pages(manager):
    manager.set_hosting(enabled=True, node_name="n")
    (manager.pages_root / "pages" / "about.mu").write_text(">About")
    (manager.pages_root / "files" / "data.bin").write_bytes(b"\x01")
    status = manager.refresh_hosted_pages()
    assert {p["path"] for p in status["pages"]} == \
        {"/page/index.mu", "/page/about.mu"}
    assert {f["path"] for f in status["files"]} == {"/file/data.bin"}
    assert manager._transport.providers["/file/data.bin"]() == b"\x01"


def test_hosting_restored_from_config(tmp_path, registry):
    config = Config(data_dir=tmp_path)
    storage = Storage(db_path=tmp_path / "storage.db")
    transport = FakeNodeTransport("22" * 16, registry)
    mgr = NodeBrowserManager(None, storage, config, transport=transport)
    mgr.set_hosting(enabled=True, node_name="Persistent")
    storage.close()

    config2 = Config(data_dir=tmp_path)
    storage2 = Storage(db_path=tmp_path / "storage.db")
    transport2 = FakeNodeTransport("33" * 16, registry)
    NodeBrowserManager(None, storage2, config2, transport=transport2)
    assert transport2.hosting_name == "Persistent"
    assert "/page/index.mu" in transport2.providers
    storage2.close()


# ---------------------------------------------------------------------------
# Cache pruning (storage level)
# ---------------------------------------------------------------------------

def test_prune_nomad_pages_keeps_most_recent(manager):
    storage = manager._storage
    for i in range(5):
        storage.put_nomad_page(NODE_A, f"/page/{i}.mu", b"x")
    storage.prune_nomad_pages(2)
    remaining = [storage.get_nomad_page(NODE_A, f"/page/{i}.mu")
                 for i in range(5)]
    assert sum(1 for r in remaining if r is not None) == 2
    assert remaining[4] is not None


def test_prune_nomad_files_respects_byte_budget(manager):
    storage = manager._storage
    for i in range(4):
        storage.put_nomad_file(NODE_A, f"/file/{i}", b"x" * 100)
    storage.prune_nomad_files(250)
    remaining = [storage.get_nomad_file(NODE_A, f"/file/{i}")
                 for i in range(4)]
    assert sum(1 for r in remaining if r is not None) == 2
    assert remaining[3] is not None


# ---------------------------------------------------------------------------
# Friend -> node mapping
# ---------------------------------------------------------------------------

def test_nomad_node_hash_for_identity_matches_rns_derivation():
    import RNS

    from trenchchat.core.node_browser import nomad_node_hash_for_identity

    identity_hex = "12" * 16
    expected = RNS.Destination.hash(
        bytes.fromhex(identity_hex), "nomadnetwork", "node").hex()
    assert nomad_node_hash_for_identity(identity_hex) == expected


def test_node_for_identity_finds_announced_node(manager):
    from trenchchat.core.node_browser import nomad_node_hash_for_identity

    identity_hex = "34" * 16
    node_hex = nomad_node_hash_for_identity(identity_hex)
    manager.record_node_announce(node_hex, "Friend's node")
    row = manager.node_for_identity(identity_hex)
    assert row is not None
    assert row["display_name"] == "Friend's node"


def test_node_for_identity_none_when_never_heard(manager):
    assert manager.node_for_identity("56" * 16) is None
    assert manager.node_for_identity("not-hex") is None
