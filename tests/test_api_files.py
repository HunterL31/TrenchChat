"""
The shared-file surface of the HTTP/WS API: share, fetch, bytes, events.

Two real peers from peer_factory sit behind two apps, so a share really is
stored, the manifest really crosses the transport, and the second peer's
download really runs against the first one's serve callback. What is under
test is the endpoint contract the Flutter client will be written against: the
"file" object on a message row, the fetch snapshot, the file_fetch events, and
the bytes coming back byte for byte under the download headers.
"""

import base64
import random
import sys
import time
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trenchchat.core.permissions import (
    PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE, SHARE_FILES,
    VOICE_CHAT,
)
from trenchchat.core.protocol import FILE_CHUNK_BYTES, MAX_SHARED_FILE_BYTES

from tests.helpers import wait_for, wait_for_member

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

try:
    with warnings.catch_warnings():
        # Same httpx fallback warning test_api_security.py suppresses.
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from api import TOKEN_HEADER, create_app
    _HAVE_BACKEND_DEPS = True
except ImportError:  # pragma: no cover - depends on the local install
    _HAVE_BACKEND_DEPS = False
    TOKEN_HEADER = "x-tc-token"

needs_backend = pytest.mark.skipif(
    not _HAVE_BACKEND_DEPS,
    reason="install devtools/testenv/requirements.txt to exercise the API",
)

TOKEN = "test-token-not-a-real-one"
AUTH = {TOKEN_HEADER: TOKEN}
WS_HOST = {"Host": "127.0.0.1:8801"}

FILE_NAME = "notes.bin"
OTHER_CHANNEL = "dd" * 16


def blob(size: int = 150_000, seed: int = 7) -> bytes:
    return random.Random(seed).randbytes(size)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _api_backend(peer):
    """A Backend stand-in carrying one real peer's managers.

    Everything the file endpoints touch is the peer's own object, so a call
    through the API runs the same code a real client would drive; the rest of
    create_app's wiring is satisfied by the mock.
    """
    backend = MagicMock()
    backend.config = peer.config
    backend.identity = peer.identity
    backend.storage = peer.storage
    backend.messaging = peer.messaging
    backend.subscription_mgr = peer.subscription_mgr
    backend.presence_mgr = peer.presence_mgr
    backend.file_mgr = peer.file_mgr
    backend.invite_mgr.list_pending_invites.return_value = []
    backend.invite_mgr.list_pending_memberships.return_value = []
    return backend


@pytest.fixture
def client_factory():
    """make(peer) -> TestClient serving that peer, closed at teardown."""
    clients: list = []

    def make(peer):
        client = TestClient(create_app(_api_backend(peer), token=TOKEN),
                            base_url="http://127.0.0.1:8801")
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def channel(peer_factory):
    """(owner, member, channel_hash) on an invite-only channel both hold.

    Each peer sees the other as online, so a download exercises holder choice
    rather than presence.
    """
    owner = peer_factory("alice")
    member = peer_factory("bob")
    perms = dict(PRESET_PRIVATE)
    ch_hash = owner.channel_mgr.create_channel("files-ch", "", permissions=perms)
    owner.invite_mgr.publish_member_list(ch_hash,
                                         add_members=[member.identity.hash])
    assert wait_for_member(owner.storage, ch_hash, member.identity.hash_hex)

    member.storage.upsert_channel(ch_hash, "files-ch", "",
                                  owner.identity.hash_hex, perms, time.time())
    # The document the owner published lands on the member's own thread and
    # rewrites the channel's permissions when it does. Everything below has to
    # come after it, or a test that narrows the member's permissions has them
    # restored a fraction of a second later and passes on the timing.
    assert wait_for(
        lambda: member.storage.get_member_list_version(ch_hash) is not None,
    ), "the member never applied the owner's member list"
    member.storage.subscribe(ch_hash)
    member.storage.set_channel_permissions(ch_hash, perms)
    for peer, role in ((owner, ROLE_OWNER), (member, ROLE_MEMBER)):
        member.storage.upsert_member(ch_hash, peer.identity.hash_hex,
                                     peer.name.capitalize(), role=role)
    for peer, other in ((owner, member), (member, owner)):
        peer.presence_mgr.record_seen(other.identity.hash_hex)
    return owner, member, ch_hash


def share(client, ch_hash: str, data: bytes, name: str = FILE_NAME,
          content: str = "here"):
    return client.post(f"/channels/{ch_hash}/messages", headers=AUTH,
                       json={"content": content, "file_name": name,
                             "file_data_b64": b64(data)})


def rows(client, ch_hash: str) -> list[dict]:
    return client.get(f"/channels/{ch_hash}/messages", headers=AUTH).json()


