<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
<!-- delivery: import — universal, pulled into CLAUDE.md via @ -->

## Task Record Workflow

Work is tracked in the task store, not in chat. The store is the handoff between sessions — anything that exists only in conversation is lost at the session boundary.

1. Read your assignment: `bd show <id>`.
2. Claim it: `bd update <id> --status in_progress`.
3. Record non-trivial decisions as you make them, marked ephemeral pending curation.
4. Blocking question? Comment `Q:@<role>: <question>`, add the label `pending-from:<role>`, and stop. Don't guess past it.
5. Hand off by setting your role's terminal status — named in your role section.

Rules:

| Rule | Why |
|---|---|
| Only the validator closes work as done | One gate, one owner |
| Finish with `bd close`, never a status | Only the built-in `closed` releases a blocking edge; a custom "done" stalls every dependent, silently |
| Never invent a status | A status with no queue is one nothing watches, and the work disappears |
| Never rewrite a decision record | Append an addendum; the superseded reasoning is the point of having a record |
| Cite a file, not a record id, for store failures | A pointer into the store is useless when the store is what's broken |
