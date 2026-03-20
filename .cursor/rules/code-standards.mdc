---
description: Python code standards for the TrenchChat repository
alwaysApply: true
---

# Code Standards

## Import ordering

Three groups, each separated by a blank line:

```python
# 1. stdlib
import hashlib
import time

# 2. third-party
import RNS
import LXMF
import msgpack

# 3. local
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
```

## Type hints

Use type hints on all public method signatures. Use the `X | None` union syntax
(Python 3.10+), not `Optional[X]`.

```python
# ✅
def send_message(self, channel_hash_hex: str, content: str,
                 reply_to: str | None = None) -> None:

# ❌
def send_message(self, channel_hash_hex, content, reply_to=None):
```

## Naming

- `snake_case` — functions, variables, module names
- `PascalCase` — classes
- `UPPER_SNAKE_CASE` — module-level constants
- Prefix private attributes and methods with a single underscore (`_foo`).

## Error handling

- Catch specific exceptions where possible.
- Use bare `except Exception` only in callback dispatchers where one bad callback
  must not kill the others. Always log the exception.
- Never swallow exceptions silently.

```python
# ✅ callback dispatcher pattern
for cb in self._callbacks:
    try:
        cb(arg)
    except Exception as e:
        RNS.log(f"TrenchChat: callback error: {e}", RNS.LOG_ERROR)

# ❌ silent swallow
try:
    do_thing()
except Exception:
    pass
```

## bytes / str coercion from LXMF fields

LXMF may deliver string fields as `bytes` depending on msgpack encoding.
Use this pattern consistently:

```python
value = fields.get(F_DISPLAY_NAME, "")
if isinstance(value, bytes):
    value = value.decode(errors="replace")
```

If the same coercion appears more than twice in a single function, extract a helper.

## Encapsulation

Never access private attributes (`_foo`) of another class. If you need data
from a sibling module, add a public method or property to that class.

```python
# ❌ breaks encapsulation
for cb in self._messaging._message_callbacks:
    cb(channel_hash_hex, msg_id)

# ✅ add a public method to Messaging
self._messaging.notify_message_received(channel_hash_hex, msg_id)
```

## Docstrings

Every module, public class, and public method must have a docstring.
Use plain prose — not reStructuredText or Google-style parameter blocks.
One-liners are fine for simple methods; multi-line for anything non-obvious.

```python
def flush_pending(self, dest_hex: str) -> None:
    """Attempt to deliver all queued messages for a peer whose path is now known."""
```

## No dead code

Do not leave unused imports, unused methods, or commented-out code in the repo.
Remove them before committing.

## Line length

100 characters maximum. Break long lines at logical boundaries (after commas,
before operators), not mid-expression.

## Constants over magic values

Extract any repeated literal into a named module-level constant.

```python
# ❌
time.time() - 7 * 86400

# ✅
SYNC_WINDOW_SECS = 7 * 86400
time.time() - SYNC_WINDOW_SECS
```