def file_row(client, ch_hash: str, file_hash: str) -> dict | None:
    for row in rows(client, ch_hash):
        if row["file"] and row["file"]["hash"] == file_hash:
            return row
    return None


def wait_for_row(client, ch_hash: str, file_hash: str,
                 timeout: float = 10.0) -> dict:
    found: dict = {}

    def arrived() -> bool:
        row = file_row(client, ch_hash, file_hash)
        if row is not None:
            found["row"] = row
        return row is not None

    assert wait_for(arrived, timeout=timeout), "the file message never arrived"
    return found["row"]


@needs_backend
class TestSharingThroughTheMessagesEndpoint:
    def test_a_share_answers_with_the_message_and_the_file(self, channel,
                                                           client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)
        data = blob()

        res = share(client, ch_hash, data)

        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["message_id"]
        assert owner.storage.get_file(body["file_hash"])["complete"]

    def test_the_sender_row_carries_the_file_as_held(self, channel,
                                                     client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)
        data = blob()

        file_hash = share(client, ch_hash, data).json()["file_hash"]

        row = file_row(client, ch_hash, file_hash)
        assert row["file"] == {
            "name": FILE_NAME, "size": len(data), "hash": file_hash,
            "state": "done", "progress": 1.0, "reason": None,
        }
        assert row["file_stripped"] is False
        assert row["has_image"] is False

    def test_a_message_with_no_file_carries_none(self, channel, client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)

        client.post(f"/channels/{ch_hash}/messages", headers=AUTH,
                    json={"content": "plain"})

        (row,) = rows(client, ch_hash)
        assert row["file"] is None
        assert row["file_stripped"] is False

    def test_an_image_and_a_file_together_are_refused(self, channel,
                                                      client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)

        res = client.post(f"/channels/{ch_hash}/messages", headers=AUTH,
                          json={"content": "x", "image_data_b64": b64(b"jpeg"),
                                "file_name": FILE_NAME,
                                "file_data_b64": b64(b"bytes")})

        assert res.status_code == 400
        assert res.json()["reason"] == "file_and_image"
        assert rows(client, ch_hash) == []

    def test_a_name_with_no_bytes_is_refused(self, channel, client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)

        res = client.post(f"/channels/{ch_hash}/messages", headers=AUTH,
                          json={"content": "x", "file_name": FILE_NAME})

        assert res.status_code == 400
        assert res.json()["reason"] == "empty_file"

    def test_bytes_with_no_name_are_refused(self, channel, client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)

        res = client.post(f"/channels/{ch_hash}/messages", headers=AUTH,
                          json={"content": "x", "file_data_b64": b64(b"data")})

        assert res.status_code == 400
        assert res.json()["reason"] == "incomplete_file"

    def test_malformed_base64_is_a_bad_request(self, channel, client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)

        res = client.post(f"/channels/{ch_hash}/messages", headers=AUTH,
                          json={"content": "x", "file_name": FILE_NAME,
                                "file_data_b64": "!!!not base64!!!"})

        assert res.status_code == 400
        assert res.json()["reason"] == "bad_file_base64"

    def test_a_file_over_the_ceiling_is_refused_before_it_is_stored(
            self, channel, client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)
        oversized = b"\x00" * (MAX_SHARED_FILE_BYTES + 1)

        res = share(client, ch_hash, oversized)

        assert res.status_code == 400
        assert res.json()["reason"] == "file_too_large"
        assert owner.storage.list_files() == []

    def test_a_body_over_the_request_cap_is_refused(self, channel,
                                                    client_factory):
        from api import MAX_REQUEST_BYTES

        owner, _member, ch_hash = channel
        client = client_factory(owner)

        res = client.post(
            f"/channels/{ch_hash}/messages",
            headers={**AUTH, "Content-Length": str(MAX_REQUEST_BYTES + 1)},
            content=b"{}",
        )

        assert res.status_code == 413

    def test_the_request_cap_leaves_room_for_the_largest_file(self):
        from api import MAX_REQUEST_BYTES

        assert MAX_REQUEST_BYTES > MAX_SHARED_FILE_BYTES * 4 // 3

    def test_a_member_without_share_files_is_refused(self, channel,
                                                     client_factory):
        _owner, member, ch_hash = channel
        text_only = dict(PRESET_PRIVATE)
        text_only[ROLE_MEMBER] = [SEND_MESSAGE, VOICE_CHAT]
        member.storage.set_channel_permissions(ch_hash, text_only)
        client = client_factory(member)

        res = share(client, ch_hash, blob(1000))

        assert res.status_code == 200
        assert res.json() == {"ok": False, "reason": "no_share_permission"}
        assert member.storage.list_files() == []

    def test_a_direct_message_refuses_a_file(self, channel, client_factory):
        owner, member, _ch_hash = channel
        client = client_factory(owner)

        res = client.post(f"/dms/{member.identity.hash_hex}/messages",
                          headers=AUTH,
                          json={"content": "x", "file_name": FILE_NAME,
                                "file_data_b64": b64(b"data")})

        assert res.status_code == 400
        assert res.json()["reason"] == "no_file_in_dm"


