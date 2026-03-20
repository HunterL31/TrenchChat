"""
Invite-only channel membership management.

Member list document v2 (msgpack):
{
    "channel_hash":  bytes,        # 16 bytes
    "version":       int,
    "published_at":  float,
    "members":       [bytes, ...], # all member identity hashes
    "admins":        [bytes, ...], # subset of members
    "owners":        [bytes],      # exactly one — the channel creator
    "permissions":   bytes,        # msgpack-encoded permissions dict
    "signatures":    {bytes: bytes} # admin/owner hash -> Ed25519 signature
}

v2 signed payload = msgpack of:
    [channel_hash, version, published_at, sorted(members), sorted(admins),
     sorted(owners), permissions_blob]

v1 signed payload (legacy, no "owners" key in doc) = msgpack of:
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
from trenchchat.core.permissions import (
    ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
    permissions_from_json, permissions_to_json,
)
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE,
    F_INVITE_TOKEN, F_INVITEE_HASH, F_EXPIRY_TS, F_ADMIN_HASH,
    F_MEMBER_LIST_DOC, F_CHANNEL_NAME, F_CHANNEL_DESC,
    F_CHANNEL_CREATOR, F_CHANNEL_ACCESS, F_CHANNEL_CREATED_AT,
    F_CHANNEL_PERMISSIONS,
    MT_JOIN_REQUEST, MT_MEMBER_LIST_UPDATE, MT_INVITE,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

DEFAULT_TOKEN_TTL = 7 * 24 * 3600  # 7 days


def _signed_payload(channel_hash: bytes, version: int, published_at: float,
                    members: list[bytes], admins: list[bytes],
                    owners: list[bytes] | None = None,
                    permissions_blob: bytes = b"") -> bytes:
    """Build the payload that gets signed.

    If *owners* is provided the v2 format is used (includes owners and
    permissions_blob).  Otherwise the v1 format is used for backward compat.
    """
    items: list = [channel_hash, version, published_at,
                   sorted(members), sorted(admins)]
    if owners is not None:
        items.extend([sorted(owners), permissions_blob])
    return msgpack.packb(items, use_bin_type=True)


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
        self._invite_callbacks: list = []
        self._channel_callbacks: list = []
        self._member_list_callbacks: list = []
        router.add_delivery_callback(self._on_lxmf_message)

    def add_invite_callback(self, callback):
        """callback(channel_hash_hex, channel_name, token, expiry, admin_hash_hex)"""
        if callback not in self._invite_callbacks:
            self._invite_callbacks.append(callback)

    def remove_invite_callback(self, callback):
        if callback in self._invite_callbacks:
            self._invite_callbacks.remove(callback)

    def add_channel_joined_callback(self, callback):
        """callback(channel_hash_hex, channel_name) — fired when auto-joined via invite."""
        if callback not in self._channel_callbacks:
            self._channel_callbacks.append(callback)

    def remove_channel_joined_callback(self, callback):
        if callback in self._channel_callbacks:
            self._channel_callbacks.remove(callback)

    def add_member_list_callback(self, callback):
        """callback(channel_hash_hex) — fired whenever a member list update is accepted."""
        if callback not in self._member_list_callbacks:
            self._member_list_callbacks.append(callback)

    def remove_member_list_callback(self, callback):
        if callback in self._member_list_callbacks:
            self._member_list_callbacks.remove(callback)

    # --- member list document ---

    def _build_document(self, channel_hash_hex: str,
                        members: list[bytes], admins: list[bytes],
                        version: int, published_at: float,
                        owners: list[bytes] | None = None,
                        permissions: dict | None = None) -> dict:
        if owners is None:
            owners = []
        permissions_blob = (msgpack.packb(permissions, use_bin_type=True)
                            if permissions else b"")
        payload = _signed_payload(
            bytes.fromhex(channel_hash_hex), version, published_at,
            members, admins, owners, permissions_blob,
        )
        sig = _sign(self._identity.rns_identity, payload)
        return {
            "channel_hash": bytes.fromhex(channel_hash_hex),
            "version":      version,
            "published_at": published_at,
            "members":      members,
            "admins":       admins,
            "owners":       owners,
            "permissions":  permissions_blob,
            "signatures":   {self._identity.hash: sig},
        }

    def _validate_document(self, doc: dict, channel_hash_hex: str) -> bool:
        """Return True if the document has at least one valid admin/owner signature."""
        admins_in_doc: list[bytes] = doc.get("admins", [])
        owners_in_doc: list[bytes] = doc.get("owners", [])
        sigs: dict = doc.get("signatures", {})
        signers = set(admins_in_doc) | set(owners_in_doc)

        is_v2 = "owners" in doc
        if is_v2:
            payload = _signed_payload(
                doc["channel_hash"], doc["version"], doc["published_at"],
                doc["members"], admins_in_doc,
                owners_in_doc, doc.get("permissions", b""),
            )
        else:
            payload = _signed_payload(
                doc["channel_hash"], doc["version"], doc["published_at"],
                doc["members"], admins_in_doc,
            )

        for signer_hash_bytes, sig in sigs.items():
            delivery_hash = RNS.Destination.hash(signer_hash_bytes, "lxmf", "delivery")
            signer_identity = RNS.Identity.recall(delivery_hash)
            if signer_identity is None:
                continue
            if signer_hash_bytes in signers:
                if _verify(signer_identity, payload, sig):
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

        # Rebuild members table with role-based membership
        owners_set = set(doc.get("owners", []))
        admins_set = set(doc.get("admins", []))
        member_rows: list[tuple[str, str, str]] = []
        for m in doc["members"]:
            if m in owners_set:
                role = ROLE_OWNER
            elif m in admins_set:
                role = ROLE_ADMIN
            else:
                role = ROLE_MEMBER
            member_rows.append((m.hex(), "", role))
        self._storage.replace_members(channel_hash_hex, member_rows)

        # Apply permissions from the document if present
        perms_blob = doc.get("permissions", b"")
        if perms_blob:
            try:
                perms = msgpack.unpackb(perms_blob, raw=False)
                if isinstance(perms, dict):
                    self._storage.set_channel_permissions(channel_hash_hex, perms)
            except Exception:
                pass

        for cb in self._member_list_callbacks:
            try:
                cb(channel_hash_hex)
            except Exception as e:
                RNS.log(f"TrenchChat: member list callback error: {e}", RNS.LOG_ERROR)

        return True

    # --- publish a new member list (admin action) ---

    def publish_member_list(self, channel_hash_hex: str,
                            add_members: list[bytes] | None = None,
                            remove_members: list[bytes] | None = None,
                            add_admins: list[bytes] | None = None,
                            remove_admins: list[bytes] | None = None,
                            add_owners: list[bytes] | None = None,
                            remove_owners: list[bytes] | None = None):
        """Build, sign, persist, and broadcast an updated member list."""
        existing = self._storage.get_member_list_version(channel_hash_hex)
        if existing:
            old_doc = msgpack.unpackb(existing["document_blob"], raw=True)
            members = list(old_doc[b"members"])
            admins  = list(old_doc[b"admins"])
            owners  = list(old_doc.get(b"owners", []))
            version = existing["version"] + 1
        else:
            members = [self._identity.hash]
            admins  = [self._identity.hash]
            owners  = [self._identity.hash]
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
        for o in (add_owners or []):
            if o not in owners:
                owners.append(o)
        for o in (remove_owners or []):
            if o in owners:
                owners.remove(o)

        channel = self._storage.get_channel(channel_hash_hex)
        perms = (permissions_from_json(channel["permissions"])
                 if channel and channel["permissions"] else None)

        published_at = time.time()
        doc = self._build_document(channel_hash_hex, members, admins,
                                   version, published_at,
                                   owners=owners, permissions=perms)
        self._accept_document(doc, channel_hash_hex)
        self._broadcast_member_list(channel_hash_hex, doc)

    def _broadcast_member_list(self, channel_hash_hex: str, doc: dict):
        blob = msgpack.packb(doc, use_bin_type=True)
        channel = self._storage.get_channel(channel_hash_hex)
        fields = {
            F_MSG_TYPE:        MT_MEMBER_LIST_UPDATE,
            F_CHANNEL_HASH:    bytes.fromhex(channel_hash_hex),
            F_MEMBER_LIST_DOC: blob,
        }
        if channel:
            fields[F_CHANNEL_NAME]        = channel["name"]
            fields[F_CHANNEL_DESC]        = channel["description"] or ""
            fields[F_CHANNEL_CREATOR]     = channel["creator_hash"]
            fields[F_CHANNEL_PERMISSIONS] = channel["permissions"]
            fields[F_CHANNEL_CREATED_AT]  = channel["created_at"]
        for row in self._storage.get_members(channel_hash_hex):
            dest_hex = row["identity_hash"]
            if dest_hex == self._identity.hash_hex:
                continue
            self._send_raw(dest_hex, fields)

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
        RNS.log(f"TrenchChat: sending invite for channel {channel_hash_hex[:12]}… "
                f"to {invitee_hash_hex[:12]}…", RNS.LOG_NOTICE)
        invitee_hash = bytes.fromhex(invitee_hash_hex)
        token, expiry = self.generate_invite_token(channel_hash_hex, invitee_hash, ttl)
        self._send_raw(invitee_hash_hex, {
            F_MSG_TYPE:     MT_INVITE,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_INVITE_TOKEN: token,
            F_INVITEE_HASH: invitee_hash,
            F_EXPIRY_TS:    expiry,
            F_ADMIN_HASH:   self._identity.hash,
        })

    def send_join_request(self, channel_hash_hex: str, token: bytes,
                          expiry: float, admin_hash_hex: str):
        """Send a join request to an admin using a received invite token."""
        RNS.log(f"TrenchChat: sending join request for channel {channel_hash_hex[:12]}… "
                f"to admin {admin_hash_hex[:12]}…", RNS.LOG_NOTICE)
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
        admin_delivery_hash = RNS.Destination.hash(admin_hash, "lxmf", "delivery")
        admin_identity = RNS.Identity.recall(admin_delivery_hash)
        if admin_identity is None:
            RNS.log(f"TrenchChat [invite]: cannot verify token — admin identity "
                    f"{admin_hash.hex()[:12]}… not known", RNS.LOG_WARNING)
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

        RNS.log(f"TrenchChat [invite]: received control message type={msg_type!r}",
                RNS.LOG_DEBUG)

        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            RNS.log("TrenchChat [invite]: control message missing channel hash, dropping",
                    RNS.LOG_WARNING)
            return
        channel_hash_hex = channel_hash_bytes.hex() \
            if isinstance(channel_hash_bytes, bytes) else str(channel_hash_bytes)

        if msg_type == MT_JOIN_REQUEST:
            RNS.log(f"TrenchChat [invite]: join request received for channel {channel_hash_hex[:12]}…",
                    RNS.LOG_NOTICE)
            self._handle_join_request(fields, channel_hash_hex)

        elif msg_type == MT_MEMBER_LIST_UPDATE:
            blob = fields.get(F_MEMBER_LIST_DOC)
            if blob:
                try:
                    doc = msgpack.unpackb(blob, raw=True)
                    doc_clean = {
                        "channel_hash": doc[b"channel_hash"],
                        "version":      doc[b"version"],
                        "published_at": doc[b"published_at"],
                        "members":      list(doc[b"members"]),
                        "admins":       list(doc[b"admins"]),
                        "signatures":   dict(doc[b"signatures"]),
                    }
                    if b"owners" in doc:
                        doc_clean["owners"] = list(doc[b"owners"])
                    if b"permissions" in doc:
                        doc_clean["permissions"] = doc[b"permissions"]
                    accepted = self._accept_document(doc_clean, channel_hash_hex)
                    RNS.log(f"TrenchChat [invite]: member list update v{doc_clean['version']} "
                            f"for {channel_hash_hex[:12]}… — {'accepted' if accepted else 'rejected'}",
                            RNS.LOG_NOTICE)

                    # If channel metadata was included and we don't know this channel yet,
                    # upsert it and subscribe so it appears in the sidebar.
                    if accepted:
                        channel_name = fields.get(F_CHANNEL_NAME)
                        if channel_name and not self._storage.get_channel(channel_hash_hex):
                            if isinstance(channel_name, bytes):
                                channel_name = channel_name.decode("utf-8", errors="replace")
                            desc = fields.get(F_CHANNEL_DESC, b"")
                            if isinstance(desc, bytes):
                                desc = desc.decode("utf-8", errors="replace")
                            creator = fields.get(F_CHANNEL_CREATOR, b"")
                            if isinstance(creator, bytes):
                                creator = creator.decode("utf-8", errors="replace")
                            perms_field = fields.get(F_CHANNEL_PERMISSIONS)
                            if perms_field is None:
                                perms_field = fields.get(F_CHANNEL_ACCESS, b"invite")
                            if isinstance(perms_field, bytes):
                                perms_field = perms_field.decode("utf-8", errors="replace")
                            created_at = fields.get(F_CHANNEL_CREATED_AT, time.time())
                            self._storage.upsert_channel(
                                hash=channel_hash_hex,
                                name=channel_name,
                                description=desc,
                                creator_hash=creator,
                                permissions=perms_field,
                                created_at=created_at,
                            )
                            self._storage.subscribe(channel_hash_hex)
                            RNS.log(f"TrenchChat [invite]: auto-joined channel "
                                    f"{channel_name!r} ({channel_hash_hex[:12]}…)",
                                    RNS.LOG_NOTICE)
                            for cb in self._channel_callbacks:
                                try:
                                    cb(channel_hash_hex, channel_name)
                                except Exception as e:
                                    RNS.log(f"TrenchChat: channel callback error: {e}",
                                            RNS.LOG_ERROR)
                except Exception as e:
                    RNS.log(f"TrenchChat: member list update parse error: {e}",
                            RNS.LOG_WARNING)

        elif msg_type == MT_INVITE:
            token        = fields.get(F_INVITE_TOKEN)
            expiry       = fields.get(F_EXPIRY_TS)
            admin_hash   = fields.get(F_ADMIN_HASH)
            RNS.log(f"TrenchChat [invite]: invite received for channel {channel_hash_hex[:12]}… "
                    f"token={'present' if token else 'MISSING'} "
                    f"expiry={'present' if expiry else 'MISSING'} "
                    f"admin={'present' if admin_hash else 'MISSING'}",
                    RNS.LOG_NOTICE)
            if token and expiry and admin_hash:
                admin_hex = admin_hash.hex() if isinstance(admin_hash, bytes) else str(admin_hash)
                channel = self._storage.get_channel(channel_hash_hex)
                channel_name = channel["name"] if channel else channel_hash_hex[:12]
                for cb in self._invite_callbacks:
                    try:
                        cb(channel_hash_hex, channel_name, token, expiry, admin_hex)
                    except Exception as e:
                        RNS.log(f"TrenchChat: invite callback error: {e}", RNS.LOG_ERROR)
            else:
                RNS.log("TrenchChat [invite]: invite message missing required fields, dropping",
                        RNS.LOG_WARNING)

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
        msg_type = fields.get(F_MSG_TYPE, "unknown")
        try:
            identity_hash = bytes.fromhex(dest_hex)

            # Compute the LXMF delivery destination hash from the identity hash.
            # RNS.Identity.recall() takes a *destination* hash, not an identity hash.
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")

            dest_identity = RNS.Identity.recall(delivery_dest_hash)

            if dest_identity is None:
                RNS.Transport.request_path(delivery_dest_hash)
                RNS.log(f"TrenchChat [invite]: cannot deliver {msg_type!r} to "
                        f"{dest_hex[:12]}… — identity not known, path requested",
                        RNS.LOG_WARNING)
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
            RNS.log(f"TrenchChat [invite]: queuing {msg_type!r} → {dest_hex[:12]}…",
                    RNS.LOG_NOTICE)
            self._router.send(lxm)
        except Exception as e:
            RNS.log(f"TrenchChat: invite send error ({msg_type}): {e}", RNS.LOG_WARNING)
