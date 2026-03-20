---
description: Run the full test suite before considering any feature or fix complete
alwaysApply: true
---

# Run Tests After Every Change

After adding a feature, fixing a bug, or making any non-trivial code change, you MUST run the full test suite and confirm it passes before considering the task complete.

## Command

```bash
cd /home/hunter/repos/TrenchChat && .venv/bin/python -m pytest tests/ -v
```

## Rules

- Every test must pass. A partial pass is not acceptable.
- If any tests fail, fix them before finishing — do not leave the codebase in a broken state.
- If you add new behaviour, add a corresponding test for it in the appropriate `tests/test_*.py` file.
- Do not skip or mark tests as `xfail` to paper over failures.

## What counts as "a change"

- Any edit to `trenchchat/` source files
- Any new feature, refactor, or bug fix
- Does NOT apply to: documentation-only edits, changes to `pytest.ini` / `requirements.txt`, or changes inside `tests/` that don't touch production code
