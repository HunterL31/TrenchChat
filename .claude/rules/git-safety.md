---
description: Git commit/push safety rules, plus commit message and PR description formatting
alwaysApply: true
---

# Git Safety

## Never commit or push without being explicitly asked

Do NOT run any of the following unless the user has explicitly requested it in their message:

- `git commit`
- `git push`
- `git merge`
- `git rebase`
- `git tag`
- `git reset` (destructive forms)

Making file changes is fine. Staging (`git add`) as part of a requested commit is fine. Everything else stops at the file edit stage until the user says to commit or push.

## Never push directly to main

`main` is a protected branch. All changes must go through a pull request.

```bash
# ❌ NEVER
git push origin main
git commit ... && git push origin main

# ✅ ALWAYS
git checkout -b fix/description
# make changes
# tell the user: "Ready to commit — want me to open a PR?"
```

## Correct workflow for CI/config fixes

1. Create a fix branch: `git checkout -b fix/description`
2. Make the file changes
3. Stop, tell the user what changed and ask if they want a commit and PR
4. Only commit and push after explicit approval

## When the user asks to commit

Follow the committing-changes-with-git protocol: stage, commit with a clear message, then ask before pushing unless push was also explicitly requested.

## Commit messages: 1-2 lines, no more

A commit message is a subject line, at most one short follow-up line. State what
changed and why in plain words, no bullet lists, no restating the diff, no
filler. Same clean/concise/simple standard as docstrings and comments (see
`.claude/rules/code-standards.md`).

```
# ❌ too long, restates the diff
Add full_sync permission

This change adds a new full_sync permission to the permissions system.
It updates permissions.py to add the new permission, updates the storage
layer to persist it, updates the GUI to expose a checkbox for it, and
adds tests to test_permissions.py and test_adversarial.py covering the
new behavior in various scenarios including edge cases.

# ✅ 1-2 lines
Make full_sync a per-role permission instead of a channel-wide flag

Lets admins grant it to specific roles rather than all-or-nothing.
```

## PR descriptions: as slim as possible

A PR description is a short paragraph or a few bullets naming the major
changes, not a changelog, not a restatement of every commit, not a walkthrough
of the diff. If a reviewer needs more detail than a slim description gives
them, that detail belongs in the code or the commit history, not padding in
the PR body.

## Never link a Claude session in a PR description

A `claude.ai/code/session_...` link is useless to everyone reading the PR
(only its author can open it) and it goes stale the moment the session ends.
Leave it out of PR titles, bodies, and PR comments.

```
# ❌
Fixes the sync badge.

https://claude.ai/code/session_01ABC...

# ✅
Fixes the sync badge.
```
