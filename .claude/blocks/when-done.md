<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
<!-- delivery: import — universal completion protocol; each role names its own terminal status -->

## When Done

Before reporting complete:

1. Tests pass — `make test PATHS=<path>`.
2. Lint is clean — `make lint PATHS=<path>`.
3. Your changes are committed in a self-consistent state.
4. The task record carries a final checkpoint note.
5. The record is set to your role's terminal status.

Then report in this shape:

| Section | Content |
|---|---|
| Outcome | One line: what now exists that didn't before |
| Files | Paths touched, with commit SHAs |
| Verification | The commands you ran and their actual output — not a claim that they passed |
| Follow-ups | Work you deliberately left undone, each as a named task |

Report failures as failures. A test that fails, a step you skipped, a check you couldn't run — say so, with the output. Never describe unverified work as verified, and never infer success from a command you didn't run.
