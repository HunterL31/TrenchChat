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

from nomad_demo import demo_pages, seed_demo_node  # noqa: E402

IDENTITY = "ab" * 16


def test_demo_pages_name_their_tester():
    pages = demo_pages("Tester B", IDENTITY)
    assert set(pages) == {"index.mu", "about.mu", "art.mu"}
    for content in pages.values():
        assert "Tester B" in content
    assert IDENTITY in pages["index.mu"]
    assert IDENTITY in pages["about.mu"]


@pytest.fixture
def backend(tmp_path):
    config = Config(data_dir=tmp_path)
    storage = Storage(db_path=tmp_path / "storage.db")
    transport = FakeNodeTransport("11" * 16, FakeNodeRegistry())
    browser = NodeBrowserManager(None, storage, config, transport=transport)
    yield SimpleNamespace(
        node_browser=browser,
        identity=SimpleNamespace(hash_hex=IDENTITY),
    )
    storage.close()


def test_seed_enables_hosting_with_named_pages(backend):
    seed_demo_node(backend, "Tester A")
    transport = backend.node_browser._transport
    assert transport.hosting_name == "Tester A's demo node"
    assert set(transport.providers) >= {
        "/page/index.mu", "/page/about.mu", "/page/art.mu"}
    assert b"Tester A" in transport.providers["/page/about.mu"]()


def test_seed_survives_a_second_run(backend):
    seed_demo_node(backend, "Tester A")
    seed_demo_node(backend, "Tester A")
    assert backend.node_browser._transport.hosting_name == \
        "Tester A's demo node"