@needs_backend
class TestDownloadingOnASecondPeer:
    def test_a_manifest_reads_available_before_anyone_asks(self, channel,
                                                           client_factory):
        owner, member, ch_hash = channel
        owner_client = client_factory(owner)
        member_client = client_factory(member)
        data = blob()

        file_hash = share(owner_client, ch_hash, data).json()["file_hash"]

        row = wait_for_row(member_client, ch_hash, file_hash)
        assert row["file"]["state"] == "available"
        assert row["file"]["progress"] == 0.0
        assert row["file"]["name"] == FILE_NAME
        assert row["file"]["size"] == len(data)
        assert member.storage.get_file(file_hash) is None

    def test_bytes_are_404_until_the_download_finishes(self, channel,
                                                       client_factory):
        owner, _member, ch_hash = channel
        owner_client = client_factory(owner)
        member_client = client_factory(_member)
        file_hash = share(owner_client, ch_hash, blob()).json()["file_hash"]
        wait_for_row(member_client, ch_hash, file_hash)

        res = member_client.get(f"/channels/{ch_hash}/files/{file_hash}",
                                headers=AUTH)

        assert res.status_code == 404
        assert res.json()["reason"] == "unknown"

    def test_fetch_runs_to_done_and_serves_the_bytes(self, channel,
                                                     client_factory):
        owner, member, ch_hash = channel
        owner_client = client_factory(owner)
        member_client = client_factory(member)
        data = blob()
        file_hash = share(owner_client, ch_hash, data).json()["file_hash"]
        row = wait_for_row(member_client, ch_hash, file_hash)

        states = []
        with member_client.websocket_connect(f"/ws?token={TOKEN}",
                                             headers=WS_HOST) as ws:
            started = member_client.post(
                f"/channels/{ch_hash}/files/{file_hash}/fetch", headers=AUTH,
                json={"message_id": row["message_id"]})
            assert started.status_code == 200
            assert started.json()["ok"] is True
            assert started.json()["file_hash"] == file_hash
            events = []
            while not states or states[-1] not in ("done", "failed"):
                event = ws.receive_json()
                if event["type"] == "file_fetch" \
                        and event["file_hash"] == file_hash:
                    events.append(event)
                    states.append(event["state"])

        assert states[-1] == "done"
        assert events[-1]["message_ids"] == [row["message_id"]]
        assert events[-1]["channels"] == [ch_hash]
        assert events[-1]["progress"] == 1.0
        assert events[-1]["reason"] is None

        served = member_client.get(f"/channels/{ch_hash}/files/{file_hash}",
                                   headers=AUTH)
        assert served.status_code == 200
        assert served.content == data
        assert served.headers["x-content-type-options"] == "nosniff"
        assert served.headers["content-type"].startswith(
            "application/octet-stream")
        assert 'filename="notes.bin"' in served.headers["content-disposition"]
        assert "attachment" in served.headers["content-disposition"]

        assert file_row(member_client, ch_hash, file_hash)["file"]["state"] \
            == "done"

    def test_a_second_fetch_joins_the_finished_download(self, channel,
                                                        client_factory):
        owner, member, ch_hash = channel
        owner_client = client_factory(owner)
        member_client = client_factory(member)
        data = blob()
        file_hash = share(owner_client, ch_hash, data).json()["file_hash"]
        row = wait_for_row(member_client, ch_hash, file_hash)
        url = f"/channels/{ch_hash}/files/{file_hash}/fetch"

        member_client.post(url, headers=AUTH,
                           json={"message_id": row["message_id"]})
        assert wait_for(lambda: member_client.get(url, headers=AUTH).json()
                        .get("state") == "done", timeout=20.0)

        again = member_client.post(url, headers=AUTH,
                                   json={"message_id": row["message_id"]})
        assert again.status_code == 200
        assert again.json()["state"] == "done"
        assert member_client.get(
            f"/channels/{ch_hash}/files/{file_hash}",
            headers=AUTH).content == data

    def test_fetch_status_answers_a_client_that_missed_the_events(
            self, channel, client_factory):
        owner, member, ch_hash = channel
        owner_client = client_factory(owner)
        member_client = client_factory(member)
        file_hash = share(owner_client, ch_hash, blob()).json()["file_hash"]
        row = wait_for_row(member_client, ch_hash, file_hash)
        url = f"/channels/{ch_hash}/files/{file_hash}/fetch"

        assert member_client.get(url, headers=AUTH).status_code == 404
        member_client.post(url, headers=AUTH,
                           json={"message_id": row["message_id"]})

        assert wait_for(lambda: member_client.get(url, headers=AUTH).json()
                        .get("state") == "done", timeout=20.0)
        body = member_client.get(url, headers=AUTH).json()
        assert body["name"] == FILE_NAME
        assert body["chunks_held"] == body["chunk_count"]

    def test_fetch_for_an_unknown_message_is_404(self, channel,
                                                 client_factory):
        owner, member, ch_hash = channel
        owner_client = client_factory(owner)
        member_client = client_factory(member)
        file_hash = share(owner_client, ch_hash, blob()).json()["file_hash"]
        wait_for_row(member_client, ch_hash, file_hash)

        res = member_client.post(
            f"/channels/{ch_hash}/files/{file_hash}/fetch", headers=AUTH,
            json={"message_id": "no-such-message"})

        assert res.status_code == 404
        assert res.json()["reason"] == "unknown"

    def test_fetch_naming_a_file_the_message_does_not_carry_is_404(
            self, channel, client_factory):
        owner, member, ch_hash = channel
        owner_client = client_factory(owner)
        member_client = client_factory(member)
        file_hash = share(owner_client, ch_hash, blob()).json()["file_hash"]
        row = wait_for_row(member_client, ch_hash, file_hash)

        res = member_client.post(
            f"/channels/{ch_hash}/files/{'ab' * 32}/fetch", headers=AUTH,
            json={"message_id": row["message_id"]})

        assert res.status_code == 404

    def test_bytes_under_another_channel_are_404(self, channel,
                                                 client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)
        file_hash = share(client, ch_hash, blob()).json()["file_hash"]

        res = client.get(f"/channels/{OTHER_CHANNEL}/files/{file_hash}",
                         headers=AUTH)

        assert res.status_code == 404
        assert client.get(f"/channels/{ch_hash}/files/{file_hash}",
                          headers=AUTH).status_code == 200


