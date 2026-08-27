"""
Family nomad -- Nomad Network page browsing and hosting.

One tester enables hosting (which announces a nomadnetwork.node destination
and serves its nomad_pages directory), another discovers it from the
announce and fetches pages and files over a real RNS Link via
Link.request. This is the layer pytest's FakeNodeTransport cannot touch:
real announce propagation, path resolution, link establishment, and the
request/response transfer itself.

Testers run on this machine, so scenarios write extra pages straight into
the hosting tester's data/testerX/nomad_pages directory and use
POST /nomad/hosting/refresh to pick them up.

Manual interop check (not automated): run pip `nomadnet` against a testenv
TCP interface, browse its index from TrenchChat's NET tab, and browse
TrenchChat's hosted index from nomadnet's browser.

See docs/testenv-scenarios.md for the matrix these implement.
"""

import time
from pathlib import Path

from asserts import settle, wait_until, ScenarioFailure
from scenario import PROBE, scenario

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# Enabling hosting announces once; if that single announce is missed there
# is nothing to re-trigger it for NODE_ANNOUNCE_INTERVAL_SECS, so discovery
# waits are chunked with a re-enable (which re-announces) between chunks.
_DISCOVER_CHUNK_SECS = 30.0
_DISCOVER_ATTEMPTS = 3
_FETCH_TIMEOUT = 120.0


def _pages_dir(peer) -> Path:
    return _DATA_DIR / f"tester{peer.tag}" / "nomad_pages"


def _host_and_discover(host, browser, node_name: str) -> str:
    """Enable hosting on host, wait until browser has discovered the node.

    Returns the node destination hash browser should dial.
    """
    for _ in range(_DISCOVER_ATTEMPTS):
        host.nomad_set_hosting(enabled=True, node_name=node_name)
        found, _ = settle(lambda: len(browser.nomad_nodes()) > 0,
                          f"{browser.tag} to hear {host.tag}'s node announce",
                          _DISCOVER_CHUNK_SECS)
        if found:
            break
    nodes = browser.nomad_nodes()
    if not nodes:
        raise ScenarioFailure(
            f"{browser.tag} never discovered {host.tag}'s node announce")
    named = [n for n in nodes if n["display_name"] == node_name]
    if not named:
        raise ScenarioFailure(
            f"{browser.tag} discovered nodes {nodes} but none named {node_name!r}")
    return named[0]["node_hash"]


def _fetch_page(browser, node_hash: str, path: str,
                timeout: float = _FETCH_TIMEOUT) -> tuple[str, float]:
    """Browse to a page and wait for its cached copy. Returns (source, secs)."""
    result = browser.nomad_browse(f"{node_hash}:{path}")
    if not result.get("ok"):
        raise ScenarioFailure(f"browse refused: {result}")
    elapsed = wait_until(lambda: browser.nomad_page(node_hash, path) is not None,
                         f"{browser.tag} to fetch {path}", timeout)
    return browser.nomad_page(node_hash, path), elapsed


@scenario("nomad1", "A hosted node is discovered and its index fetched",
          peers="AB")
def n1(env):
    a, b = env.peers("A", "B")
    node_hash = _host_and_discover(a, b, "n1 node")

    source, elapsed = _fetch_page(b, node_hash, "/page/index.mu")
    if "TrenchChat Node" not in source:
        raise ScenarioFailure(
            f"fetched index does not look like the default page: {source[:120]!r}")
    return {"node_hash": node_hash[:12], "fetch_secs": round(elapsed, 1),
            "page_bytes": len(source)}


@scenario("nomad2", "Added pages and files serve after a rescan", peers="AB")
def n2(env):
    a, b = env.peers("A", "B")
    node_hash = _host_and_discover(a, b, "n2 node")

    pages = _pages_dir(a)
    (pages / "pages").mkdir(parents=True, exist_ok=True)
    (pages / "files").mkdir(parents=True, exist_ok=True)
    (pages / "pages" / "about.mu").write_text(
        ">About\n`!bold`! and a `[link`:/page/index.mu]\n", encoding="utf-8")
    payload = bytes(range(256)) * 64
    (pages / "files" / "blob.bin").write_bytes(payload)

    status = a.nomad_refresh_hosting()
    served = {p["path"] for p in status["pages"]}
    if "/page/about.mu" not in served:
        raise ScenarioFailure(f"rescan did not pick up about.mu: {served}")

    source, page_secs = _fetch_page(b, node_hash, "/page/about.mu")
    if ">About" not in source:
        raise ScenarioFailure(f"about.mu content wrong: {source[:120]!r}")

    fetch = b.nomad_fetch(node_hash, "/file/blob.bin")
    if not fetch.get("ok"):
        raise ScenarioFailure(f"file fetch refused: {fetch}")
    file_secs = wait_until(
        lambda: b.nomad_file(node_hash, "/file/blob.bin") is not None,
        "B to fetch blob.bin", _FETCH_TIMEOUT)
    fetched = b.nomad_file(node_hash, "/file/blob.bin")
    if fetched != payload:
        raise ScenarioFailure(
            f"file round-trip corrupted: {len(fetched)} bytes back, "
            f"{len(payload)} sent")
    return {"page_secs": round(page_secs, 1), "file_secs": round(file_secs, 1),
            "file_bytes": len(payload)}


@scenario("nomad3", "A fetch from an offline node fails, then succeeds on return",
          peers="AB", kind=PROBE)
def n3(env):
    """Predicts the dial ladder's give-up surfaces as a bounded failure while
    the host is offline, and that a plain retry works once it returns --
    nothing covers redial-after-return timing yet."""
    a, b = env.peers("A", "B")
    node_hash = _host_and_discover(a, b, "n3 node")

    # Prime one fetch so the link machinery has worked once, then drop A.
    _fetch_page(b, node_hash, "/page/index.mu")
    a.go_offline()
    time.sleep(2.0)

    started = time.time()
    result = b.nomad_browse(f"{node_hash}:/page/never-seen.mu")
    notes = {"browse_ok": result.get("ok", False)}
    # The page can never arrive; what is being measured is that the fetch
    # neither hangs forever nor poisons the machinery for the retry below.
    settled, waited = settle(
        lambda: b.nomad_page(node_hash, "/page/never-seen.mu") is not None,
        "a page from an offline node (expected never)", 90.0)
    notes["offline_fetch_arrived"] = settled
    notes["offline_wait_secs"] = round(time.time() - started, 1)
    if settled:
        notes["surprise"] = "a page arrived from an offline node"

    a.go_online()
    # index.mu is already cached from the priming fetch, so the retry must
    # target a page that can only arrive over a fresh link.
    (_pages_dir(a) / "pages" / "back.mu").write_text(">Back online\n",
                                                     encoding="utf-8")
    try:
        a.nomad_refresh_hosting()
        _, retry_secs = _fetch_page(b, node_hash, "/page/back.mu",
                                    timeout=180.0)
        notes["retry_after_return_secs"] = round(retry_secs, 1)
    except Exception as e:
        notes["surprise"] = f"retry after host returned failed: {e}"
    return notes
