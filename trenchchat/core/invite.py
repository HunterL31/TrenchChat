"""
Invite-only channel membership management.

Member list document (msgpack):
{
    "channel_hash":  bytes,        # 16 bytes
    "version":       int,
    "published_at":  float,
    "members":       [bytes, ...], # identity hashes (16 bytes each)
    "admins":        [bytes, ...],
    "signatures":    {bytes: bytes} # admin_hash -> Ed25519 signature
}

Signed payload = msgpack of:
    [channel_hash, version, published_at, sorted(members), sorted(admins)]

Invite token = Ed25519 signature over:
    invitee_identity_hash (16 bytes)
    + channel_hash (16 bytes)
    + expiry_timestamp (8 bytes, big-endian double)
"""

import struct
import time
import RNS
import LXMF
import msgpack

from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

# LXMF field keys
F_MSG_TYPE           = 0x10
F_INVITE_TOKEN       = 0x11
F_INVITEE_HASH       = 0x12
F_EXPIRY_TS          = 0x13
F_ADMIN_HASH         = 0x14
F_CHANNEL_HASH       = 0x01
F_MEMBER_LIST_DOC    = 0x21

MT_JOIN_REQUEST      = "join_request"
MT_MEMBER_LIST_UPDATE = "member_list_update"

DEFAULT_TOKEN_TTL = 7 * 24 * 3600  # 7 days


def _signed_payload(channel_hash: bytes, version: int, published_at: float,
                    members: list[bytes], admins: list[bytes]) -> bytes:
    return msgpack.packb(
        [channel_hash, version, published_at,
         sorted(members), sorted(admins)],
        use_bin_type=True,
    )


def _sign(identity: RNS.Identity, data: bytes) -> bytes:
    return identity.sign(data)


def _verify(identity: RNS.Identity, data: bytes, signature: bytes) -> bool:
    try:
        return identity.validate(signature, data)
    except Exception:
        return False