@needs_backend
class TestFileEndpointsNeedTheToken:
    def test_the_bytes_endpoint_refuses_an_untokened_request(
            self, channel, client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)
        data = blob()
        file_hash = share(client, ch_hash, data).json()["file_hash"]
        url = f"/channels/{ch_hash}/files/{file_hash}"

        assert client.get(url).status_code == 401
        # A browser navigation carries no headers, so the query parameter is
        # the only way it can present one.
        with_query = client.get(f"{url}?token={TOKEN}")
        assert with_query.status_code == 200
        assert with_query.content == data

    def test_the_fetch_endpoints_need_the_token(self, channel, client_factory):
        owner, _member, ch_hash = channel
        client = client_factory(owner)
        file_hash = share(client, ch_hash, blob()).json()["file_hash"]
        url = f"/channels/{ch_hash}/files/{file_hash}/fetch"

        assert client.get(url).status_code == 401
        assert client.post(url, json={"message_id": "x"}).status_code == 401
        assert client.get("/files/usage").status_code == 401


@needs_backend
class TestStoreUsage:
    def test_usage_reports_what_is_held_against_each_budget(
            self, channel, client_factory):
        from trenchchat.core.storage import (
            FILE_STORE_MAX_BYTES, OWN_FILE_STORE_MAX_BYTES,
            PARTIAL_STORE_MAX_BYTES,
        )

        owner, _member, ch_hash = channel
        client = client_factory(owner)
        data = blob()

        before = client.get("/files/usage", headers=AUTH).json()
        assert before["usage"] == {"own": 0, "received": 0, "partial": 0}
        assert before["limits"] == {
            "own": OWN_FILE_STORE_MAX_BYTES,
            "received": FILE_STORE_MAX_BYTES,
            "partial": PARTIAL_STORE_MAX_BYTES,
        }
        assert before["max_file_bytes"] == MAX_SHARED_FILE_BYTES

        share(client, ch_hash, data)

        after = client.get("/files/usage", headers=AUTH).json()
        assert after["usage"]["own"] == len(data)
        assert after["usage"]["received"] == 0


@needs_backend
class TestChunkedShapeIsUnchangedByTheApi:
    def test_a_file_crosses_as_chunks_not_as_one_blob(self, channel,
                                                      client_factory):
        """The API hands core whole bytes; the store still holds chunks."""
        owner, _member, ch_hash = channel
        client = client_factory(owner)
        data = blob(FILE_CHUNK_BYTES * 2 + 10)

        file_hash = share(client, ch_hash, data).json()["file_hash"]

        assert owner.storage.file_chunk_indices(file_hash) == [0, 1, 2]
