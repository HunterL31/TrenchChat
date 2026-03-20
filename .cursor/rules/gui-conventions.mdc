---
description: PyQt6 GUI conventions and thread-safety rules for TrenchChat
globs: trenchchat/gui/**/*.py
alwaysApply: false
---

# GUI Conventions

## Thread safety — always use Qt signals

RNS and LXMF callbacks fire on background threads. **Never** call Qt widget methods
directly from a callback. Emit a Qt signal instead; Qt will marshal it to the main thread.

```python
# ✅ CORRECT — signal emitted from RNS thread, slot runs on Qt main thread
class MainWindow(QMainWindow):
    _message_received = pyqtSignal(str, str)   # channel_hash_hex, message_id

    def __init__(self, ...):
        self._message_received.connect(self._on_message_received)
        messaging.add_message_callback(self._message_received.emit)

    def _on_message_received(self, channel_hash_hex: str, message_id: str):
        # safe to update widgets here
        self._refresh_channel(channel_hash_hex)

# ❌ WRONG — widget update from a non-Qt thread will crash or corrupt state
def _rns_callback(self, channel_hash_hex, message_id):
    self._channel_list.update()   # called from RNS thread
```

## Signal naming

Signals used only within the class are prefixed with an underscore:

```python
_message_received  = pyqtSignal(str, str)
_channel_discovered = pyqtSignal(str)
_invite_received   = pyqtSignal(str, str, bytes, float, str)
```

Public signals (intended for external consumers) use plain `snake_case`.

## No business logic in GUI files

GUI code should only:
- Read data from `Storage` for display purposes.
- Delegate mutations to the appropriate core manager (`ChannelManager`, `Messaging`,
  `SubscriptionManager`, `InviteManager`).

Do not put LXMF message construction, protocol field access, or subscription logic
inside `trenchchat/gui/`. If you find yourself importing `LXMF` or `F_MSG_TYPE` in a
GUI file, move that logic to a core module.

```python
# ✅ GUI delegates to core
self._subscription_mgr.subscribe(channel_hash_hex, owner_hash_hex)

# ❌ GUI constructs protocol messages directly
lxm = LXMF.LXMessage(dest, src, "", desired_method=LXMF.LXMessage.DIRECT)
lxm.fields = {F_MSG_TYPE: MT_SUBSCRIBE, ...}
```

## Dialog ownership

Dialogs are created with `self` as parent so Qt manages their lifetime.
Always call `dialog.exec()` (blocking) or `dialog.show()` (non-blocking) — never
instantiate a dialog without showing it.

## Storage access in GUI

Read-only queries (for populating lists, message bubbles, etc.) may be called
directly on `Storage`. Write operations (insert, update, delete) must go through
the appropriate core manager so business rules and callbacks are applied correctly.
