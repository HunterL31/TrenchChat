"""The dev-environment nomad demo seed: pages name their tester, and
seeding enables hosting on the tester's node browser."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from trenchchat.config import Config
from trenchchat.core.node_browser import NodeBrowserManager
from trenchchat.core.storage import Storage

from tests.fake_node import FakeNodeRegistry, FakeNodeTransport

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

from nomad_demo import demo_files, demo_pages, seed_demo_node  # noqa: E402

IDENTITY = "ab" * 16


def test_demo_pages_name_their_tester():
    pages = demo_pages("Tester B", IDENTITY)
    assert set(pages) == {"index.mu", "about.mu", "art.mu", "fields.mu",
                          "echo.mu", "table.mu", "live.mu", "side.mu"}
    assert IDENTITY in pages["index.mu"]
    assert IDENTITY in pages["about.mu"]
    for name in ("index.mu", "about.mu", "art.mu", "fields.mu"):
        assert "Tester B" in pages[name]


def test_every_demo_page_is_reachable_from_the_index():
    """A page nothing points at cannot be exercised from the UI. Reachable
    means a link or a partial names it, at any depth from the index."""
    pages = demo_pages("Tester B", IDENTITY)
    reached = {"index.mu"}
    frontier = ["index.mu"]
    while frontier:
        source = pages[frontier.pop()]
        for name in pages:
            if f"/page/{name}" in source and name not in reached:
                reached.add(name)
                frontier.append(name)
    assert reached == set(pages)


@pytest.fixture
def backend(tmp_path):
    config = Config(data_dir=tmp_path)
    storage = Storage(db_path=tmp_path / "storage.db")
    transport = FakeNodeTransport("11" * 16, FakeNodeRegistry())
    identity = SimpleNamespace(hash_hex=IDENTITY)
    browser = NodeBrowserManager(identity, storage, config,
                                 transport=transport)
    yield SimpleNamespace(
        node_browser=browser,
        identity=identity,
    )
    storage.close()


def test_seed_enables_hosting_with_named_pages(backend):
    seed_demo_node(backend, "Tester A")
    transport = backend.node_browser._transport
    assert transport.hosting_name == "Tester A's demo node"
    assert set(transport.providers) >= {
        "/page/index.mu", "/page/about.mu", "/page/art.mu", "/page/fields.mu",
        "/page/table.mu", "/page/live.mu", "/page/side.mu",
        "/file/notes.txt"}
    assert b"Tester A" in transport.providers["/page/about.mu"]()
    # A file provider yields its Path, not its bytes: that is what lets the
    # transport answer with the handle-plus-name shape nomadnet can save.
    served = transport.providers["/file/notes.txt"]()
    assert served.read_bytes() == demo_files()["notes.txt"].encode()


def test_seed_survives_a_second_run(backend):
    seed_demo_node(backend, "Tester A")
    seed_demo_node(backend, "Tester A")
    assert backend.node_browser._transport.hosting_name == \
        "Tester A's demo node"
