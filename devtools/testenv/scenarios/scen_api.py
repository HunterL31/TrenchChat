"""
Family api -- the API surface itself.

Every other family drives the backend through its API and takes the API's own
access control on trust. This one tests that control directly, because it is
the only thing standing between a tester's identity and any process -- or any
web page -- that can reach the port.

The access-control scenarios send no message and touch no mesh, so they run in
seconds. api5 is the exception: the map's change event is announce-driven, and
only a real peer coming up produces one.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import ScenarioFailure, wait_until
from scenario import PROBE, scenario

# An origin no backend in this environment has any reason to allow.
FOREIGN_ORIGIN = "http://evil.example"

# Routes worth proving individually: one read of the identity, one read of
# history, and one mutation. A gate that covered only some of them would be
# worse than none, because the surface would look protected.
GUARDED_GETS = ["/me", "/channels", "/friends", "/directory"]


@scenario("api1", "An unauthenticated caller is refused on every route", peers="A")
def i1(env):
    """No token means no API, including the event socket.

    Before the August audit this surface had no authentication and wildcard
    CORS, so any local process -- and any page the user's browser happened to
    load -- could read the whole transcript and post as them.
    """
    a, = env.peers("A")

    refused = {path: a.raw_get(path, token="") for path in GUARDED_GETS}
    allowed = [path for path, status in refused.items() if status != 401]
    if allowed:
        raise ScenarioFailure(f"routes answered without a token: {allowed} ({refused})")

    post_status = a.raw_post("/me/display_name", {"display_name": "intruder"}, token="")
    if post_status != 401:
        raise ScenarioFailure(f"a mutation without a token answered {post_status}")

    socket = a.ws_probe(token="")
    if socket == "open":
        raise ScenarioFailure("the event socket streamed to an unauthenticated client")

    if a.me()["hash_hex"] != a.hash:
        raise ScenarioFailure("the display name was changed by a refused caller")

    return {"http_refusals": sorted(set(refused.values())), "socket": socket}


@scenario("api2", "A wrong token is refused and a right one works three ways", peers="A")
def i2(env):
    """Header, bearer and query parameter all authenticate the same token.

    Three routes exist because a browser can set headers on neither a
    WebSocket handshake nor an <img> src, so the query parameter is not
    redundant -- it is the only one those two can use.
    """
    a, = env.peers("A")

    wrong = a.raw_get("/me", token="not-the-token")
    if wrong != 401:
        raise ScenarioFailure(f"a wrong token answered {wrong}, not 401")

    header = a.raw_get("/me", token=None)
    bearer = a.raw_get("/me", token="",
                       headers={"Authorization": f"Bearer {a.token}"})
    query = a.raw_get("/me", token="", params={"token": a.token})

    bad = {name: status for name, status in
           {"header": header, "bearer": bearer, "query": query}.items()
           if status != 200}
    if bad:
        raise ScenarioFailure(f"a valid token was refused: {bad}")

    socket_header = a.ws_probe(token=None)
    socket_query = a.ws_probe(token=None, query_token=True)
    if socket_header != "open" or socket_query != "open":
        raise ScenarioFailure(
            f"a valid token did not open the socket: header={socket_header}, "
            f"query={socket_query}"
        )

    return {"socket_header": socket_header, "socket_query": socket_query}


@scenario("api3", "A foreign browser origin cannot open the event socket", peers="A")
def i3(env):
    """The socket checks Origin itself, because nothing else does.

    A browser applies neither CORS nor same-origin policy to a WebSocket
    handshake. Without this check a page could hold a valid token -- one
    leaked through the ?token= URL in history or a referrer -- and stream
    every inbound message live.
    """
    a, = env.peers("A")

    foreign = a.ws_probe(token=None, origin=FOREIGN_ORIGIN)
    if foreign == "open":
        raise ScenarioFailure(
            f"the socket accepted a handshake from {FOREIGN_ORIGIN}"
        )

    same_origin = a.ws_probe(token=None, origin=a.base_url)
    if same_origin != "open":
        raise ScenarioFailure(
            f"the socket refused its own origin: {same_origin}"
        )

    return {"foreign_origin": foreign, "same_origin": same_origin}


@scenario("api4", "One token opens every tester in the environment", kind=PROBE)
def i4(env):
    """Recording a property of the dev environment, not asserting one.

    orchestrator.py mints a single token and hands it to every worker, so any
    tester's token is expected to open any other's API. That is a reasonable
    choice for a dev box, but it means the environment has no per-identity
    isolation, and the orchestrator's own /config -- which is unauthenticated
    -- hands the token to anyone who can reach port 8800. Recorded so the
    harness never claims isolation it does not have.
    """
    a, b, c, d = env.peers("A", "B", "C", "D")

    cross = {p.tag: b.raw_get("/me", token=p.token) for p in (a, c, d)}
    shared = all(status == 200 for status in cross.values())

    config_token = env.orch.api_token()
    orchestrator_leaks = bool(config_token) and config_token == a.token

    return {
        "b_accepts_other_testers_tokens": cross,
        "one_shared_token": shared,
        "orchestrator_config_serves_it_unauthenticated": orchestrator_leaks,
        "surprise": ("" if shared else
                     "tokens are per-tester after all -- the harness could "
                     "assert isolation between them"),
    }


@scenario("api5", "A peer announcing wakes the map over the event socket", peers="AB")
def i5(env):
    """The map is pushed, not polled.

    Nothing about a topology change reaches a client on its own: the path
    table moves on a background RNS thread, and before this the client could
    only find out by asking again on a timer. The backend debounces those
    bursts into one event, so what is under test is that the event arrives at
    all and that the map behind it has actually caught up -- B present, with
    its identity resolved rather than a bare destination hash.
    """
    a, b = env.peers("A", "B")

    with a.events() as stream:
        b.set_display_name("Map Mover")
        elapsed = wait_until(
            lambda: stream.count("network_map_changed") >= 1,
            "A to be told the network map changed", timeout=45.0,
        )
        events = stream.count("network_map_changed")

    node = None

    def resolved() -> bool:
        nonlocal node
        node = a.map_node_for(b.hash)
        return node is not None

    map_elapsed = wait_until(resolved, "A's map to hold B's identity",
                             timeout=45.0)

    if node["kind"] not in ("peer", "transport"):
        raise ScenarioFailure(f"B appears on A's map as {node['kind']!r}")

    # A peer's delivery announce and its trenchchat.user announce are two
    # packets, so the map can hold B a moment before it knows B is one of ours.
    flag_elapsed = wait_until(
        lambda: (a.map_node_for(b.hash) or {}).get("trenchchat"),
        "B to be flagged as a TrenchChat client on A's map", timeout=45.0,
    )
    node = a.map_node_for(b.hash)

    return {
        "event_after_secs": round(elapsed, 1),
        "events_seen": events,
        "map_resolved_after_secs": round(map_elapsed, 1),
        "trenchchat_flag_after_secs": round(flag_elapsed, 1),
        "b_label": node["label"],
        "b_hops": node["hops"],
        "b_online": node["online"],
        "b_interface": node["interface"],
    }
