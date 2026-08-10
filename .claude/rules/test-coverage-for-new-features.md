---
description: Ensure new functionality is covered by tests
alwaysApply: true
---

# Test Coverage for New Functionality

Any time you add new functionality to `trenchchat/`, you must verify it is covered by tests before finishing.

## Steps

1. After writing the feature, check whether existing tests already exercise the new behaviour.
2. If not, add a test in the appropriate `tests/test_*.py` file:
   - `test_storage.py` — database layer (no networking)
   - `test_channels.py` — channel creation and discovery
   - `test_messaging.py` — message send/receive
   - `test_subscriptions.py` — subscribe/unsubscribe protocol
   - `test_invites.py` — invite-only channel flow
   - `test_sync.py` — offline sync mechanisms
   - `test_permissions.py` — role/permission logic and dialog
   - `test_adversarial.py` — malicious clients bypassing permission checks
   - Create a new file if the feature doesn't fit any of the above.
3. Run the full suite and confirm everything passes.

## What requires a test

- New public methods or functions in `trenchchat/`
- New message types or protocol fields
- New config options that change runtime behaviour
- Bug fixes — add a regression test that would have caught the bug
- New permissions — add an adversarial test in `test_adversarial.py` that
  calls the core method directly without going through the GUI

## What does not require a new test

- Pure refactors with no behaviour change (existing tests are sufficient)
- GUI-only changes in `trenchchat/gui/`
- Documentation or comment edits
