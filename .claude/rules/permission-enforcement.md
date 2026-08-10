---
description: Three-layer permission enforcement pattern for TrenchChat
alwaysApply: true
---

# Permission Enforcement

Every permission must be enforced at **three independent layers**. A gap at any
layer allows a bad client (or a bug) to bypass the restriction.

## The three layers

| Layer | Where | What it does |
|---|---|---|
| **GUI gate** | Context menu / button visibility | Hides the control so normal users never see it |
| **GUI outbound guard** | Action handler (`_on_*`) | Re-checks permission before calling core; discards disallowed changes even if the UI was somehow triggered |
| **Core inbound enforcement** | Core manager method | Rejects the operation regardless of caller; the only layer that protects against bad clients |

## Mapping of permissions to enforcement points

| Permission | GUI gate | GUI outbound guard | Core enforcement |
|---|---|---|---|
| `SEND_MESSAGE` | compose disabled | `_on_send_message` returns early | `_on_lxmf_message` drops message |
| `INVITE` | menu item hidden | n/a (token verification is the guard) | `_handle_join_request` checks it; `_verify_invite_token` checks it |
| `KICK` | button hidden in `MembersDialog` | `_on_view_members` filters `members_to_remove` | `publish_member_list` nulls `remove_members` |
| `MANAGE_ROLES` | button hidden in `MembersDialog` | `_on_view_members` filters `admins_to_add/remove` | `publish_member_list` nulls `add/remove_admins` |
| `MANAGE_CHANNEL` | menu item hidden | `_on_edit_permissions` returns early | n/a (member list doc is signature-validated) |

## Rules

- The GUI gate is **convenience only** — never rely on it as the sole check.
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

# ✅ GUI outbound guard — re-check after dialog closes
can_kick = self._storage.has_permission(channel_hash, my_hex, KICK)
remove_members = [m for m in dlg.members_to_remove] if can_kick else []

# ❌ GUI gate only — not sufficient
if can_kick:
    self._remove_btn.show()
```

## Adding a new permission

When adding a new permission to `ALL_PERMISSIONS`:
1. Add the GUI gate (hide the relevant control).
2. Add a GUI outbound guard in the action handler.
3. Add core enforcement in the relevant core method.
4. Add an adversarial test in `tests/test_adversarial.py` that bypasses the
   GUI and calls the core method directly.