class InviteManager:
    def __init__(self, identity: Identity, storage: Storage, router: Router):
        self._identity = identity
        self._storage = storage
        self._router = router
        router.add_delivery_callback(self._on_lxmf_message)

    # --- member list document ---

    def _build_document(self, channel_hash_hex: str,
                        members: list[bytes], admins: list[bytes],
                        version: int, published_at: float) -> dict:
        payload = _signed_payload(
            bytes.fromhex(channel_hash_hex), version, published_at, members, admins
        )
        sig = _sign(self._identity.rns_identity, payload)
        return {
            "channel_hash": bytes.fromhex(channel_hash_hex),
            "version":      version,
            "published_at": published_at,
            "members":      members,
            "admins":       admins,
            "signatures":   {self._identity.hash: sig},
        }

    def _validate_document(self, doc: dict, channel_hash_hex: str) -> bool:
        """Return True if the document has at least one valid admin signature."""
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return False

        admins_in_doc: list[bytes] = doc.get("admins", [])
        sigs: dict = doc.get("signatures", {})

        payload = _signed_payload(
            doc["channel_hash"], doc["version"], doc["published_at"],
            doc["members"], admins_in_doc,
        )

        for admin_hash_bytes, sig in sigs.items():
            admin_identity = RNS.Identity.recall(admin_hash_bytes)
            if admin_identity is None:
                continue
            if admin_hash_bytes in admins_in_doc:
                if _verify(admin_identity, payload, sig):
                    return True
        return False

    def _accept_document(self, doc: dict, channel_hash_hex: str) -> bool:
        """
        Apply acceptance rules. Returns True if accepted.
        Rules (in order):
          1. At least one valid admin signature.
          2. version > local_version  → accept.
          3. version == local_version, higher published_at → accept.
          4. version == local_version, same published_at, lower admin hash → accept.
        """
        if not self._validate_document(doc, channel_hash_hex):
            return False

        existing = self._storage.get_member_list_version(channel_hash_hex)
        new_v = doc["version"]
        new_ts = doc["published_at"]

        if existing is None:
            pass  # no existing — accept
        else:
            old_v = existing["version"]
            old_ts = existing["published_at"]
            if new_v < old_v:
                return False
            if new_v == old_v:
                if new_ts < old_ts:
                    return False
                if new_ts == old_ts:
                    # Tiebreak: lowest signing admin hash wins
                    new_min = min(doc.get("signatures", {}).keys(), default=b"\xff" * 16)
                    old_doc = msgpack.unpackb(existing["document_blob"], raw=True)
                    old_min = min(old_doc.get(b"signatures", {}).keys(),
                                  default=b"\xff" * 16)
                    if new_min >= old_min:
                        return False

        # Persist
        blob = msgpack.packb(doc, use_bin_type=True)
        self._storage.upsert_member_list_version(
            channel_hash_hex, new_v, new_ts, blob
        )

        # Rebuild members table
        member_rows = [
            (m.hex(), "", m in doc.get("admins", []))
            for m in doc["members"]
        ]
        self._storage.replace_members(channel_hash_hex, member_rows)
        return True

    # --- publish a new member list (admin action) ---

    def publish_member_list(self, channel_hash_hex: str,
                            add_members: list[bytes] | None = None,
                            remove_members: list[bytes] | None = None,
                            add_admins: list[bytes] | None = None,
                            remove_admins: list[bytes] | None = None):
        """Build, sign, persist, and broadcast an updated member list."""
        existing = self._storage.get_member_list_version(channel_hash_hex)
        if existing:
            old_doc = msgpack.unpackb(existing["document_blob"], raw=True)
            members = list(old_doc[b"members"])
            admins  = list(old_doc[b"admins"])
            version = existing["version"] + 1
        else:
            members = [self._identity.hash]
            admins  = [self._identity.hash]
            version = 1

        for m in (add_members or []):
            if m not in members:
                members.append(m)
        for m in (remove_members or []):
            if m in members:
                members.remove(m)
        for a in (add_admins or []):
            if a not in admins:
                admins.append(a)
        for a in (remove_admins or []):
            if a in admins:
                admins.remove(a)

        published_at = time.time()
        doc = self._build_document(channel_hash_hex, members, admins,
                                   version, published_at)
        self._accept_document(doc, channel_hash_hex)
        self._broadcast_member_list(channel_hash_hex, doc)

    def _broadcast_member_list(self, channel_hash_hex: str, doc: dict):
        blob = msgpack.packb(doc, use_bin_type=True)
        for row in self._storage.get_members(channel_hash_hex):
            dest_hex = row["identity_hash"]
            if dest_hex == self._identity.hash_hex:
                continue
            self._send_raw(dest_hex, {
                F_MSG_TYPE:        MT_MEMBER_LIST_UPDATE,
                F_CHANNEL_HASH:    bytes.fromhex(channel_hash_hex),
                F_MEMBER_LIST_DOC: blob,
            })

    # --- invite token ---

    def generate_invite_token(self, channel_hash_hex: str,
                               invitee_hash: bytes,
                               ttl: float = DEFAULT_TOKEN_TTL) -> tuple[bytes, float]:
        """Returns (token_bytes, expiry_timestamp)."""
        expiry = time.time() + ttl
        payload = (invitee_hash
                   + bytes.fromhex(channel_hash_hex)
                   + struct.pack(">d", expiry))
        token = _sign(self._identity.rns_identity, payload)
        return token, expiry

    def send_invite(self, channel_hash_hex: str, invitee_hash_hex: str,
                    ttl: float = DEFAULT_TOKEN_TTL):
        """Generate a token and send it to the invitee via LXMF."""
        invitee_hash = bytes.fromhex(invitee_hash_hex)
        token, expiry = self.generate_invite_token(channel_hash_hex, invitee_hash, ttl)
        self._send_raw(invitee_hash_hex, {
            F_MSG_TYPE:     "invite",
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: invitee_hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   self._identity.hash,
        })

    def send_join_request(self, channel_hash_hex: str, token: bytes,
                          expiry: float, admin_hash_hex: str):
        """Send a join request to an admin using a received invite token."""
        self._send_raw(admin_hash_hex, {
            F_MSG_TYPE:     MT_JOIN_REQUEST,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: self._identity.hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   bytes.fromhex(admin_hash_hex),
        })

    def _verify_invite_token(self, token: bytes, invitee_hash: bytes,
                              channel_hash_hex: str, expiry: float,
                              admin_hash: bytes) -> bool:
        if time.time() > expiry:
            return False
        admin_identity = RNS.Identity.recall(admin_hash)
        if admin_identity is None:
            return False
        if not self._storage.is_admin(channel_hash_hex, admin_hash.hex()):
            return False
        payload = (invitee_hash
                   + bytes.fromhex(channel_hash_hex)
                   + struct.pack(">d", expiry))
        return _verify(admin_identity, payload, token)

    # --- inbound handler ---

    def _on_lxmf_message(self, message: LXMF.LXMessage):
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")

        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            return
        channel_hash_hex = channel_hash_bytes.hex() \
            if isinstance(channel_hash_bytes, bytes) else str(channel_hash_bytes)

        if msg_type == MT_JOIN_REQUEST:
            self._handle_join_request(fields, channel_hash_hex)

        elif msg_type == MT_MEMBER_LIST_UPDATE:
            blob = fields.get(F_MEMBER_LIST_DOC)
            if blob:
                try:
                    doc = msgpack.unpackb(blob, raw=True)
                    # Convert bytes keys to bytes values for consistency
                    doc_clean = {
                        "channel_hash": doc[b"channel_hash"],
                        "version":      doc[b"version"],
                        "published_at": doc[b"published_at"],
                        "members":      list(doc[b"members"]),
                        "admins":       list(doc[b"admins"]),
                        "signatures":   dict(doc[b"signatures"]),
                    }
                    self._accept_document(doc_clean, channel_hash_hex)
                except Exception as e:
                    RNS.log(f"TrenchChat: member list update parse error: {e}",
                            RNS.LOG_WARNING)

        elif msg_type == "invite":
            # Store the received invite for the user to act on via the UI.
            # The UI calls send_join_request() when the user accepts.
            pass

    def _handle_join_request(self, fields: dict, channel_hash_hex: str):
        token        = fields.get(F_INVITE_TOKEN)
        invitee_hash = fields.get(F_INVITEE_HASH)
        expiry       = fields.get(F_EXPIRY_TS)
        admin_hash   = fields.get(F_ADMIN_HASH)

        if not all([token, invitee_hash, expiry, admin_hash]):
            return

        if not self._storage.is_admin(channel_hash_hex, self._identity.hash_hex):
            return

        if not self._verify_invite_token(token, invitee_hash, channel_hash_hex,
                                         expiry, admin_hash):
            RNS.log("TrenchChat: invalid or expired invite token rejected",
                    RNS.LOG_WARNING)
            return

        self.publish_member_list(channel_hash_hex,
                                 add_members=[invitee_hash])

    # --- helpers ---

    def _send_raw(self, dest_hex: str, fields: dict):
        try:
            dest_hash = bytes.fromhex(dest_hex)
            dest_identity = RNS.Identity.recall(dest_hash)
            if dest_identity is None:
                RNS.Transport.request_path(dest_hash)
                return
            dest = RNS.Destination(
                dest_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            lxm = LXMF.LXMessage(
                dest,
                self._router.delivery_destination,
                "",
                desired_method=LXMF.LXMessage.DIRECT,
            )
            lxm.fields = fields
            self._router.send(lxm)
        except Exception as e:
            RNS.log(f"TrenchChat: invite send error: {e}", RNS.LOG_WARNING)
