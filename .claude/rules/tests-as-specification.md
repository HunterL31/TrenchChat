---
description: Treat existing tests as the authoritative specification — never modify them just to make them pass
alwaysApply: true
---

# Tests Are the Specification

Existing tests define the **intended behaviour contract** of the codebase. A failing test after a change is a signal that the change conflicts with that contract — not a signal to rewrite the test.

## Rules

- **Never modify an existing test just to make it pass.** If your change breaks a test, reconsider the change first.
- **A failing test is design feedback.** It means your implementation doesn't match the agreed-upon behaviour. Redesign the implementation, not the test.
- Modifying a test is only acceptable when the test itself was **provably wrong** — i.e. it was testing a bug rather than intended behaviour. This must be an explicit, conscious decision, not a shortcut.
- If a feature genuinely requires a behaviour change that invalidates an old test, the old test must be **replaced** with a new one that covers the same concern under the new design. Deleting or weakening a test without a replacement is not acceptable.
- Never use `pytest.mark.skip`, `xfail`, or conditional logic inside a test to paper over a failure.

## Workflow when a test fails

1. Read the failing test and understand what behaviour it specifies.
2. Ask: does my change violate that specification intentionally or by accident?
3. If by **accident** — fix the implementation.
4. If **intentional** — update the plan/design to account for the behaviour change, then write a replacement test before removing the old one.

```python
# ❌ WRONG — weakening a test to make it pass
def test_kick_requires_permission():
    # Removed assertion because new code doesn't enforce this yet
    pass

# ✅ RIGHT — the test is the spec; fix the implementation instead
def test_kick_requires_permission():
    result = core.kick_member(channel, actor=non_admin, target=member)
    assert result is False
```
