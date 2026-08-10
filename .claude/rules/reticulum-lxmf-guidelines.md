---
description: Correct usage of the Reticulum (RNS) and LXMF APIs in TrenchChat
alwaysApply: true
---

# Reticulum / LXMF Guidelines

## Delivery destination pattern

LXMF delivery is addressed by a *destination hash*, not a raw identity hash.
Always derive the delivery destination hash before calling `Identity.recall()`:

```python
# ✅ CORRECT
delivery_dest_hash = RNS.Destination.hash(identity_hash_bytes, "lxmf", "delivery")
dest_identity = RNS.Identity.recall(delivery_dest_hash)

# ❌ WRONG — recall() takes a destination hash, not an identity hash
dest_identity = RNS.Identity.recall(identity_hash_bytes)
```

## Building outbound LXMF destinations

Always use the literal aspect strings `"lxmf"` and `"delivery"` for peer-to-peer delivery:

```python
dest = RNS.Destination(
    dest_identity,
    RNS.Destination.OUT,
    RNS.Destination.SINGLE,
    "lxmf",
    "delivery",
)
```

## Constructing LXMessages

- Set `desired_method=LXMF.LXMessage.DIRECT` for all peer-to-peer messages.
- Use `lxm.fields` for structured protocol data (see `protocol-constants` rule).
- Use `lxm.content` (the constructor's `content` arg) for human-readable text only.
- Control messages carry no human-readable content — pass `""` as content.

```python
lxm = LXMF.LXMessage(
    dest,
    self._router.delivery_destination,
    "",                                  # empty for control messages
    desired_method=LXMF.LXMessage.DIRECT,
)
lxm.fields = { F_MSG_TYPE: MT_SUBSCRIBE, F_CHANNEL_HASH: channel_hash_bytes }
self._router.send(lxm)
```

## Path requests — no blocking

When `Identity.recall()` returns `None`, request the path and return immediately.
**Never** block with `time.sleep()` waiting for a path to resolve.

```python
# ✅ CORRECT — fire-and-forget; caller queues for retry
if dest_identity is None:
    RNS.Transport.request_path(delivery_dest_hash)
    self._pending.setdefault(dest_hex, []).append(msg_params)
    return

# ❌ WRONG — blocks the RNS thread
timeout = time.time() + 10
while dest_identity is None and time.time() < timeout:
    time.sleep(0.5)
    dest_identity = RNS.Identity.recall(delivery_dest_hash)
```

## Announce handlers

Announce handler classes must define the `aspect_filter` class attribute and implement
`received_announce(destination_hash, announced_identity, app_data)`.
Register with `RNS.Transport.register_announce_handler(handler_instance)`.

```python
class MyAnnounceHandler:
    aspect_filter = "trenchchat.channel"   # or "lxmf.delivery" for peer handlers

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes):
        ...
```

## Logging

Always use `RNS.log()` — never `print()` or the stdlib `logging` module.
Prefix every message with `"TrenchChat:"` (or `"TrenchChat [subsystem]:"` for
subsystem-specific messages) so log lines are easy to grep.

```python
RNS.log("TrenchChat: delivery failed for peer", RNS.LOG_WARNING)
RNS.log("TrenchChat [invite]: join request received", RNS.LOG_NOTICE)
```

Log level guide:
- `LOG_DEBUG`  — verbose trace, disabled in normal operation
- `LOG_NOTICE` — normal lifecycle events (connected, announced, joined)
- `LOG_WARNING` — recoverable problems (path unknown, parse error, rejected message)
- `LOG_ERROR`  — unexpected failures that may affect correctness

## Resolving sender identity from an inbound LXMessage

`message.source_hash` is the LXMF *delivery destination* hash, not the raw identity hash.
Resolve it with `Identity.recall()` to get the identity, then read `.hash` for the identity hash:

```python
sender_identity = RNS.Identity.recall(message.source_hash) if message.source_hash else None
sender_hex = sender_identity.hash.hex() if sender_identity else (
    message.source_hash.hex() if message.source_hash else ""
)
```
