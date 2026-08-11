"""
One-shot smoke test proving that two Backend instances, each in its own
OS process with its own standalone Reticulum instance, can actually talk
to each other over a real point-to-point TCP interface -- i.e. that the
full-realism two-tester design in backend_core.py is sound before any web
layer gets built on top of it.

Run directly:
    python devtools/testenv/smoke_test.py

Exits 0 and prints PASS on success, non-zero and FAIL otherwise.
"""

import json
import multiprocessing as mp
import shutil
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTENV_DIR = Path(__file__).resolve().parent
for p in (str(_REPO_ROOT), str(_TESTENV_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

_PORT = 41501
_BASE = _TESTENV_DIR / "smoke_data"


def tester_a(base: Path):
    from backend_core import Backend

    data_dir = base / "A"
    backend = Backend(data_dir, "Tester A (smoke)", role="server",
                      listen_port=_PORT, peer_host="127.0.0.1", peer_port=_PORT,
                      instance_name="trenchchat_smoke_a")
    backend.write_identity_file()
    backend.start_heartbeat()

    from backend_core import wait_for_identity_file
    b_info = wait_for_identity_file(base / "B", timeout=30)
    b_hash = b_info["hash_hex"]

    if not backend.warm_up(b_hash, timeout=25.0, interval=1.0):
        result = {"joined": False, "channel_hash": None,
                  "sent_content": None, "error": "path to B never resolved"}
        (data_dir / "result.json").write_text(json.dumps(result))
        backend.close()
        return

    # A server with two channels: one invite must admit B to both, and the
    # message below goes to the *second* channel to prove it.
    from trenchchat.core import actions
    server_hash = actions.create_server(
        backend.server_mgr, backend.invite_mgr, "smoke-server",
        "cross-process smoke test",
    )
    first = actions.create_channel_in_server(
        backend.storage, backend.channel_mgr, backend.invite_mgr,
        server_hash, backend.identity.hash_hex, "general",
    )
    ch_hash = actions.create_channel_in_server(
        backend.storage, backend.channel_mgr, backend.invite_mgr,
        server_hash, backend.identity.hash_hex, "second",
    )
    backend.invite_mgr.send_invite(server_hash, b_hash)

    deadline = time.time() + 30
    joined = False
    while time.time() < deadline:
        if backend.storage.is_member(server_hash, b_hash):
            joined = True
            break
        time.sleep(0.2)

    sent_content = "hello over real TCP loopback"
    if joined:
        # A's is_member() only proves A's own state. The member-list-update
        # that finalizes B's membership and a chat message sent right after
        # are two independent LXMF sends with no ordering guarantee over a
        # real network -- messaging.py drops inbound chat messages until the
        # receiver's own storage shows is_subscribed. A real human pauses
        # here naturally before typing; give B's async processing the same
        # courtesy instead of racing it.
        time.sleep(3.0)
        recipients = [row["identity_hash"] for row in backend.storage.get_members(ch_hash)]
        backend.messaging.send_message(
            channel_hash_hex=ch_hash, content=sent_content, subscriber_hashes=recipients
        )

    result = {"joined": joined, "channel_hash": ch_hash,
              "server_hash": server_hash, "first_channel": first,
              "sent_content": sent_content}
    (data_dir / "result.json").write_text(json.dumps(result))
    time.sleep(3)  # let delivery threads finish before the process exits
    backend.close()


def tester_b(base: Path):
    from backend_core import Backend

    data_dir = base / "B"
    backend = Backend(data_dir, "Tester B (smoke)", role="client",
                      listen_port=_PORT, peer_host="127.0.0.1", peer_port=_PORT,
                      instance_name="trenchchat_smoke_b")
    backend.write_identity_file()
    backend.start_heartbeat()

    # Backend itself doesn't auto-accept (a real GUI user clicks "Accept"
    # explicitly -- see api.py's /invites/{hash}/accept). This scripted
    # test has no human, so it plays that role itself, via the same
    # accept_invite() a UI click would call.
    def on_invite(channel_hash_hex, channel_name, token, expiry, admin_hex):
        backend.accept_invite(channel_hash_hex, token, expiry, admin_hex)

    backend.invite_mgr.add_invite_callback(on_invite)

    received = {"content": None, "message_id": None}

    def on_message(channel_hash_hex, message_id):
        for m in backend.storage.get_messages(channel_hash_hex):
            if m["message_id"] == message_id:
                received["content"] = m["content"]
                received["message_id"] = message_id

    backend.messaging.add_message_callback(on_message)

    deadline = time.time() + 45
    while time.time() < deadline and received["content"] is None:
        time.sleep(0.2)

    # One invite should have admitted B to every channel in the server.
    servers = [dict(s) for s in backend.storage.get_all_servers()]
    received["server_count"] = len(servers)
    received["channel_count"] = sum(
        len(backend.storage.get_server_channels(s["hash"])) for s in servers
    )

    (data_dir / "result.json").write_text(json.dumps(received))
    backend.close()


def main() -> int:
    if _BASE.exists():
        shutil.rmtree(_BASE)
    (_BASE / "A").mkdir(parents=True)
    (_BASE / "B").mkdir(parents=True)

    ctx = mp.get_context("spawn")
    pa = ctx.Process(target=tester_a, args=(_BASE,), name="tester-a")
    pb = ctx.Process(target=tester_b, args=(_BASE,), name="tester-b")

    # Give the server-role process a head start so the TCPServerInterface
    # is listening before the client dials it.
    pa.start()
    time.sleep(1.5)
    pb.start()

    pa.join(timeout=90)
    pb.join(timeout=90)

    if pa.is_alive() or pb.is_alive():
        print("SMOKE TEST: FAIL (process hung, killing)")
        pa.terminate()
        pb.terminate()
        return 1

    try:
        a_result = json.loads((_BASE / "A" / "result.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        a_result = None
    try:
        b_result = json.loads((_BASE / "B" / "result.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        b_result = None

    print("A result:", a_result)
    print("B result:", b_result)

    ok = bool(
        a_result and b_result
        and a_result.get("joined")
        and b_result.get("content") == a_result.get("sent_content")
        # One invite, both channels: B must hold the server and its full roster.
        and b_result.get("server_count") == 1
        and b_result.get("channel_count") == 2
    )
    print("SMOKE TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
