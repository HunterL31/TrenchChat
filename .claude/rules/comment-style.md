---
description: Keep comments minimal — explain confusing code, not design decisions
alwaysApply: true
---

# Comment Style

Keep comments to a minimum. Only write one when it explains genuinely confusing
code — a non-obvious constraint, a subtle invariant, a workaround for a specific
bug, or behavior that would surprise a reader. Well-named identifiers and clear
code should speak for themselves.

Do not use comments to justify design decisions, explain what the code does, or
narrate the current task/fix. That belongs in the PR description or commit
message, not in the source — it rots as the codebase evolves.

```python
# ❌ justifies a design decision
# Using a dict here instead of a class for simplicity
config = {"timeout": 30, "retries": 3}

# ❌ explains what the code does (already obvious from the code)
# Loop through all channels and check membership
for channel in channels:
    if user in channel.members:
        ...

# ✅ explains genuinely confusing code — a non-obvious constraint
# LXMF may deliver this as bytes depending on msgpack encoding; must coerce.
value = fields.get(F_DISPLAY_NAME, "")
if isinstance(value, bytes):
    value = value.decode(errors="replace")
```
