---
name: gitpush
description: Basic PUSH of changes to GitHub.
---
fully review the entire repo and push the changes with a short but descriptive comment. Make sure to update the gitignore.
input and ouput directory structure should be synced but not the files.
Split as convenient the commits and github push into related changes with its related comment.
Revome any Calude co-authoring reference in the push.

Before committing, run the same checks as CI and fix any failures so the push stays green:
1. `uv run ruff format .` (apply formatting)
2. `uv run ruff check .` (lint)
3. `uv run pytest` (tests)

Also scan the diff for hardcoded credentials (e.g. LogiNext access tokens) before committing — auth must always come from .env via lgnx_config.settings, never be pasted into code or docs.