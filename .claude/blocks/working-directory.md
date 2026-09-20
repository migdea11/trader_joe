<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
<!-- delivery: import — universal, pulled into CLAUDE.md via @ -->

## Working Directory

Run every command from the repository root. Never `git -C <path>`, never `cd <path> && ...`.

Each command *spelling* needs its own permission-allowlist entry, so a new variant of a command you already have means a fresh approval prompt in the middle of your task. Pin the forms below. If you need an operation not listed, prefer the simplest standard form over inventing a variant.

| Purpose | Form |
|---|---|
| Recent commits | `git log --oneline -N` |
| Working state | `git status --short` |
| Changeset | `git diff <base>..HEAD` |
| Inspect a commit | `git show <sha>` |
| Current branch | `git branch --show-current` |
| Run tests | `make test PATHS=<path>` |
| Lint | `make lint PATHS=<path>` |
| Format | `make lint-fix PATHS=<path>` |
