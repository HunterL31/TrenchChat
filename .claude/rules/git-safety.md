---
description: Git commit and push safety rules — never commit or push without explicit user request
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
3. Stop — tell the user what changed and ask if they want a commit and PR
4. Only commit and push after explicit approval

## When the user asks to commit

Follow the committing-changes-with-git protocol: stage, commit with a clear message, then ask before pushing unless push was also explicitly requested.
