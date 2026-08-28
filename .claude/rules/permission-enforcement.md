---
description: Three-layer permission enforcement pattern for TrenchChat
globs: "trenchchat/core/**/*.py,devtools/testenv/api.py,tests/test_adversarial.py"
alwaysApply: false
---

# Permission Enforcement

Every permission must be enforced at **three independent layers**. A gap at any
layer allows a bad client (or a bug) to bypass the restriction.

## The three layers

| Layer | Where | What it does |
|---|---|---|
| **Client gate** | Flutter UI: control visibility/enabled state | Hides the control so normal users never see it |
| **Outbound guard** | `actions.py` function / API endpoint | Re-checks permission before calling core; discards disallowed changes even if the UI was somehow triggered |
| **Core inbound enforcement** | Core manager method | Rejects the operation regardless of caller; the only layer that protects against bad clients |

## Mapping of permissions to enforcement points

| Permission | Client gate | Outbound guard | Core enforcement |
|---|---|---|---|
| `SEND_MESSAGE` | compose disabled | `/channels/{hash}/messages` recipient computation | `_on_lxmf_message` drops message |
| `INVITE` | invite control hidden | n/a (token verification is the guard) | `_handle_join_request` checks it; `_verify_invite_token` checks it |
| `KICK` | kick button hidden in members view | `actions.update_membership` re-applies the gate | `publish_member_list` nulls `remove_members` |
| `MANAGE_ROLES` | promote/demote hidden in members view | `actions.update_membership` re-applies the gate | `publish_member_list` nulls `add/remove_admins` |
| `MANAGE_CHANNEL` | permissions editor hidden | `actions.edit_channel_permissions` re-checks it | n/a (member list doc is signature-validated) |

Direct messages are gated by mutual friendship rather than a channel permission, but the same
three-layer shape applies: the client offers a conversation only for an accepted friend, the
`/dms` endpoints refuse a non-friend with 403 (`actions.send_direct_message`), and
`Messaging._on_direct_message` drops anything inbound from a peer this node does not hold —
which is the only layer that holds against a peer calling in directly. See
`docs/direct-messages.md`; the adversarial cases are
`tests/test_adversarial.py::TestDirectMessageGate`.

## Rules

- The client gate is **convenience only** — never rely on it as the sole check.
- The core enforcement layer must work correctly even if called directly
  (e.g. from tests, sync, or a malicious peer).
- For invite-only channels, `SEND_MESSAGE` is only enforced when the sender
  has a known role. Open-join channels have no member table to check against.

```python
# ✅ Core enforcement — publish_member_list
if remove_members and not self._storage.has_permission(
    channel_hash_hex, my_hex, KICK
):
    RNS.log("...", RNS.LOG_WARNING)
    remove_members = None

# ✅ Outbound guard — actions.update_membership re-checks before publishing
can_kick = storage.has_permission(channel_hash, my_hex, KICK)
remove_members = remove_members if can_kick else []

# ❌ Client gate only — not sufficient
if (canKick) buildKickButton()
```

## Adding a new permission

When adding a new permission to `ALL_PERMISSIONS`:
1. Add the client gate (hide the relevant control in the Flutter UI).
2. Add an outbound guard in the `actions.py` function or API endpoint.
3. Add core enforcement in the relevant core method.
4. Add an adversarial test in `tests/test_adversarial.py` that bypasses the
   client and calls the core method directly.
